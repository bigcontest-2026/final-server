from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from .config import Settings
from .data import load_dataset
from donghaeng_agent.mock_graph import execute_run
from donghaeng_agent.mock_contracts import Dataset, RunRequest, RunResult
from donghaeng_model.snapshot import ResearchSnapshot, load_research_snapshot
from .store import RunStore
from .investigation_api import investigation_router
from .final_model import FinalModel, ModelUnavailable
from .security import AccessController, bind_investigation_tenant, install_security
from donghaeng_agent.repository import InvestigationRepository
from donghaeng_agent.tools import InvestigationTools
from donghaeng_agent.worker import InvestigationWorker
from donghaeng_agent.observability import init_observability, flush_observability, shutdown_observability


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    access = AccessController(settings)
    repository = InvestigationRepository(settings.investigation_db_path)
    bind_investigation_tenant(repository, settings)
    # Use the same resolved published activity bundle as the model HTTP API.
    # No activity bundle is valid development state and never creates fake rows.
    try:
        activity_bundle = FinalModel(settings).bundle_path("activity")
    except ModelUnavailable:
        activity_bundle = None
    investigation_tools = InvestigationTools(settings.research_path, settings.support_manifest_path,
                                             resource_root=settings.agent_resource_root,
                                             capture_root=settings.support_capture_root,
                                             i5_bundle=settings.i5_bundle_path,
                                             final_model_bundle=activity_bundle)
    def welfare_request(scope):
        # Resolve the parent using the published I5 row, never a code substring.
        month = scope['measurement_as_of'].replace('-', '')
        rows = investigation_tools.get_survey_area_isolation_index(dong_code=scope['region_code'],month=month,limit=2)['records']
        if len(rows) != 1 or activity_bundle is None:
            return []
        return [dict(i5_bundle=str(settings.i5_bundle_path),activity_bundle=str(activity_bundle),
            db_path=str(settings.agent_resource_root/'runtime/welfare_demo/welfare_erd_v2.sqlite'),
            publication_id='erd-demo-v2',region_code=rows[0]['region_code'],month=month,
            sex_code=sex,age_code={'20대':'20G','30대':'30G'}[scope['age_group']],
            as_of=str(scope['support_as_of']),research_context=True,allow_synthetic=True)
            for sex in ('MAN','WMAN')]
    worker = InvestigationWorker(repository, investigation_tools, settings.investigation_checkpoint_path,
        welfare_request=welfare_request if settings.access_mode=='local_development' and
        (settings.agent_resource_root/'runtime/welfare_demo/welfare_erd_v2.sqlite').exists() else None)

    @asynccontextmanager
    async def lifespan(application):
        init_observability()
        try:
            if settings.mode == "research" and settings.investigation_worker_enabled:
                worker.start()
            yield
        finally:
            try:
                worker.close()
            finally:
                try:
                    flush_observability()
                finally:
                    shutdown_observability()

    application = FastAPI(
        title="동행 관측소 — 행동지수 & 설문 근거 & 유동 관측 API", version="0.3.0",
        description="연구 기반 행정동 행동 대리지수, 자치구 설문 구성지수와 별도 SK 관측. 개인의 고립확률 또는 진단이 아닙니다.", lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware, allow_origins=list(settings.cors_origins),
        allow_credentials=False, allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Authorization"],
    )
    install_security(application, settings, access)
    store = RunStore(settings.max_runs, settings.run_ttl_seconds)
    application.state.run_store = store
    application.state.investigation_repository = repository
    application.state.investigation_worker = worker
    application.state.investigation_tools = investigation_tools
    if settings.mode == "research":
        application.include_router(investigation_router(repository, investigation_tools, worker))

    def require_mock():
        if settings.mode == "live":
            raise HTTPException(status_code=501, detail="live 모드는 실제 모델·정책 시스템 미연결로 사용할 수 없습니다. mock으로 자동 전환하지 않습니다.")
        if settings.mode != "mock":
            raise HTTPException(status_code=409, detail="연구 모드는 고정된 실측 스냅샷입니다. 가상 점수 시나리오 또는 가상 정책을 실행하지 않습니다. 시연은 APP_MODE=mock으로 별도 실행해 주세요.")

    def research_dataset():
        if settings.mode == "live":
            raise HTTPException(status_code=501, detail="live 모드는 실제 모델·정책 시스템 미연결로 사용할 수 없습니다. 연구 스냅샷으로 자동 전환하지 않습니다.")
        try:
            return load_research_snapshot(settings.research_path)
        except (OSError, UnicodeError, ValidationError) as exc:
            raise HTTPException(status_code=503, detail="연구 스냅샷이 없거나 측정 계약이 유효하지 않습니다. 합성 자료로 전환하지 않습니다.") from exc

    def dataset():
        require_mock()
        try:
            return load_dataset(settings.dataset_path)
        except (OSError, UnicodeError, ValidationError) as exc:
            raise HTTPException(status_code=503, detail="공유 시연 데이터가 없거나 계약 형식이 유효하지 않습니다.") from exc

    @application.get("/api/health")
    def health():
        if settings.mode == "research":
            return {"status": "ok", "mode": "research", "graph": "langgraph_investigation_with_review_interrupt", "mock_graph": "disabled", "measurement": "dong behavior index and district-age survey evidence", "snapshot_ready": settings.research_path.is_file()}
        require_mock()
        return {"status": "ok", "mode": "mock", "graph": "langgraph"}

    @application.get("/api/research", response_model=ResearchSnapshot)
    def research():
        return research_dataset()

    @application.get("/api/research/i5")
    def survey_i5(request: Request,
                  region_code: str | None = Query(default=None, pattern=r"^(11|26|27|28|29|30|31|36|41|43|44|46|47|48|50|51|52)[0-9]{3}$"),
                  dong_code: str | None = Query(default=None, pattern=r"^[0-9]{10}$"),
                  month: str | None = Query(default=None, pattern=r"^2025(0[7-9]|1[0-2])$"),
                  offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)):
        if settings.mode != "research":
            raise HTTPException(status_code=409, detail="I5 reference is available in research mode only")
        if set(request.query_params) - {"region_code", "dong_code", "month", "offset", "limit"}:
            raise HTTPException(status_code=422, detail="I5 인구구성 참고지수에 지원하지 않는 조회 조건입니다.")
        if region_code and dong_code and not dong_code.startswith(region_code):
            raise HTTPException(status_code=422, detail="시군구와 행정동 코드가 일치하지 않습니다.")
        try:
            return investigation_tools.get_survey_area_isolation_index(
                region_code=region_code, dong_code=dong_code, month=month, offset=offset, limit=limit,
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="I5 bundle is missing, incomplete or failed integrity checks") from exc

    @application.get("/api/districts", response_model=Dataset)
    def districts():
        return dataset()

    @application.post("/api/runs", response_model=RunResult)
    def run(request: RunRequest):
        data = dataset()
        if not any(d.code == request.district_code for d in data.districts):
            raise HTTPException(status_code=404, detail="요청한 행정동을 찾을 수 없습니다.")
        result = execute_run(data, request)
        store.put(result)
        return result

    @application.get("/api/runs/{run_id}", response_model=RunResult)
    def get_run(run_id: str):
        require_mock()
        result = store.get(run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="실행 기록이 없거나 만료되었습니다. 시연 기록은 서버 재시작 시 사라집니다.")
        return result

    return application

