from fastapi import APIRouter, HTTPException, Query, Request

from donghaeng_agent.contracts import FollowupCommand, InvestigationRequest, RecheckCommand, RetryCommand, ReviewCommand, RunCommand
from donghaeng_agent.provider import create_provider, model_configuration
from donghaeng_agent.repository import RepositoryError
from donghaeng_agent.observability import observability_status


def investigation_router(repository, tools, worker):
    router = APIRouter(prefix="/api/investigations", tags=["investigations"])

    def perform(fn):
        try:
            return fn()
        except RepositoryError as exc:
            raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message}) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail={"code": "source_unavailable", "message": "검증 가능한 연구·공식 원문 자료를 읽을 수 없습니다."}) from exc

    @router.get("/readiness")
    def readiness(request: Request):
        institution = request.state.principal.mode == "institution"
        return {"schema_version": "investigation-1.0", "storage": "sqlite_business_and_separate_langgraph_checkpoint",
                "worker": "single_local_durable_worker", "model": (
                    {"provider": None, "ready": False, "code": "institution_provider_not_configured"}
                    if institution else create_provider().readiness()),
                "measurement_ready": tools.research_path.is_file(), "official_corpus_ready": tools.support_path.is_file(),
                "i5_bundle_configured": tools.i5_bundle is not None,
                "activity_bundle_configured": tools.final_model_bundle is not None,
                "access": request.app.state.access_controller.status(),
                "observability": observability_status(),
                "official_live_api": "not_connected", "external_actions": "disabled"}

    @router.get("/evidence/activity")
    def activity_evidence(request: Request,
                          region_code: str = Query(pattern=r"^(11|26|27|28|29|30|31|36|41|43|44|46|47|48|50|51|52)[0-9]{3}$"),
                          month: str = Query(pattern=r"^[0-9]{4}(0[1-9]|1[0-2])$"),
                          sex_code: str = Query(pattern=r"^(MAN|WMAN)$"),
                          age_code: str = Query(pattern=r"^(20G|30G|40G|50G|60GU)$")):
        if set(request.query_params) - {"region_code", "month", "sex_code", "age_code"}:
            raise HTTPException(status_code=422, detail="시군구 활동 근거에 지원하지 않는 조회 조건입니다.")
        return perform(lambda: tools.get_final_behavior_evidence(region_code=region_code, month=month,
                                                                 sex_code=sex_code, age_code=age_code))

    @router.get("/catalog/search")
    def search_catalog(query: str = Query(default="", max_length=200), limit: int = Query(default=20, ge=1, le=100)):
        return perform(lambda: tools.catalog_search(query, limit))

    @router.post("", status_code=201)
    def create(request: InvestigationRequest):
        data = request.model_dump(mode="json")
        def action():
            replay = repository.replay_command("create", data)
            if replay:
                return repository.get(replay["case_id"])
            tools.resolve_region(data)
            response = repository.create(data)
            return repository.get(response["case_id"])
        return perform(action)

    @router.get("")
    def list_cases():
        return {"schema_version": "investigation-1.0", "cases": repository.list()}

    @router.get("/{case_id}")
    def get_case(case_id: str):
        return perform(lambda: repository.get(case_id))

    @router.post("/{case_id}/runs", status_code=202)
    def run(case_id: str, command: RunCommand, request: Request):
        data = command.model_dump(mode="json")
        def action():
            replay = repository.replay_command(case_id + ":run", data)
            if replay:
                return replay
            if request.state.principal.mode == "institution":
                if command.strategy != "rule_baseline":
                    raise RepositoryError("institution_provider_not_configured", "기관 승인 모델 제공자가 아직 구성되지 않았습니다.", 503)
                return repository.enqueue(case_id, data, {**tools.versions(), "model": None, "model_config": None,
                                                         "execution_scope": "institution_rule_baseline_only"})
            provider = create_provider()
            return repository.enqueue(case_id, data, {**tools.versions(), "model": provider.model,
                                                     "model_config": model_configuration(provider)})
        return perform(action)

    @router.post("/{case_id}/reviews", status_code=202)
    def review(case_id: str, command: ReviewCommand, request: Request):
        def action():
            data = command.model_dump(mode="json")
            principal = request.state.principal
            data["reviewer_type"] = "user" if principal.mode == "institution" else "developer_verification"
            data["actor"] = principal.audit_actor()
            replay = repository.replay_command(case_id + ":review", data)
            if replay:
                return replay
            if command.decision == "accept":
                changes = tools.recheck(repository.get(case_id))
                if any(x["status"] != "same" for x in changes):
                    raise RepositoryError("stale_support", "보관 공식 원문·추출문·조건이 달라졌습니다. 재확인 후 새 브리프를 검토하세요.")
            return repository.review(case_id, data)
        return perform(action)

    @router.post("/{case_id}/resume", status_code=202)
    def resume_failed(case_id: str, command: RetryCommand):
        return perform(lambda: repository.retry(case_id, command.model_dump(mode="json")))

    @router.post("/{case_id}/recheck")
    def recheck(case_id: str, command: RecheckCommand):
        return perform(lambda: repository.record_recheck(case_id, command.model_dump(mode="json"), tools.recheck(repository.get(case_id))))

    @router.post("/{case_id}/followups")
    def followup(case_id: str, command: FollowupCommand, request: Request):
        data = {**command.model_dump(mode="json"), "actor": request.state.principal.audit_actor()}
        return perform(lambda: repository.followup(case_id, data))

    return router
