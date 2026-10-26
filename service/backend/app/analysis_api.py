"""Verified observations and independently saved, cited regional context research."""
from copy import deepcopy
import csv
import hashlib
import json
import logging
import math
import sqlite3
from typing import Literal

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from donghaeng_agent.analysis_reports import attach_context_investigation, build_analysis_report, previous_month, report_region_name
from donghaeng_agent.analysis_report_store import AnalysisReportStore
from donghaeng_agent.analysis_research import RESEARCH_PROMPT_VERSION, research_analysis_report
from donghaeng_agent.provider import ProviderUnavailable
from donghaeng_agent import activity_summary
from .final_model import FinalModel, ModelUnavailable
from .security import _bind_database

logger = logging.getLogger(__name__)


class AnalysisReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    region_code: str = Field(pattern=r"^(11|26|27|28|29|30|31|36|41|43|44|46|47|48|50|51|52)[0-9]{3}$")
    dong_code: str | None = Field(default=None, pattern=r"^[0-9]{10}$")
    month: str = Field(pattern=r"^[0-9]{4}(0[1-9]|1[0-2])$")
    sex_code: Literal["MAN", "WMAN"] | None = None
    age_code: Literal["20G", "30G", "40G", "50G", "60GU"] | None = None

    @model_validator(mode="after")
    def valid_dong_parent(self):
        if self.dong_code is not None and not self.dong_code.startswith(self.region_code):
            raise ValueError("dong_parent_region_mismatch")
        return self

    @field_validator("month")
    @classmethod
    def valid_month(cls, value):
        if previous_month(value) is None:
            raise ValueError("invalid_observation_month")
        return value


class AnalysisInvestigationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    comparison_report_ids: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("comparison_report_ids")
    @classmethod
    def valid_comparisons(cls, values):
        if any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError("invalid_comparison_report_id")
        if len(set(values)) != len(values):
            raise ValueError("duplicate_comparison_report_id")
        return values


def _root_report(store, report):
    seen = set()
    while report.get("parent_report_id"):
        if report["report_id"] in seen:
            raise HTTPException(409, detail={"code": "analysis_parent_report_missing", "message": "원래 관측 보고서를 확인할 수 없습니다."})
        seen.add(report["report_id"])
        report = store.get(report["parent_report_id"])
        if report is None:
            raise HTTPException(409, detail={"code": "analysis_parent_report_missing", "message": "원래 관측 보고서를 확인할 수 없습니다."})
    return report


def _comparison_context(store, base, report_ids):
    """Resolve only saved aggregate evidence, freezing the exact selected documents."""
    if not report_ids:
        return {}
    reports, families, areas = [], set(), set()
    base_area = base["scope"].get("dong_code") or base["scope"]["region_code"]
    for report_id in sorted(report_ids):
        selected = store.get(report_id)
        if selected is None:
            raise HTTPException(404, detail={"code": "analysis_comparison_not_found", "message": "선택한 비교 보고서를 찾을 수 없습니다. 저장 목록에서 다시 선택해 주세요."})
        root = _root_report(store, selected)
        scope = selected["scope"]
        area = scope.get("dong_code") or scope["region_code"]
        if root["report_id"] == base["report_id"] or area == base_area:
            raise HTTPException(422, detail={"code": "analysis_comparison_self", "message": "현재 분석과 다른 지역의 보고서를 선택해 주세요."})
        if root["report_id"] in families or area in areas:
            raise HTTPException(422, detail={"code": "analysis_comparison_duplicate", "message": "같은 지역의 보고서를 중복해서 비교할 수 없습니다."})
        families.add(root["report_id"])
        areas.add(area)
        matching = (all(scope.get(key) == base["scope"].get(key) for key in ("month", "sex_code", "age_code"))
                    and bool(scope.get("dong_code")) == bool(base["scope"].get("dong_code"))
                    and all(selected.get(key) == base.get(key) for key in ("rule_version", "threshold_percent", "provenance"))
                    and any(item.get("observation") for item in selected.get("assessments", [])))
        # Unknown data versions are not evidence of compatibility, even if both are absent.
        for report in (base, selected):
            for family in ("activity", "survey"):
                version = report.get("provenance", {}).get(family) or {}
                matching = matching and bool(version.get("run_id")) and bool(version.get("manifest_sha256"))
        if not matching:
            raise HTTPException(422, detail={"code": "analysis_comparison_incompatible", "message": "비교 보고서의 관측월·성별·연령·공간단위·자료 버전·분류 규칙이 일치하지 않거나 활동 관측이 없습니다."})
        snapshot = {key: deepcopy(selected[key]) for key in (
            "report_id", "created_at", "scope", "rule_version", "threshold_percent", "provenance", "context", "quality_status")}
        snapshot.update(region_name=report_region_name(selected), observations=deepcopy(selected["assessments"]),
                        operational_alert_allowed=False)
        if selected.get("activity_availability") is not None:
            snapshot["activity_availability"] = deepcopy(selected["activity_availability"])
        reports.append(snapshot)
    return {"report_ids": sorted(report_ids), "reports": reports}


def _research_key(comparison_context):
    if not comparison_context:
        return RESEARCH_PROMPT_VERSION
    frozen = json.dumps(comparison_context, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return RESEARCH_PROMPT_VERSION + ":comparison:" + hashlib.sha256(frozen.encode()).hexdigest()


def _provenance(root, manifest, common):
    return {"run_id": manifest["run_id"], "manifest_sha256": common.sha(root / "manifest.json")}


def _activity_input(model, scope):
    def read(root, manifest, query, survey, common, validators):
        rows = common.read_jsonl(root / "behavior_evidence.jsonl")
        histories = common.read_jsonl(root / "historical_reference.jsonl")
        history_by_id = {}
        for row in histories:
            validators["historical"].validate(row)
            if row["record_id"] in history_by_id:
                raise ValueError("duplicate_historical_key")
            history_by_id[row["record_id"]] = row
        seen, current, previous = set(), [], []
        for row in rows:
            validators["behavior"].validate(row)
            key = row["key"]
            identity = tuple(key[name] for name in ("region_code", "month", "sex_code", "age_code"))
            if identity in seen:
                raise ValueError("duplicate_behavior_key")
            seen.add(identity)
            reference_id = row["historical_reference_id"]
            if reference_id is not None and reference_id not in history_by_id:
                raise ValueError("invalid_behavior_historical_reference")
            if reference_id is not None:
                reference = history_by_id[reference_id]
                if (reference["month"].replace("-", "") != key["month"] or
                    reference["region"] != row["region_name"] or
                    reference["sex"] != {"MAN": "남성", "WMAN": "여성"}[key["sex_code"]] or
                    reference["age"] + "G" != key["age_code"] or
                    reference["dataset"] != "data2" or reference["view"] != "age_sex" or reference["calendar_type"] != "all" or
                    any(reference.get(field) != "ALL" for field in ("industry", "time_band", "residence_sido"))):
                    raise ValueError("historical_reference_scope_mismatch")
            if key["region_code"] != scope["region_code"] or any(
                scope.get(name) is not None and scope[name] != key[name] for name in ("sex_code", "age_code")):
                continue
            if key["month"] == scope["month"]:
                current.append({"behavior": row, "historical_reference": history_by_id.get(reference_id)})
            elif key["month"] == previous_month(scope["month"]):
                previous.append(row)
        name = "survey_mois_reference/region_month_summary.csv"
        mois = None
        if name in manifest["outputs"]:
            with (root / name).open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                if not {"region_code", "month", "population_total", "population_20plus", "one_person_household_share"}.issubset(reader.fieldnames or []):
                    raise ValueError("invalid_mois_columns")
                selected = [row for row in reader if row["region_code"] == scope["region_code"] and row["month"] == scope["month"]]
            if len(selected) > 1:
                raise ValueError("duplicate_mois_region_month")
            if selected:
                # Keep the original table's six-group dimensions and nulls.
                mois = {key: value if value != "" else None for key, value in selected[0].items()}
                for field, value in mois.items():
                    if value is None or field in ("region_code", "month", "region"):
                        continue
                    numeric = float(value)
                    if not math.isfinite(numeric) or (not field.startswith("monthly_change_") and numeric < 0):
                        raise ValueError("invalid_mois_numeric_value")
                    if (field.startswith("share_") or field == "one_person_household_share") and numeric > 1:
                        raise ValueError("invalid_mois_share")
        elif (root / name).exists():
            raise ValueError("untracked_mois_output")
        return {"current": current, "previous": previous, "mois": mois,
                "provenance": _provenance(root, manifest, common)}
    return model.read("activity", read)


def _survey_input(model, scope):
    def read(root, manifest, query, survey, common, validators):
        rows = model.survey_rows(root, manifest, common, validators)
        dong_code = scope.get("dong_code")
        level = "administrative_dong" if dong_code else "sgg"
        selected = [row for row in rows if row["spatial_unit"] == level and
                    row["region_code"] == scope["region_code"] and row["month"] == scope["month"]
                    and row["area_code"] == (dong_code or scope["region_code"])]
        if len(selected) > 1:
            raise ValueError("duplicate_i5_region_month")
        if dong_code and not selected:
            raise HTTPException(422, detail={"code": "analysis_dong_unavailable",
                "message": "선택한 행정동·관측월의 지역 자료를 확인할 수 없습니다. 행정동과 기준월을 다시 선택해 주세요."})
        if selected and manifest["contract_version"] == "survey_area_isolation_index_10group_v1":
            national = survey.national_profiles(root / "national_ten_group_profile.csv")
            selected = [survey.with_group_profiles(selected[0], national)]
        return {"i5": selected[0] if selected else None, "provenance": _provenance(root, manifest, common)}
    return model.read("survey", read)


def analysis_router(settings):
    router = APIRouter(prefix="/api/model/analysis-reports", tags=["analysis-reports"])
    model = FinalModel(settings)
    # An explicitly dedicated store avoids changing legacy business/checkpoint schemas.
    store = AnalysisReportStore(settings.investigation_db_path.with_name("analysis_reports.sqlite3"))
    with store.connection(write=True) as db:
        _bind_database(db, settings, ("analysis_reports", "analysis_report_research", "analysis_report_summaries"))

    def failed(exc):
        if isinstance(exc, ModelUnavailable):
            return HTTPException(503, detail={"code": exc.code, "source": exc.family,
                "message": "모델 자료 검증에 실패해 보고서를 저장하지 않았습니다. 자료 버전과 무결성을 확인해 주세요."})
        return HTTPException(503, detail={"code": "analysis_storage_unavailable",
            "message": "보고서 저장소를 사용할 수 없습니다. 잠시 후 다시 확인해 주세요."})

    @router.post("")
    def create(body: AnalysisReportRequest, request: Request):
        if request.query_params:
            raise HTTPException(422, "보고서 조건은 요청 본문에 지정해 주세요.")
        scope = body.model_dump(exclude={"dong_code"} if body.dong_code is None else set())
        try:
            activity = _activity_input(model, scope)
            survey = _survey_input(model, scope)
            provenance = {"activity": activity["provenance"], "survey": survey["provenance"]}
            report = build_analysis_report(scope=scope, current_rows=activity["current"],
                previous_rows=activity["previous"], i5=survey["i5"], mois=activity["mois"], provenance=provenance)
            # Freeze both bundle versions across the combined read, including pointers.
            for family, expected in (("activity", activity["provenance"]), ("survey", survey["provenance"])):
                actual = model.read(family, lambda root, manifest, query, survey, common, validators:
                                    _provenance(root, manifest, common))
                if actual != expected:
                    raise ModelUnavailable(family, "bundle_changed_during_query")
            return store.save(report)
        except (ModelUnavailable, sqlite3.Error, OSError) as exc:
            raise failed(exc) from exc

    @router.post("/{report_id}/summary")
    def summary(report_id: str, request: Request, body: dict | None = Body(default=None)):
        if request.query_params or body is not None:
            raise HTTPException(422, "요약에는 저장된 보고서만 사용합니다. 추가 입력은 지정할 수 없습니다.")
        if settings.access_mode != "local_development":
            raise HTTPException(503, detail={"code": "institution_provider_not_configured",
                "message": "기관에서 승인한 요약 모델이 아직 구성되지 않았습니다."})
        try:
            report = store.get(report_id)
            if report is None:
                raise HTTPException(404, detail={"code": "analysis_report_not_found", "message": "저장된 분석 보고서가 없습니다."})
            provider = activity_summary.create_provider()
            key = activity_summary.summary_key(report, provider)
            claim = store.claim_summary(report_id, key)
            if claim["status"] == "completed":
                return claim["summary"]
            if claim["status"] == "running":
                raise HTTPException(409, detail={"code": "activity_summary_in_progress",
                    "message": "이 보고서의 요약을 생성 중입니다. 잠시 후 다시 시도해 주세요."}, headers={"Retry-After": "5"})
            try:
                text = activity_summary.summarize_activity(report, provider)
                return store.finish_summary(report_id, key, claim["token"], text)
            finally:
                store.release_summary(report_id, key, claim["token"])
        except ProviderUnavailable as exc:
            busy = exc.code == "model_concurrency_limit"
            raise HTTPException(409 if busy else 503, detail={"code": "activity_summary_busy" if busy else "activity_summary_unavailable",
                "message": "다른 AI 작업이 진행 중입니다. 잠시 후 요약을 다시 시도해 주세요." if busy else
                           "활동 요약을 생성하지 못했습니다. 모델 연결을 확인한 뒤 다시 시도해 주세요."},
                headers={"Retry-After": "5"} if busy else None) from None
        except (sqlite3.Error, OSError) as exc:
            raise failed(exc) from exc

    @router.post("/{report_id}/investigate")
    def investigate(report_id: str, request: Request, body: AnalysisInvestigationRequest = Body(default_factory=AnalysisInvestigationRequest)):
        if request.query_params:
            raise HTTPException(422, "저장된 보고서의 범위로 조사합니다. 추가 조회 조건은 지정할 수 없습니다.")
        # Match the existing service boundary: no personal subscription in institution mode.
        if settings.access_mode != "local_development":
            raise HTTPException(503, detail={"code": "institution_provider_not_configured",
                "message": "기관에서 승인한 조사 모델이 아직 구성되지 않았습니다. 저장된 현황 보고서는 계속 열람할 수 있습니다."})
        try:
            base = store.get(report_id)
            if base is None:
                raise HTTPException(404, detail={"code": "analysis_report_not_found", "message": "조사할 현황 보고서를 찾을 수 없습니다."})
            comparison_ids = body.comparison_report_ids
            if "comparison_report_ids" not in body.model_fields_set and base.get("parent_report_id"):
                comparison_ids = base.get("comparison_context", {}).get("report_ids", [])
            base = _root_report(store, base)
            # A valid regional observation is required; do not research an arbitrary missing scope.
            if not any(item.get("observation") for item in base.get("assessments", [])):
                raise HTTPException(422, detail={"code": "analysis_observation_unavailable",
                    "message": "선택한 범위의 활동 관측이 없어 원인 조사를 시작하지 않았습니다. 지역·월·집단을 확인해 주세요."})
            parent_id = base["report_id"]
            comparison_context = _comparison_context(store, base, comparison_ids)
            research_key = _research_key(comparison_context)
            claim = store.claim_research(parent_id, research_key)
            if claim["status"] == "completed":
                return claim["report"]
            if claim["status"] == "running":
                raise HTTPException(409, detail={"code": "analysis_research_in_progress",
                    "message": "이 보고서의 지역 맥락을 조사하고 있습니다. 잠시 후 다시 확인해 주세요."}, headers={"Retry-After": "10"})
            token = claim["token"]
            try:
                research_input = deepcopy(base)
                research_input["comparison_context"] = deepcopy(comparison_context)
                investigation = research_analysis_report(research_input)
                if investigation.get("prompt_version") != RESEARCH_PROMPT_VERSION:
                    raise ProviderUnavailable("analysis_research_version_mismatch")
                investigation["comparison_context"] = deepcopy(comparison_context)
                report = attach_context_investigation(base, investigation)
                return store.finish_research(parent_id, research_key, token, report)
            except ProviderUnavailable as exc:
                # Codes are server-owned; never log prompts, credentials or raw CLI diagnostics.
                logger.warning("Regional context research unavailable: %s", exc.code)
                missing = exc.code in {"model_key_missing", "model_name_missing", "codex_cli_missing",
                    "codex_subscription_login_required", "codex_cli_version_not_validated", "model_provider_not_configured"}
                raise HTTPException(503, detail={"code": "analysis_research_unavailable",
                    "message": ("지역 맥락을 조사할 모델 연결을 확인해 주세요. " if missing else "지역 맥락 조사를 완료하지 못했습니다. 잠시 후 다시 시도해 주세요. ")
                               + "관측 보고서는 서버에 저장되어 있습니다.", "saved_report_id": parent_id}) from None
            finally:
                store.release_research(parent_id, research_key, token)
        except (sqlite3.Error, OSError) as exc:
            raise failed(exc) from exc

    @router.get("")
    def list_reports(request: Request, limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0),
                     region_code: str | None = Query(default=None, pattern=r"^(11|26|27|28|29|30|31|36|41|43|44|46|47|48|50|51|52)[0-9]{3}$"),
                     dong_code: str | None = Query(default=None, pattern=r"^[0-9]{10}$"),
                     month: str | None = Query(default=None, pattern=r"^[0-9]{4}(0[1-9]|1[0-2])$"),
                     sex_code: Literal["MAN", "WMAN"] | None = None,
                     age_code: Literal["20G", "30G", "40G", "50G", "60GU"] | None = None):
        if set(request.query_params) - {"limit", "offset", "region_code", "dong_code", "month", "sex_code", "age_code"}:
            raise HTTPException(422, "지원하지 않는 보고서 조회 조건입니다.")
        if month is not None and previous_month(month) is None:
            raise HTTPException(422, "조회할 관측월을 확인해 주세요.")
        if dong_code is not None and region_code is not None and not dong_code.startswith(region_code):
            raise HTTPException(422, "행정동과 상위 시군구가 일치하지 않습니다.")
        try:
            return store.list(limit=limit, offset=offset, region_code=region_code, dong_code=dong_code,
                              month=month, sex_code=sex_code, age_code=age_code)
        except sqlite3.Error as exc:
            raise failed(exc) from exc

    @router.get("/{report_id}")
    def get_report(report_id: str, request: Request):
        if request.query_params:
            raise HTTPException(422, "지원하지 않는 보고서 조회 조건입니다.")
        try:
            report = store.get(report_id)
        except sqlite3.Error as exc:
            raise failed(exc) from exc
        if report is None:
            raise HTTPException(404, detail={"code": "analysis_report_not_found", "message": "저장된 분석 보고서가 없습니다."})
        return report

    return router
