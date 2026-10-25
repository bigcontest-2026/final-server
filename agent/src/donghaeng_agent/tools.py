"""Read-only tools over published aggregates and captured official documents."""
import hashlib
import json
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from .repository import RepositoryError, digest, now
from .provider import PROMPT_VERSION
from .signal_rules import SIGNAL_RULE_VERSION, classify_activity, previous_month
from donghaeng_model.snapshot import load_research_snapshot


PROVINCE_NAMES = {"11": "서울", "26": "부산", "27": "대구", "28": "인천", "29": "광주",
                  "30": "대전", "31": "울산", "36": "세종", "41": "경기", "43": "충북",
                  "44": "충남", "46": "전남", "47": "경북", "48": "경남", "50": "제주",
                  "51": "강원", "52": "전북"}


@lru_cache(maxsize=3)
def _final_validator(filename):
    from jsonschema import Draft202012Validator
    return Draft202012Validator(json.loads((files("final_model") / "schemas" / filename).read_text()))


class InvestigationTools:
    def __init__(self, research_path: Path, support_path: Path, *, resource_root=None, capture_root=None,
                 i5_bundle=None, final_model_bundle=None):
        self.research_path, self.support_path = Path(research_path), Path(support_path)
        self.i5_bundle = Path(i5_bundle).resolve() if i5_bundle is not None else None
        self.final_model_bundle = Path(final_model_bundle).resolve() if final_model_bundle is not None else None
        # Infer the repository from the supplied manifest, never from this module.
        # Explicit roots also support installed packages and relocated local captures.
        inferred = self.support_path.parent
        if inferred.name == "support" and inferred.parent.name == "resources":
            inferred = inferred.parent.parent
        self.resource_root = Path(resource_root if resource_root is not None else inferred).resolve()
        self.capture_root = Path(capture_root if capture_root is not None else self.resource_root / "evidence/support").resolve()

    def versions(self):
        return {"snapshot_sha256": hashlib.sha256(self.research_path.read_bytes()).hexdigest(),
                "support_manifest_sha256": hashlib.sha256(self.support_path.read_bytes()).hexdigest() if self.support_path.exists() else None,
                "i5_manifest_sha256": self._manifest_hash(self.i5_bundle),
                "final_model_manifest_sha256": self._manifest_hash(self.final_model_bundle),
                "tool_version": "investigation-tools-2.2", "graph_version": "investigation-graph-2.1",
                "signal_rule_version": SIGNAL_RULE_VERSION,
                "prompt_version": PROMPT_VERSION}

    @staticmethod
    def _manifest_hash(bundle):
        return hashlib.sha256((Path(bundle) / "manifest.json").read_bytes()).hexdigest() if bundle is not None else None

    def _read_final(self, bundle, family, operation):
        """Verify the published bytes and original schemas before exposing evidence."""
        from final_model.src.common import verify_bundle
        if bundle is None:
            raise ValueError(f"{family}_bundle_not_configured")
        from jsonschema.exceptions import ValidationError
        try:
            before = self._manifest_hash(bundle)
            manifest = verify_bundle(bundle)
            required = ({"sgg_isolation_index.jsonl", "dong_isolation_index.jsonl"} if family == "i5"
                        else {"behavior_evidence.jsonl", "historical_reference.jsonl"})
            if not required.issubset(manifest["outputs"]):
                raise ValueError("untracked_required_output")
            expected = "survey_area_isolation_index_v1" if family == "i5" else "final_activity_evidence_v1"
            actual = manifest.get("contract_version") if family == "i5" else manifest.get("provenance", {}).get("model_version")
            if actual != expected:
                raise ValueError("unsupported_final_model_contract")
            result = operation(Path(bundle))
        except (ValidationError, KeyError, TypeError) as exc:
            raise ValueError("invalid_final_model_schema") from exc
        if before != self._manifest_hash(bundle):
            raise ValueError("bundle_changed_during_query")
        return {**result, "manifest_sha256": before}

    def get_survey_area_isolation_index(self, *, region_code=None, dong_code=None, month=None, offset=0, limit=100):
        if self.i5_bundle is None:
            return {"records": [], "total_matched": 0,
                    "availability": {"status": "data_unavailable", "reason": "i5_bundle_not_configured"},
                    "quality_status": "hold", "operational_alert_allowed": False}
        from final_model.src.survey_area_index import query
        def read(bundle):
            result = query(bundle, level="administrative_dong" if dong_code else "sgg", region_code=region_code,
                           dong_code=dong_code, month=month, offset=offset, limit=limit)
            validator = _final_validator("survey_area_index.schema.json")
            for row in result["records"]:
                validator.validate(row)
                if row["isolation_index"] != row["i5_reference"]:
                    raise ValueError("survey_score_mismatch")
            return result
        return self._read_final(self.i5_bundle, "i5", read)

    def get_final_behavior_evidence(self, *, region_code, month, sex_code=None, age_code=None, offset=0, limit=20):
        """The original 5-digit district/sex/age contract, never a dong score."""
        from final_model.src.query import query
        def read(bundle):
            result = query(bundle, region_code=region_code, month=month, sex_code=sex_code,
                           age_code=age_code, offset=offset, limit=limit)
            seen = set()
            for item in result["records"]:
                _final_validator("behavior.schema.json").validate(item["behavior"])
                key = tuple(item["behavior"]["key"][name] for name in ("region_code", "month", "sex_code", "age_code"))
                if key in seen:
                    raise ValueError("duplicate_behavior_key")
                seen.add(key)
                reference_id = item["behavior"]["historical_reference_id"]
                if reference_id is not None and (item["historical_reference"] is None or item["historical_reference"]["record_id"] != reference_id):
                    raise ValueError("invalid_behavior_historical_reference")
                if item["historical_reference"] is not None:
                    _final_validator("historical.schema.json").validate(item["historical_reference"])
            return result
        return self._read_final(self.final_model_bundle, "activity", read)

    def get_survey_mois_reference(self, *, table="national_six_group_profile", **filters):
        from final_model.src.query import survey_query
        def read(bundle):
            return {"contract_version": "survey_mois_reference_v1", "table": table,
                    "records": survey_query(bundle, table=table, **filters),
                    "automatic_10_to_6_group_join": False, "quality_status": "hold",
                    "operational_alert_allowed": False}
        return self._read_final(self.final_model_bundle, "activity", read)

    def _i5_region_aliases(self, row, manifest_sha256):
        # Keep the official province and municipality aliases introduced by the
        # nationwide bundle. Never authorize a name from an untracked registry.
        aliases = {PROVINCE_NAMES.get(row["region_code"][:2]), row["region_name"],
                   row["region_name"].split()[0].removesuffix("시")}
        manifest_bytes = (self.i5_bundle / "manifest.json").read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256:
            raise ValueError("bundle_changed_during_query")
        manifest = json.loads(manifest_bytes)
        registry_path = (self.i5_bundle / "area_registry.json").resolve()
        expected_hash = manifest["outputs"].get("area_registry.json")
        if expected_hash is None:
            if registry_path.exists():
                raise ValueError("untracked_area_registry")
            return aliases - {None}
        if not registry_path.is_relative_to(self.i5_bundle):
            raise ValueError("invalid_area_registry_path")
        registry_bytes = registry_path.read_bytes()
        if hashlib.sha256(registry_bytes).hexdigest() != expected_hash:
            raise ValueError("area_registry_hash_mismatch")
        locations = [entry for entry in json.loads(registry_bytes)
                     if entry["dong_code"] == row["dong_code"] and entry["month"] == row["month"]]
        if len(locations) != 1 or locations[0]["region_code"] != row["region_code"]:
            raise ValueError("invalid_i5_region_registry")
        province = locations[0]["province_name"]
        aliases.add(province)
        for suffix in ("특별자치도", "특별자치시", "광역시", "특별시", "도"):
            if province.endswith(suffix):
                aliases.add(province.removesuffix(suffix))
        return aliases - {None}

    def resolve_region(self, request):
        # Nationwide I5 determines geography only from an exact published dong row.
        # Region aliases are validated against the same verified bundle.
        if self.i5_bundle is not None:
            if request["measurement_as_of"].replace("-", "") not in {f"2025{month:02d}" for month in range(7, 13)}:
                raise RepositoryError("unsupported_measurement_period", "현재 공개된 I5 근거의 관측월 범위를 벗어났습니다.", 422)
            result = self.get_survey_area_isolation_index(dong_code=request["region_code"],
                                                        month=request["measurement_as_of"].replace("-", ""), limit=2)
            if result["total_matched"] > 1:
                raise ValueError("duplicate_i5_region_month")
            if result["records"]:
                row = result["records"][0]
                allowed_cities = self._i5_region_aliases(row, result["manifest_sha256"])
                if request["city"] not in allowed_cities:
                    raise RepositoryError("region_city_mismatch", "행정동 코드와 요청 도시·광역지역이 일치하지 않습니다.", 422)
                return {"dong_name": row["area_name"], "district": row["region_name"],
                        "parent_region_code": row["region_code"]}
        snapshot = load_research_snapshot(self.research_path)
        records = snapshot.mobility.records + (snapshot.paper_behavior_index.records if snapshot.paper_behavior_index else [])
        matches = [r for r in records if r.dong_code == request["region_code"] and r.city == request["city"]]
        if not matches:
            raise RepositoryError("unsupported_region", "관측 자료의 정확한 지역 코드·도시 조합이 없습니다.", 422)
        if request["measurement_as_of"] not in snapshot.mobility.periods and (not snapshot.paper_behavior_index or request["measurement_as_of"] != snapshot.paper_behavior_index.period):
            raise RepositoryError("unsupported_measurement_period", "해당 관측월의 검증 가능한 스냅샷이 없습니다.", 422)
        return {"dong_name": matches[0].dong_name, "district": matches[0].district}

    def measurements(self, request, execution_id):
        source_versions = self.versions()
        snapshot = load_research_snapshot(self.research_path)
        version = hashlib.sha256(self.research_path.read_bytes()).hexdigest()
        region = self.resolve_region(request)
        observations, evidence = [], []
        activity_assessments = []

        def add(kind, data, status, population, period, unit, limitations):
            eid = f"ev_{execution_id}_{kind}"
            item = {"evidence_id": eid, "kind": kind, "source_id": "published_research_snapshot",
                    "version_hash": version, "retrieved_at": now(), "observation_period": period,
                    "population": population, "geography": {**region, "city": request["city"], "region_code": request["region_code"]},
                    "unit": unit, "extracted_values": data, "limitations": limitations,
                    "source_url": None, "public_aggregate_only": True}
            evidence.append(item)
            observations.append({"kind": kind, "status": status, "data": data, "evidence_id": eid,
                                 "population": population, "period": period, "limitations": limitations})

        # District-age survey is never copied to a dong score or to another city.
        survey = next((r for r in snapshot.survey.records if request["city"] == "서울" and r.district == region["district"] and r.age_group == request["age_group"]), None)
        published = (survey is not None and survey.score is not None and survey.n >= 30 and survey.n_eff is not None
                     and survey.n_eff >= 20 and survey.quality == "adequate_descriptive_sample")
        survey_data = survey.model_dump(mode="json") if survey else None
        if survey_data is not None and not published:
            survey_data.update(score=None, ci_low=None, ci_high=None, support_absence=None, loneliness=None)
        add("survey", survey_data,
            "available" if published else "held", snapshot.survey.population, str(snapshot.survey.year),
            "자치구×연령 직접응답 구성지수(0~100)", ["연간 자치구×연령 근거이며 동별·개인 고립라벨이 아닙니다.",
              "지원망 부재와 외로움 50/50은 규범적 구성입니다."] + ([] if published else ["이 도시·연령의 공개 가능한 직접 설문이 없습니다."]))
        behavior = next((r for r in (snapshot.paper_behavior_index.records if snapshot.paper_behavior_index else [])
                         if r.dong_code == request["region_code"] and r.city == request["city"] and r.period == request["measurement_as_of"]), None)
        add("behavior", behavior.model_dump(mode="json") if behavior and behavior.score is not None else None,
            "available" if behavior and behavior.score is not None else "held", "서울 통신집계 20~39세", request["measurement_as_of"],
            "지역 행동 대리지수(0~100)", ["20~39세 고정 모집단을 20대/30대 점수로 복제하지 않습니다.",
              "개인 고립확률·진단·고립 인구 비율이 아니며 동일 공간 기준월의 상대 구성입니다."])
        if request["sk_mode"] != "excluded":
            rows = [r.model_dump(mode="json") for r in snapshot.mobility.records if r.city == request["city"] and r.dong_code == request["region_code"]
                    and r.age_group == request["age_group"] and r.period <= request["measurement_as_of"]]
            rows.sort(key=lambda r: r["period"])
            exact = next((r for r in rows if r["period"] == request["measurement_as_of"]), None)
            mutation = request["sk_mode"] == "quality_mutation"
            if mutation and exact:
                # One effective-window quality mutation: remove the selected month's observation.
                exact.update(flow_total=None, change_pct=None, quality="evaluation_missing_selected_month", coverage=0,
                             observed_coordinates=0, notes=exact["notes"] + ["평가 전용: 선택 관측월을 누락으로 변경. 원자료 파일은 보존합니다."])
            missing = [p for p in snapshot.mobility.periods if p <= request["measurement_as_of"] and not any(r["period"] == p and r["flow_total"] is not None for r in rows)]
            complete = exact and exact["flow_total"] is not None and exact.get("coverage", 1) >= 1 and not missing and exact["quality"] == "complete_fixed_footprint"
            status = "available" if complete else "partial" if exact and exact["flow_total"] is not None else "held" if mutation else "unavailable"
            baseline_months = ["2025-07", "2025-08"]
            reference_rows = [r.model_dump(mode="json") for r in snapshot.mobility.records if r.city == request["city"] and r.dong_code == request["region_code"]
                              and r.age_group == request["age_group"] and r.period in baseline_months]
            if mutation and exact and exact["period"] in baseline_months:
                reference_rows = [exact if r["period"] == exact["period"] else r for r in reference_rows]
            baseline_complete = len(reference_rows) == 2 and all(r["flow_total"] is not None and r.get("coverage", 1) == 1 for r in reference_rows)
            baseline_numeric = sum(r["flow_total"] for r in reference_rows) / 2 if baseline_complete else None
            add("sk", {"selected": exact, "window": rows, "missing_periods": missing,
                       "baseline_numeric": baseline_numeric, "baseline_months": baseline_months, "baseline_complete": baseline_complete,
                       "baseline_coverage": [{"period": r["period"], "coverage": r.get("coverage"), "fixed_coordinates": r.get("fixed_coordinates"), "observed_coordinates": r.get("observed_coordinates")} for r in reference_rows],
                       "reference_available_after": "2025-08", "reference_is_retrospective_for_selected_month": request["measurement_as_of"] < "2025-08",
                       "footprint": snapshot.mobility.model_dump().get("footprint"), "reference_period": snapshot.mobility.model_dump().get("reference_period"),
                       "quality_mutation": mutation}, status, request["age_group"] + " 제공 관측 거점", request["measurement_as_of"],
                snapshot.mobility.unit, ["고정 고활동 거점의 제공 유동량으로 동 전체·고유 방문자·주민·고립라벨이 아닙니다.",
                  "관측 0과 자료 누락을 구분하고 이전 유효월로 선택월 누락을 숨기지 않습니다.",
                  "552행은 독립 고립 라벨 552개가 아닙니다."])
        if self.i5_bundle is not None:
            result = self.get_survey_area_isolation_index(dong_code=request["region_code"],
                                                        month=request["measurement_as_of"].replace("-", ""), limit=2)
            row = result["records"][0] if result["records"] else None
            add("survey_i5_population_reference", row, "available" if row is not None else "unavailable",
                "전국 설문 성·연령 6집단을 해당 지역 20세 이상 주민등록 인구구성으로 가중",
                request["measurement_as_of"], "인구구성 가중 전국 설문 참고점수(0~100)",
                ["지역 직접 설문·개인 고립확률·고립률·요청 연령 전용 점수가 아닙니다.",
                 "월별 차이는 인구구성 차이이며 같은 지역의 반복 설문 결과가 아닙니다.",
                 "행동·통신·카드 근거와 임의 합산하지 않습니다."])
            evidence[-1].update(source_id="national_i5_population_reference",
                                version_hash=result["manifest_sha256"],
                                quality_status="hold", operational_alert_allowed=False)
            if row is not None:
                evidence[-1]["geography"].update(spatial_unit=row["spatial_unit"], parent_region_code=row["region_code"])
            observations[-1].update(quality_status="hold", operational_alert_allowed=False)
        if self.final_model_bundle is not None and region.get("parent_region_code"):
            # This is explicitly a parent-region context. Sex rows remain separate.
            result = self.get_final_behavior_evidence(region_code=region["parent_region_code"],
                     month=request["measurement_as_of"].replace("-", ""), age_code={"20대": "20G", "30대": "30G"}[request["age_group"]])
            prior = self.get_final_behavior_evidence(region_code=region["parent_region_code"],
                     month=previous_month(request["measurement_as_of"].replace("-", "")),
                     age_code={"20대": "20G", "30대": "30G"}[request["age_group"]])
            prior_by_sex = {item["behavior"]["key"]["sex_code"]: item["behavior"] for item in prior["records"]}
            activity_assessments = [classify_activity(item["behavior"],
                prior_by_sex.get(item["behavior"]["key"]["sex_code"])) for item in result["records"]]
            add("final_activity_context", result, "available" if result["records"] else "unavailable",
                "원자료 성·연령별 관측장소 유동량·가맹점 결제 집계; 식별 주민 아님",
                request["measurement_as_of"], "원본 단위·Q/H/T·변화율; 성별 행을 별도 유지",
                ["5자리 시군구의 배경 근거이며 요청 행정동의 관측값이 아닙니다.",
                 "행정동·주민 개인으로 전파하거나 성별 집단을 임의 합산하지 않습니다.",
                 "C3 과거 참고는 카드와 같은 출처이며 독립적인 추가 관측으로 세지 않습니다."])
            evidence[-1].update(source_id="final_activity_evidence", version_hash=result["manifest_sha256"],
                                geography={"spatial_unit": "sgg", "region_code": region["parent_region_code"],
                                           "scope_relation": "containing_region_context"},
                                quality_status="hold", operational_alert_allowed=False)
            observations[-1].update(quality_status="hold", operational_alert_allowed=False,
                                    geography=evidence[-1]["geography"], signal_assessments=activity_assessments)
        if source_versions != self.versions():
            raise ValueError("source_version_changed_during_measurement_read")
        return {"status": "ok", "scope": {**request, **region}, "observations": observations, "evidence": evidence,
                "activity_assessments": activity_assessments,
                "sk_mode": request["sk_mode"], "snapshot_version": version,
                "source_versions": source_versions,
                "quality_status": "hold", "operational_alert_allowed": False,
                "measurement_status": "available" if all(o["status"] == "available" for o in observations) else "partial"}

    def quality(self, measurements):
        actions, questions = [], []
        for obs in measurements["observations"]:
            for assessment in obs.get("signal_assessments", []):
                if assessment["category"] in ("single_source_decline", "joint_decline", "priority_review"):
                    actions.append({"type": "check_context", "reason": assessment["category_label"] + ": " + " ".join(assessment["reasons"]),
                                    "evidence_ids": [obs["evidence_id"]]})
            if obs["status"] != "available":
                questions.append({"id": "quality_" + obs["kind"], "question": f"{obs['kind']}의 해당 지역·관측시점 근거를 추가 확보할 수 있습니까?", "evidence_ids": [obs["evidence_id"]]})
                actions.append({"type": "request_measurement_evidence", "reason": obs["kind"] + " 근거 부족", "evidence_ids": [obs["evidence_id"]]})
            if obs["kind"] == "sk" and obs["status"] == "available":
                selected = obs["data"]["selected"]
                if abs(selected["change_pct"] or 0) >= 10:
                    actions.append({"type": "check_context", "reason": "선택월의 활동 변화 원인 확인: 기상·공휴일·거점 대표성", "evidence_ids": [obs["evidence_id"]]})
                    questions.append({"id": "sk_context", "question": "동일 기간의 기상·휴일·거점 변경 근거가 있습니까? 활동 변화의 원인은 현재 확정하지 않습니다.", "evidence_ids": [obs["evidence_id"]]})
                baseline = obs["data"]["baseline_numeric"]
                if baseline is not None and baseline < 1000:
                    actions.append({"type": "check_small_base", "reason": "7/8월 제공 유동량 기준평균 1000 미만: 절대량·거점 대표성 확인", "evidence_ids": [obs["evidence_id"]]})
                    questions.append({"id": "sk_small_base", "question": "작은 기초량에서의 변화율입니다. 관측 거점과 절대량을 함께 검토했습니까?", "evidence_ids": [obs["evidence_id"]]})
        return {"status": "partial" if questions else "ok", "actions": actions, "questions": questions,
                "activity_rule_version": SIGNAL_RULE_VERSION,
                "activity_assessments": measurements.get("activity_assessments", []),
                "context_connector": "unavailable", "causal_claim_allowed": False,
                "rule_config": {"change_absolute_pct": 10, "baseline_numeric_min": 1000, "validation_status": "development_inquiry_rules; not_validated_isolation_risk_cutoffs"}}

    def manifest(self):
        if not self.support_path.exists():
            return {"sources": [], "status": "unavailable", "reason": "official_manifest_missing"}
        return json.loads(self.support_path.read_text(encoding="utf-8"))

    def catalog_search(self, query="", limit=20):
        meta = self.manifest().get("catalog", {})
        if not meta.get("path"):
            return {"status": "unavailable", "error": {"code": "catalog_missing"}, "items": [], "total_matches": 0, "detail_verified": False, "eligibility_status": "unknown"}
        catalog = json.loads(self.source_path(meta["path"]).read_text(encoding="utf-8"))
        if hashlib.sha256(self.source_path(meta["raw_path"]).read_bytes()).hexdigest() != meta["sha256"]:
            return {"status": "failed", "error": {"code": "catalog_hash_mismatch"}, "items": [], "total_matches": 0, "detail_verified": False, "eligibility_status": "unknown"}
        terms = query.casefold().split()
        items = [x for x in catalog["items"] if all(term in " ".join(str(x.get(k, "")) for k in ("service_id", "title", "summary", "ministry")).casefold() for term in terms)]
        results = [{**x, "detail_verified": False, "eligibility_status": "unknown", "source_kind": "official_catalog", "verification_level": "metadata_only"} for x in items[:max(1, min(limit, 100))]]
        return {"status": "ok", "query": query, "items": results, "total_matches": len(items), "actual_rows": catalog["actual_rows"],
                "raw_sha256": meta["sha256"], "source_url": catalog["source_url"], "retrieved_at": catalog["retrieved_at"], "verification_level": "metadata_only",
                "detail_verified": False, "eligibility_status": "unknown", "warnings": ["실제 중앙 지원 목록의 메타데이터 검색입니다. 상세 대상·선정·신청 조건은 미확인이며 자격 판정에 사용하지 않습니다."]}

    def search(self, request, support_type="general", exclude_ids=None):
        data = self.manifest()
        sources = data.get("sources", data.get("supports", []))
        # Same fixed captured corpus for every strategy; no invented online API result.
        return {"status": "ok" if sources else "unavailable", "candidates": [{"id": s.get("id", s.get("support_id")), "title": s["title"], "url": s.get("url", s.get("source_url")), "regions": s.get("regions", []), "source_kind": s.get("source_kind")} for s in sources
                    if s.get("id", s.get("support_id")) not in (exclude_ids or [])],
                "support_type": support_type, "retrieval_mode": "captured_official_corpus", "retrieved_at": now(),
                "warnings": ["보관된 공식 원문 묶음 조회입니다. 실시간 API 성공·현재 접수 수용을 뜻하지 않습니다."]}

    def source_path(self, value):
        path = Path(value)
        path = path if path.is_absolute() else self.resource_root / path
        path = path.resolve()
        allowed = (self.support_path.parent.resolve(), self.capture_root)
        if not any(path.is_relative_to(root) for root in allowed):
            raise ValueError("support capture must be inside configured support or capture directory")
        return path

    def details(self, candidate_id):
        source = next((s for s in self.manifest().get("sources", []) if s["id"] == candidate_id), None)
        if not source:
            return {"status": "unavailable", "error": {"code": "source_not_found"}, "candidate_id": candidate_id}
        try:
            path = self.source_path(source["capture_path"])
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual_hash != source["sha256"]:
                return {"status": "failed", "error": {"code": "capture_hash_mismatch"}, "candidate_id": candidate_id}
            # Extraction is separately hashed, because raw HTML and extracted text are distinct evidence.
            text_path = self.source_path(source["content_text_path"])
            text_bytes = text_path.read_bytes()
            text_hash = hashlib.sha256(text_bytes).hexdigest()
            expected_text_hash = source.get("content_text_sha256")
            if expected_text_hash and text_hash != expected_text_hash:
                return {"status": "failed", "error": {"code": "extraction_hash_mismatch"}, "candidate_id": candidate_id}
            text = text_bytes.decode("utf-8")
            if any(c.get("source_quote") and c["source_quote"] not in text for c in source.get("conditions", [])):
                return {"status": "failed", "error": {"code": "condition_quote_not_in_capture"}, "candidate_id": candidate_id}
            return {"status": "ok", "source": source, "content_hash": actual_hash, "text_hash": text_hash,
                    "metadata_hash": self.support_signature(source), "retrieval_mode": "verified_local_official_capture", "capture_verified": True}
        except (OSError, ValueError, KeyError):
            return {"status": "unavailable", "error": {"code": "capture_unavailable"}, "candidate_id": candidate_id}

    @staticmethod
    def support_signature(source):
        return digest({k: source.get(k) for k in ("conditions", "regions", "age_min", "age_max", "period", "contact", "url", "summary")})

    def check_conditions(self, request, detail):
        if detail["status"] != "ok":
            return {"eligibility_status": "unverified_source", "conditions": [], "excluded": True, "reasons": [detail.get("error", {}).get("code", "detail_missing")]}
        source, checks = detail["source"], []
        regions = source.get("regions", [])
        geo = "match" if request["city"] in regions or "전국" in regions else "no_match" if regions else "unknown"
        checks.append({"id": "scope_region", "label": "대상 지역", "status": geo, "reason": "공식 상세 대상지역과 사건 도시 비교", "value": regions})
        age_min, age_max = source.get("age_min"), source.get("age_max")
        lo, hi = (20, 29) if request["age_group"] == "20대" else (30, 39)
        age = "unknown" if age_min is None or age_max is None else "match" if age_min <= lo and hi <= age_max else "no_match" if hi < age_min or lo > age_max else "unknown"
        checks.append({"id": "group_age_range", "label": "연령 범위", "status": age, "reason": "연령집단의 범위만 비교. 정확 생년월일·개인 자격은 미확정", "value": [age_min, age_max]})
        period = source.get("period", {})
        ref = request["support_as_of"]
        expired = bool(period.get("end") and ref > period["end"])
        future = bool(period.get("start") and ref < period["start"])
        status = "no_match" if expired or future or period.get("status") == "closed" else "match" if period.get("status") == "open" and period.get("start") and period.get("end") else "unknown"
        checks.append({"id": "source_application_window", "label": "접수 기간·상태", "status": status, "reason": "접수상태와 기간 원문 모두 확인한 경우만 현재 접수 가능", "value": period})
        for condition in source.get("conditions", []):
            kind = condition.get("kind")
            checked = age if kind == "age" and condition.get("operator") == "between" else geo if kind == "geography" else "unknown"
            checks.append({"id": condition["id"], "label": condition.get("label", condition["id"]), "status": checked,
                           "reason": "공식 원문 조건을 보존합니다. 지역 집계에는 개인·가구 자격, 모집기관별 접수와 수용의 확정 사실이 없습니다.",
                           "source_quote": condition.get("source_quote"), "kind": kind, "operator": condition.get("operator"), "value": condition.get("value")})
        if not any(c["status"] == "unknown" and c["id"] not in ("source_application_window", "group_age_range") for c in checks):
            checks.append({"id": "individual_eligibility", "label": "개인 자격·수용 가능성", "status": "unknown", "reason": "집계 연령·도시만으로 실제 이용 자격과 접수 수용을 확정할 수 없습니다."})
        excluded = any(c["status"] == "no_match" for c in checks)
        return {"eligibility_status": "excluded" if excluded else "needs_confirmation", "conditions": checks, "excluded": excluded,
                "reasons": [c["label"] for c in checks if c["status"] == "no_match"], "currently_accepting_confirmed": False}

    def recheck(self, case):
        changes = []
        for ev in case["evidence"]:
            if ev["source_id"] in ("national_i5_population_reference", "final_activity_evidence"):
                is_i5 = ev["source_id"] == "national_i5_population_reference"
                try:
                    reader = self.get_survey_area_isolation_index if is_i5 else self.get_final_behavior_evidence
                    args = {"dong_code": case["request"]["region_code"]} if is_i5 else {"region_code": ev["geography"]["region_code"]}
                    result = reader(**args, month=case["request"]["measurement_as_of"].replace("-", ""), limit=1)
                    current_hash = result["manifest_sha256"]
                    status = "same" if current_hash == ev["version_hash"] else "changed"
                except (OSError, ValueError, KeyError):
                    current_hash, status = None, "unavailable"
                changes.append({"source_id": ev["source_id"], "evidence_id": ev["evidence_id"],
                                "old_hash": ev["version_hash"], "new_hash": current_hash, "status": status,
                                "changed_fields": ["published_model_bundle"] if status != "same" else [],
                                "affected_brief_ids": [b["brief_id"] for b in case["briefs"] if ev["evidence_id"] in b["evidence_ids"]],
                                "rechecked_at": now(), "verification_scope": "hash_verified_published_model_bundle"})
                continue
            if ev["source_id"] == "published_research_snapshot":
                try:
                    current_hash = hashlib.sha256(self.research_path.read_bytes()).hexdigest()
                    load_research_snapshot(self.research_path)
                    status = "same" if current_hash == ev["version_hash"] else "changed"
                except (OSError, ValueError):
                    current_hash, status = None, "unavailable"
                affected = [b["brief_id"] for b in case["briefs"] if ev["evidence_id"] in b["evidence_ids"]]
                changes.append({"source_id": ev["source_id"], "evidence_id": ev["evidence_id"], "old_hash": ev["version_hash"], "new_hash": current_hash,
                                "status": status, "changed_fields": ["research_snapshot"] if status != "same" else [], "affected_brief_ids": affected,
                                "rechecked_at": now(), "verification_scope": "local_research_snapshot_only"})
                continue
            if ev["kind"] != "official_support":
                continue
            result = self.details(ev["source_id"])
            latest_hash = result.get("content_hash")
            changed_fields = []
            if result["status"] == "ok":
                if latest_hash != ev["version_hash"]:
                    changed_fields.append("raw_content")
                if result.get("text_hash") != ev.get("extraction_hash"):
                    changed_fields.append("extracted_text")
                if ev.get("metadata_hash"):
                    if result.get("metadata_hash") != ev["metadata_hash"]:
                        changed_fields.append("structured_conditions")
                else:
                    # Compatibility for an earlier saved brief: compare its preserved fields.
                    old = ev.get("extracted_values", {})
                    current = result["source"]
                    if any(old.get(k) != current.get(k) for k in ("conditions", "period", "contact")) or ev.get("geography") != current.get("regions"):
                        changed_fields.append("structured_conditions")
            status = "unavailable" if result["status"] != "ok" else "changed" if changed_fields else "same"
            affected = [b["brief_id"] for b in case["briefs"] if ev["evidence_id"] in b["evidence_ids"]]
            changes.append({"source_id": ev["source_id"], "old_hash": ev["version_hash"], "new_hash": latest_hash,
                            "status": status, "affected_brief_ids": affected, "rechecked_at": now(),
                            "changed_fields": changed_fields, "old_extraction_hash": ev.get("extraction_hash"), "new_extraction_hash": result.get("text_hash"),
                            "old_metadata_hash": ev.get("metadata_hash"), "new_metadata_hash": result.get("metadata_hash"),
                            "error": result.get("error"), "verification_scope": "local_capture_only"})
        return changes
