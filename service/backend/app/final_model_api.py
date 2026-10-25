from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from .config import Settings
from .final_model import FinalModel, ModelUnavailable

PROVINCE_PATTERN = r"^(11|26|27|28|29|30|31|36|41|43|44|46|47|48|50|51|52)$"
Region = Annotated[str, Query(pattern=r"^(11|26|27|28|29|30|31|36|41|43|44|46|47|48|50|51|52)[0-9]{3}$")]
Month = Literal["202507", "202508", "202509", "202510", "202511", "202512"]
Sex = Literal["MAN", "WMAN"]
Age = Literal["20G", "30G", "40G", "50G", "60GU"]


def model_router(settings: Settings) -> APIRouter:
    router = APIRouter()
    model = FinalModel(settings)

    def check_filters(request: Request, allowed: set[str]):
        if set(request.query_params) - allowed:
            raise HTTPException(status_code=422, detail="이 자료에서 지원하지 않는 조회 조건입니다.")

    def call(reader, **filters):
        try:
            return reader(**filters)
        except ModelUnavailable as exc:
            raise HTTPException(status_code=503, detail={
                "code": exc.code, "source": exc.family,
                "message": "조회 자료가 준비되지 않았습니다. 모델 번들 설정과 검증 상태를 확인해 주세요.",
            }) from exc

    @router.get("/api/health")
    @router.get("/api/model/status")
    def status():
        return model.status()

    @router.get("/api/model/regions")
    def regions(request: Request, month: Month | None = None):
        check_filters(request, {"month"})
        return call(model.regions, month=month)

    @router.get("/api/model/behavior")
    def behavior(request: Request, region_code: Region | None = None, month: Month | None = None,
                 sex_code: Sex | None = None, age_code: Age | None = None,
                 candidate_only: bool = False, offset: Annotated[int, Query(ge=0)] = 0,
                 limit: Annotated[int, Query(ge=1, le=100)] = 20):
        check_filters(request, {"region_code", "month", "sex_code", "age_code", "candidate_only", "offset", "limit"})
        return call(model.behavior, region_code=region_code, month=month, sex_code=sex_code,
                    age_code=age_code, candidate_only=candidate_only, offset=offset, limit=limit)

    @router.get("/api/research/i5")
    @router.get("/api/model/survey-area")
    def survey_area(request: Request, level: Literal["sgg", "administrative_dong"] = "sgg", region_code: Region | None = None,
                    month: Month | None = None,
                    province_code: Annotated[str | None, Query(pattern=PROVINCE_PATTERN)] = None,
                    dong_code: Annotated[str | None, Query(pattern=r"^[0-9]{10}$")] = None,
                    offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=500)] = 100):
        check_filters(request, {"level", "region_code", "province_code", "month", "dong_code", "offset", "limit"})
        if dong_code and "level" not in request.query_params:
            level = "administrative_dong"
        if dong_code and (level != "administrative_dong" or region_code and not dong_code.startswith(region_code)):
            raise HTTPException(status_code=422, detail="행정동 코드와 공간 단위·시군구 필터를 확인해 주세요.")
        if province_code and any(code and not code.startswith(province_code) for code in (region_code, dong_code)):
            raise HTTPException(status_code=422, detail="시도와 시군구·읍면동 필터를 확인해 주세요.")
        return call(model.survey_area, level=level, region_code=region_code, month=month,
                    province_code=province_code, dong_code=dong_code, offset=offset, limit=limit)

    return router
