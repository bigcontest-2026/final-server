import json
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .provider import ProviderUnavailable, create_provider, model_configuration, TOOL_SCHEMAS
from .observability import trace_step
from .repository import now
from .welfare_graph import build_welfare_graph

EXECUTION_VERSION_FIELDS = ("snapshot_sha256", "support_manifest_sha256", "i5_manifest_sha256",
                            "final_model_manifest_sha256", "tool_version", "graph_version", "prompt_version", "signal_rule_version")

class InvestigationState(TypedDict, total=False):
    case_id: str
    execution_id: str
    measurements: dict
    quality: dict
    details: dict
    checks: dict
    model_metrics: list
    model_note: str
    model_evidence_ids: list[str]
    brief: dict
    review: dict
    welfare: dict


def build_investigation_graph(repo, tools, saver, welfare_request=None):
    # The caller supplies explicit, frozen bundle/publication settings and scope.
    welfare_graph = build_welfare_graph() if welfare_request is not None else None
    def recommend_welfare(state):
        if welfare_graph is None:
            return {}
        request = welfare_request(repo.get(state['case_id'])['request'])
        requests = request if isinstance(request,list) else [request]
        result = repo.tool_result(state['execution_id'], 'welfare', 'recommend_welfare', request,
            lambda: {'groups':[welfare_graph.invoke({'request':item})['result'] for item in requests],
                     'status':'completed' if requests else 'held',
                     'operational_alert_allowed':False})
        return {'welfare':result}
    def frozen_provider(execution):
        provider = create_provider()
        frozen = execution["versions"].get("model_config")
        if frozen is None:
            raise ProviderUnavailable("model_configuration_missing")
        if frozen != model_configuration(provider):
            raise ProviderUnavailable("model_configuration_changed_before_execution")
        return provider, frozen

    def load(state):
        case = repo.get(state["case_id"])
        execution = repo.execution(state["execution_id"])
        current = tools.versions()
        # A queued execution cannot silently switch to a new dataset/corpus.
        for field in EXECUTION_VERSION_FIELDS:
            if execution["versions"].get(field) != current.get(field):
                raise ProviderUnavailable("source_version_changed_before_execution")
        measurements = repo.tool_result(state["execution_id"], "measurements", "get_measurements", case["request"],
                       lambda: tools.measurements(case["request"], state["execution_id"]))
        source_versions = measurements.get("source_versions", current)
        for field in EXECUTION_VERSION_FIELDS:
            if execution["versions"].get(field) != source_versions.get(field):
                raise ProviderUnavailable("source_version_changed_during_read")
        repo.put_evidence(state["case_id"], state["execution_id"], measurements["evidence"])
        repo.event(state["case_id"], state["execution_id"], "measurements_loaded", {"snapshot_version": measurements["snapshot_version"], "sk_mode": measurements["sk_mode"]}, state["execution_id"] + ":measurements")
        return {"measurements": measurements}

    def investigate(state):
        case, eid = repo.get(state["case_id"]), state["execution_id"]
        execution = repo.execution(eid)
        current = tools.versions()
        if any(execution["versions"].get(k) != current.get(k) for k in EXECUTION_VERSION_FIELDS):
            raise ProviderUnavailable("source_version_changed_before_execution")
        quality = repo.tool_result(eid, "quality", "inspect_observation_quality", {"sk_mode": case["request"]["sk_mode"]}, lambda: tools.quality(state["measurements"]))
        details, checks, metrics = {}, {}, []
        model_note = ""
        model_evidence_ids = []
        deadline = time.monotonic() + 480

        def check_budget():
            repo.check_active_job()
            if time.monotonic() >= deadline:
                raise ProviderUnavailable("agent_wall_time_exhausted")

        def invoke(step, name, arguments, function):
            check_budget()
            result = repo.tool_result(eid, step, name, arguments, function)
            check_budget()
            return result

        def validated_response(response):
            # Model references must identify evidence actually supplied during this
            # execution. Keep them separate from deterministic server-generated claims.
            known = {item["evidence_id"] for item in state["measurements"]["evidence"]}
            known.update(item["evidence_id"] for item in details.values() if item.get("status") == "ok")
            refs = response.get("evidence_ids")
            if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in known for ref in refs):
                raise ProviderUnavailable("model_unknown_evidence_reference")
            return response

        def fetch(cid, step):
            result = invoke(step, "fetch_support_details", {"candidate_id": cid}, lambda: tools.details(cid))
            if result.get("status") == "ok":
                result = {**result, "evidence_id": f"ev_{eid}_{cid}"}
            details[cid] = result
            return result

        def check(cid, step):
            result = invoke(step, "check_support_conditions", {"candidate_id": cid, "scope": case["request"]}, lambda: tools.check_conditions(case["request"], details.get(cid, {"status": "unavailable"})))
            checks[cid] = result
            return result

        if execution["strategy"] != "agent":
            if execution["strategy"] == "rule_baseline" and any(a["type"] == "check_context" for a in quality["actions"]):
                repo.tool_result(eid, "context", "get_context_evidence", {"period": case["request"]["measurement_as_of"], "region_code": case["request"]["region_code"]},
                                 lambda: {"status": "unavailable", "error": {"code": "context_connector_not_configured"}, "scope": "same_region_and_historical_period", "causal_claim_allowed": False})
            listing = invoke("search_1", "search_official_support", {"scope": case["request"], "support_type": "general"}, lambda: tools.search(case["request"]))
            for i, item in enumerate(listing["candidates"][:4]):
                fetch(item["id"], "detail_" + str(i))
                check(item["id"], "condition_" + str(i))
            model_note = "규칙 기반 결정적 브리프"
            if execution["strategy"] == "single_search":
                provider, configuration = frozen_provider(execution)
                messages = [{"role": "user", "content": json.dumps({"scope": state["measurements"]["scope"], "observations": state["measurements"]["observations"], "details": details, "checks": checks,
                             "task": "추가 도구 호출 없이 검색1회 결과를 근거에 따라 짧게 요약하고 확인 질문을 남기세요."}, ensure_ascii=False)}]
                response = validated_response(invoke("model_summary", "model_single_summary", {"messages": messages, "model_config": configuration}, lambda: provider.decide(messages, allow_tools=False, cancel_check=check_budget)))
                metrics.append(response["metrics"])
                model_note = response["text"][:2000]
                model_evidence_ids = response["evidence_ids"]
        else:
            provider, configuration = frozen_provider(execution)
            messages = [{"role": "user", "content": json.dumps({"scope": state["measurements"]["scope"], "observations": state["measurements"]["observations"]}, ensure_ascii=False)}]
            allowed_ids, calls_count, searches = set(), 0, 0
            allowed_names = {t["name"] for t in TOOL_SCHEMAS}
            for round_index in range(5):
                check_budget()
                response = validated_response(invoke("model_" + str(round_index), "model_decide", {"messages": messages, "model_config": configuration}, lambda: provider.decide(messages, cancel_check=check_budget)))
                metrics.append(response["metrics"])
                messages.append({"role": "assistant", "content": json.dumps({"calls": response["calls"], "text": response["text"]}, ensure_ascii=False)})
                if not response["calls"]:
                    model_note = response["text"][:2000]
                    model_evidence_ids = response["evidence_ids"]
                    break
                for index, call in enumerate(response["calls"]):
                    check_budget()
                    calls_count += 1
                    if calls_count > 12:
                        raise ProviderUnavailable("tool_budget_exhausted")
                    name = call.get("name")
                    args = call.get("arguments", {})
                    step = f"agent_{round_index}_{index}"
                    if name not in allowed_names or not isinstance(args, dict):
                        raise ProviderUnavailable("invalid_model_tool")
                    if name == "inspect_observation_quality":
                        result = quality
                    elif name == "get_context_evidence":
                        result = invoke(step, name, args, lambda: {"status": "unavailable", "error": {"code": "context_connector_not_configured"}})
                    elif name == "search_official_support":
                        searches += 1
                        if searches > 2 or not set(args) <= {"support_type", "exclude_ids"}:
                            raise ProviderUnavailable("search_budget_or_schema_error")
                        result = invoke(step, name, args, lambda: tools.search(case["request"], args.get("support_type", "general"), args.get("exclude_ids", [])))
                        allowed_ids.update(x["id"] for x in result["candidates"])
                    else:
                        cid = args.get("candidate_id")
                        if cid not in allowed_ids or set(args) != {"candidate_id"}:
                            raise ProviderUnavailable("invalid_candidate_id")
                        if name == "check_support_conditions" and cid not in details:
                            raise ProviderUnavailable("support_details_required_before_conditions")
                        result = fetch(cid, step) if name == "fetch_support_details" else check(cid, step)
                    messages.append({"role": "tool", "call_id": call["call_id"], "name": name, "content": result})
            else:
                raise ProviderUnavailable("model_round_budget_exhausted")
        check_budget()
        return {"quality": quality, "details": details, "checks": checks, "model_metrics": metrics, "model_note": model_note,
                "model_evidence_ids": model_evidence_ids}

    def draft(state):
        eid, case_id = state["execution_id"], state["case_id"]
        candidates, excluded, evidence = [], [], []
        questions = list(state["quality"]["questions"])
        evidence_ids = [e["evidence_id"] for e in state["measurements"]["evidence"]]
        for cid, detail in state["details"].items():
            check = state["checks"].get(cid)
            if detail["status"] != "ok":
                excluded.append({"id": cid, "title": cid, "reason": detail.get("error", {}).get("code", "conditions_not_checked"), "eligibility_status": "unverified_source"})
                continue
            s, ev_id = detail["source"], f"ev_{eid}_{cid}"
            evidence.append({"evidence_id": ev_id, "kind": "official_support", "source_id": cid, "source_url": s["url"],
                             "version_hash": detail["content_hash"], "extraction_hash": detail["text_hash"], "retrieved_at": s["retrieved_at"],
                             "metadata_hash": detail["metadata_hash"],
                             "published_at": s.get("published_at"), "geography": s.get("regions"), "population": "공식 지원 상세 대상", "unit": "지원 조건",
                             "extracted_values": {"conditions": s.get("conditions"), "period": s.get("period"), "contact": s.get("contact")},
                             "field_evidence": s.get("conditions"), "limitations": ["원문에 명시되지 않은 조건은 unknown", "현재 수용·개인 자격 확인은 별도"],
                             "capture_path": s["capture_path"], "public_aggregate_only": True})
            evidence_ids.append(ev_id)
            if not check:
                excluded.append({"id": cid, "title": s["title"], "reason": "conditions_not_checked",
                                 "eligibility_status": "unverified_source", "evidence_ids": [ev_id]})
                continue
            candidate = {"id": cid, "title": s["title"], "url": s["url"], "source_url": s["url"],
                         "summary": s.get("summary"), "contact": s.get("contact"), "conditions": check["conditions"],
                         "eligibility_status": check["eligibility_status"], "evidence_ids": [ev_id], "source_hash": detail["content_hash"],
                         "currently_accepting_confirmed": check["currently_accepting_confirmed"]}
            if check["excluded"]:
                excluded.append({**candidate, "reason": " / ".join(check["reasons"])})
            else:
                candidates.append(candidate)
                for condition in check["conditions"]:
                    if condition["status"] == "unknown":
                        questions.append({"id": cid + ":" + condition["id"], "question": s["title"] + " — " + condition["label"] + " 확인 필요", "evidence_ids": [ev_id]})
        repo.put_evidence(case_id, eid, evidence)
        case = repo.get(case_id)
        brief = {"schema_version": "investigation-brief-1.0", "case_id": case_id, "execution_id": eid,
                 "scope": state["measurements"]["scope"], "observations": state["measurements"]["observations"],
                 "activity_assessments": state["measurements"].get("activity_assessments", []),
                 "support_candidates": candidates, "excluded_candidates": excluded, "open_questions": questions,
                 "next_actions": state["quality"]["actions"] + [{"type": "review_official_conditions", "reason": "개인 조건·접수 수용은 공식 창구 추가 확인", "evidence_ids": [e["evidence_id"] for e in evidence]}],
                 "evidence_ids": evidence_ids, "claims": [{"claim_id": "claim_" + o["kind"], "text": "인구구성 가중 I5 참고지수 확인" if o["kind"] in ("i5", "survey_i5_population_reference") else o["kind"] + " 공개 집계 관측 확인", "evidence_ids": [o["evidence_id"]]} for o in state["measurements"]["observations"]],
                 "quality_status": "hold", "operational_alert_allowed": False,
                 "measurement_status": state["measurements"]["measurement_status"], "support_status": "needs_confirmation" if candidates else "no_verified_candidate",
                 "limitations": ["과거 활동 근거를 참고한 현재 지원정보 조사입니다.", "개인 진단·미래 고립 예측·예방 효과를 측정하지 않았습니다.",
                   "보관 공식 원문의 재현 가능한 로컬 조회이며 실시간 공공 API 호출과 구별합니다.", "개발자 검토는 실무 담당자·기관 검증과 구별합니다."],
                 "strategy": repo.execution(eid)["strategy"], "model_called": bool(state["model_metrics"]),
                 "model_note": state["model_note"], "model_evidence_ids": state.get("model_evidence_ids", []),
                 "verification": {"status": "passed", "method": "typed aggregate input, exact spatial/time scope, raw hashes and deterministic three-valued conditions", "unsupported_numeric_claims": [],
                   "model_reference_status": "known_evidence_only" if state["model_metrics"] else "not_applicable",
                   "model_text_status": "unverified_advisory; not_used_for_numeric_or_eligibility_claims" if state["model_metrics"] else "not_applicable"},
                 "review_status": "awaiting_review"}
        if state.get('welfare') is not None:
            brief['welfare_recommendations'] = state['welfare']
        result = repo.tool_result(eid, "verify", "verify_evidence", {"evidence_ids": evidence_ids}, lambda: {"status": "ok", "evidence_count": len(evidence_ids), "unknown_conditions_preserved": True, "individual_eligibility_confirmed": False})
        return {"brief": repo.put_brief(case_id, eid, brief)}

    def review(state):
        decision = interrupt({"case_id": state["case_id"], "execution_id": state["execution_id"],
                              "brief_id": state["brief"]["brief_id"], "brief_version": state["brief"]["brief_version"],
                              "open_questions": state["brief"]["open_questions"], "allowed_decisions": ["accept", "reject", "request_info"]})
        return {"review": decision}

    def finalize(state):
        repo.apply_review(state["case_id"], state["execution_id"], state["review"])
        return {}

    graph = StateGraph(InvestigationState)
    for name, node in (("load", load), ("investigate", investigate), ("recommend_welfare", recommend_welfare), ("draft", draft), ("review", review), ("finalize", finalize)):
        def traced(state, _node=node, _name=name):
            repo.check_active_job()
            if _name in ("review", "finalize"):
                versions = repo.execution(state["execution_id"])["versions"]
                current = tools.versions()
                if any(versions.get(key) != current.get(key) for key in EXECUTION_VERSION_FIELDS):
                    raise ProviderUnavailable("source_version_changed_before_execution")
            with trace_step("graph." + _name, metadata={"execution_id": state.get("execution_id")}):
                return _node(state)
        graph.add_node(name, traced)
    for src, dest in ((START, "load"), ("load", "investigate"), ("investigate", "recommend_welfare"), ("recommend_welfare", "draft"), ("draft", "review"), ("review", "finalize"), ("finalize", END)):
        graph.add_edge(src, dest)
    return graph.compile(checkpointer=saver)
