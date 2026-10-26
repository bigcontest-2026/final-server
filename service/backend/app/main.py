"""HTTP entry point. The current model can run without legacy agent packages."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.mode != "final":
        from .legacy_main import create_app as create_legacy_app
        return create_legacy_app(settings)

    from .final_model_api import model_router
    application = FastAPI(
        title="동행 관측소 — I5 참고지수와 활동변화", version="0.4.0",
        description="전국 설문을 지역 인구구성으로 가중한 I5와 시군구 활동변화를 별도 조회합니다.",
    )
    application.add_middleware(
        CORSMiddleware, allow_origins=list(settings.cors_origins),
        allow_credentials=False, allow_methods=["GET", "POST", "PUT"], allow_headers=["Content-Type", "Authorization"],
    )
    from .security import install_security
    install_security(application, settings)
    application.include_router(model_router(settings))
    from .welfare_api import welfare_router
    application.include_router(welfare_router(settings))
    from .welfare_chat_api import welfare_chat_router
    application.include_router(welfare_chat_router(settings))
    from .analysis_api import analysis_router
    application.include_router(analysis_router(settings))
    from .response_api import response_router
    application.include_router(response_router(settings))
    from .report_export_api import report_export_router
    application.include_router(report_export_router(settings))
    return application


app = create_app()
