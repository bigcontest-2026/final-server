"""Versioned response drafts, reports and explicit read-only model assistance."""
import ipaddress
import re
import sqlite3
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from donghaeng_agent.analysis_report_store import AnalysisReportStore
from donghaeng_agent.signal_rules import previous_month
from donghaeng_agent.response_workspace import ResponseWorkspaceStore, WorkspaceConflict, WorkspaceInputError
from donghaeng_agent.planning_details import PlanningEntry, PLANNING_FIELDS
from donghaeng_agent.response_assistance import assist_response
from donghaeng_agent.provider import ProviderUnavailable
from .security import _bind_database


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProgramReference(StrictModel):
    program_version_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    url: str | None = Field(default=None, max_length=2000)
    publisher: str | None = Field(default=None, max_length=200)

    @field_validator("url")
    @classmethod
    def safe_url(cls, value):
        if value is None:
            return None
        try:
            url = urlsplit(value)
            host = url.hostname or ""
            if (url.scheme not in ("https", "http") or not host or url.username or url.password or
                    re.search(r"[\x00-\x20<>\\\"']", value) or host.lower() in ("localhost", "localhost.localdomain") or
                    url.port not in (None, 80, 443)):
                raise ValueError("unsafe_reference_url")
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                raise ValueError("unsafe_reference_url")
        except (ValueError, UnicodeError):
            raise ValueError("참고 사업의 공개 원문 링크를 확인해 주세요.") from None
        return value

    @field_validator("program_version_id", "title")
    @classmethod
    def meaningful(cls, value):
        if not value.strip():
            raise ValueError("내용을 입력해 주세요.")
        return value.strip()


class ReviewInput(StrictModel):
    status: Literal["unconfirmed", "confirmed", "not_applicable"] = "unconfirmed"
    text: str = Field(default="", max_length=2000)
    evidence: str = Field(default="", max_length=2000)


class PolicyReviewInput(StrictModel):
    eligibility: ReviewInput = Field(default_factory=ReviewInput)
    support_content: ReviewInput = Field(default_factory=ReviewInput)
    delivery: ReviewInput = Field(default_factory=ReviewInput)
    finance_and_cost: ReviewInput = Field(default_factory=ReviewInput)


class SupportNeedInput(StrictModel):
    category_code: Literal["social_connection", "isolation_outreach", "income_support", "daily_living",
                           "employment", "mental_health", "health_care", "housing", "family_support"]
    status: Literal["unconfirmed", "confirmed"] = "unconfirmed"
    text: str = Field(default="", max_length=2000)
    evidence: str = Field(default="", max_length=2000)


class PlanInput(StrictModel):
    plan_id: str | None = Field(default=None, pattern=r"^plan_[0-9a-f]{32}$")
    kind: Literal["existing_program", "program_improvement", "new_program"]
    title: str = Field(min_length=1, max_length=200)
    target: str = Field(default="", max_length=2000)
    rationale: str = Field(default="", max_length=4000)
    content: str = Field(default="", max_length=8000)
    checks: str = Field(default="", max_length=4000)
    selected: bool = False
    feedback: str = Field(default="", max_length=4000)
    program_reference: ProgramReference | None = None
    policy_review: PolicyReviewInput = Field(default_factory=PolicyReviewInput)
    support_needs: list[SupportNeedInput] = Field(default_factory=list, max_length=9)
    planning_details: dict[str, PlanningEntry] = Field(default_factory=dict, max_length=16)

    @field_validator("planning_details")
    @classmethod
    def known_planning_fields(cls, value):
        if set(value) - set(PLANNING_FIELDS):
            raise ValueError("알 수 없는 계획 항목입니다.")
        return value

    @field_validator("title")
    @classmethod
    def title_content(cls, value):
        if not value.strip():
            raise ValueError("대응안 이름을 입력해 주세요.")
        return value.strip()


class WorkspaceSaveRequest(StrictModel):
    expected_version: int = Field(ge=0)
    # The store retains the normal 20-plan limit while permitting edits to a
    # larger set preserved from multiple legacy workspaces in one family.
    plans: list[PlanInput] = Field(default_factory=list)


class ResponseAssistanceRequest(StrictModel):
    mode: Literal["draft", "revise", "review"]
    kind: Literal["existing_program", "program_improvement", "new_program"]
    instruction: str = Field(min_length=1, max_length=2000)
    current_plan: PlanInput | None = None

    @field_validator("instruction")
    @classmethod
    def meaningful_instruction(cls, value):
        if not value.strip():
            raise ValueError("헤르미에게 요청할 내용을 입력해 주세요.")
        return value.strip()


class SelectedPlanVersion(StrictModel):
    plan_id: str = Field(pattern=r"^plan_[0-9a-f]{32}$")
    version: int = Field(ge=1)


class ResponseReportRequest(StrictModel):
    expected_workspace_version: int = Field(ge=1)
    selected_plan_versions: list[SelectedPlanVersion] = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def valid_title(cls, value):
        if value is not None and not value.strip():
            raise ValueError("보고서 제목을 입력해 주세요.")
        return value.strip() if value else None


def response_router(settings):
    router = APIRouter(tags=["response-workspace"])
    analysis_store = AnalysisReportStore(settings.investigation_db_path.with_name("analysis_reports.sqlite3"))
    store = ResponseWorkspaceStore(settings.investigation_db_path.with_name("response_workspaces.sqlite3"))
    with analysis_store.connection(write=True) as db:
        _bind_database(db, settings, ("analysis_reports", "analysis_report_research", "analysis_report_summaries"))
    with store.connection(write=True) as db:
        _bind_database(db, settings, ("response_workspaces", "response_plan_revisions", "response_reports", "response_workspace_members"))

    def parent(analysis_report_id):
        report = analysis_store.get(analysis_report_id)
        if report is None:
            raise HTTPException(404, detail={"code": "analysis_report_not_found", "message": "연결할 현황 분석 보고서를 찾을 수 없습니다. 저장된 보고서에서 다시 선택해 주세요."})
        return report

    def family(analysis_report_id):
        resolved = analysis_store.family(analysis_report_id)
        if resolved is None:
            parent(analysis_report_id)
        return resolved

    def query(request, allowed=()):
        if set(request.query_params) - set(allowed):
            raise HTTPException(422, detail={"code": "unsupported_response_query", "message": "지원하지 않는 조회 조건입니다."})

    def failure(exc):
        if isinstance(exc, WorkspaceConflict):
            return HTTPException(409, detail={"code": exc.code, "current_version": exc.version,
                "message": "다른 화면에서 대응안이 변경되었습니다. 최신 내용을 불러온 뒤 다시 저장해 주세요. 입력한 내용은 화면에 남겨 두었습니다."})
        if isinstance(exc, WorkspaceInputError):
            messages = {"response_plan_omission": "저장된 대응안이 빠졌습니다. 최신 대응안을 불러온 뒤 다시 저장해 주세요.",
                "response_plan_not_in_workspace": "다른 현황 분석의 대응안이 포함되었습니다. 현재 분석의 대응안만 선택해 주세요.",
                "duplicate_response_plan": "같은 대응안이 중복되었습니다. 내용을 확인해 주세요.",
                "response_plan_limit": "대응안은 20개까지 추가할 수 있습니다. 기존에 보존된 대응안은 계속 수정할 수 있습니다.",
                "response_selection_empty": "보고서에 담을 대응안을 하나 이상 선택해 주세요.",
                "response_plan_incomplete": "선택한 대응안의 이름·대상·주요 내용을 입력한 뒤 보고서를 만들어 주세요."}
            messages.update(review_confirmation_evidence_required="사용자 확인으로 기록할 항목의 내용과 직접 확인한 근거를 입력해 주세요.",
                invalid_policy_review="네 가지 검토 항목의 상태와 내용을 확인해 주세요.", invalid_support_needs="지원 필요의 분야·상태 또는 중복 항목을 확인해 주세요.")
            return HTTPException(422, detail={"code": exc.code, "message": messages.get(exc.code, "대응안 입력값을 확인해 주세요.")})
        return HTTPException(503, detail={"code": "response_storage_unavailable", "message": "대응안을 저장하지 못했습니다. 입력한 내용을 유지한 채 잠시 후 다시 시도해 주세요."})

    @router.get("/api/model/analysis-reports/{analysis_report_id}/response-workspace")
    def get_workspace(analysis_report_id: str, request: Request):
        query(request)
        try:
            return store.get(analysis_report_id, **family(analysis_report_id))
        except (sqlite3.Error, OSError) as exc:
            raise failure(exc) from None

    @router.put("/api/model/analysis-reports/{analysis_report_id}/response-workspace")
    def save_workspace(analysis_report_id: str, body: WorkspaceSaveRequest, request: Request):
        query(request)
        try:
            linked = family(analysis_report_id)
            plans = []
            for plan in body.plans:
                data = plan.model_dump()
                for field in ("policy_review", "support_needs", "planning_details"):
                    if field not in plan.model_fields_set:
                        data.pop(field)
                    elif field == "policy_review":
                        data[field] = plan.policy_review.model_dump(exclude_unset=True)
                    elif field == "support_needs":
                        data[field] = [item.model_dump(exclude_unset=True) for item in plan.support_needs]
                    else:
                        data[field] = {key: item.model_dump() for key, item in plan.planning_details.items()}
                plans.append(data)
            return store.save(analysis_report_id, body.expected_version, plans, **linked)
        except (WorkspaceConflict, WorkspaceInputError, sqlite3.Error, OSError) as exc:
            raise failure(exc) from None

    @router.post("/api/model/analysis-reports/{analysis_report_id}/response-workspace/assist")
    def assistance(analysis_report_id: str, body: ResponseAssistanceRequest, request: Request):
        query(request)
        if settings.access_mode != "local_development":
            raise HTTPException(503, detail={"code": "institution_provider_not_configured",
                "message": "기관에서 승인한 도움 모델이 아직 구성되지 않았습니다."})
        if body.mode in ("revise", "review") and body.current_plan is None:
            raise HTTPException(422, detail={"code": "response_assistance_plan_required",
                "message": "수정하거나 검토할 대응안을 먼저 선택해 주세요."})
        if body.current_plan is not None and body.current_plan.kind != body.kind:
            raise HTTPException(422, detail={"code": "response_assistance_kind_mismatch",
                "message": "선택한 대응안 유형을 다시 확인해 주세요."})
        try:
            analysis = parent(analysis_report_id)
            return assist_response(analysis, mode=body.mode, kind=body.kind,
                instruction=body.instruction,
                current_plan=body.current_plan.model_dump() if body.current_plan else None)
        except HTTPException:
            raise
        except WorkspaceInputError as exc:
            raise failure(exc) from None
        except (sqlite3.Error, OSError) as exc:
            raise failure(exc) from None
        except ProviderUnavailable as exc:
            if exc.code == "response_assistance_unverified_program_claim":
                raise HTTPException(503, detail={"code": exc.code,
                    "message": "사업 금액·일정·운영 사실의 근거를 확인하지 못해 제안을 보류했습니다. 미정 항목으로 바꾸어 다시 요청해 주세요."}) from None
            raise HTTPException(503, detail={"code": "response_assistance_unavailable",
                "message": "헤르미가 제안을 만들지 못했습니다. 입력한 내용은 유지됩니다. 다시 시도해 주세요."}) from None
        except Exception:
            # Provider diagnostics can contain credentials or the user draft.
            # Never log or return raw exceptions, prompts or connection settings.
            raise HTTPException(503, detail={"code": "response_assistance_unavailable",
                "message": "헤르미가 제안을 만들지 못했습니다. 입력한 내용은 유지됩니다. 다시 시도해 주세요."}) from None

    @router.post("/api/model/analysis-reports/{analysis_report_id}/response-reports")
    def create_report(analysis_report_id: str, body: ResponseReportRequest, request: Request):
        query(request)
        try:
            analysis = parent(analysis_report_id)
            return store.create_report(analysis, body.expected_workspace_version,
                [plan.model_dump() for plan in body.selected_plan_versions], body.title, **family(analysis_report_id))
        except (WorkspaceConflict, WorkspaceInputError, sqlite3.Error, OSError) as exc:
            raise failure(exc) from None

    @router.get("/api/model/response-reports")
    def list_reports(request: Request, analysis_report_id: str | None = None,
                     region_code: str | None = Query(default=None, pattern=r"^(11|26|27|28|29|30|31|36|41|43|44|46|47|48|50|51|52)[0-9]{3}$"),
                     dong_code: str | None = Query(default=None, pattern=r"^[0-9]{10}$"),
                     month: str | None = Query(default=None, pattern=r"^[0-9]{4}(0[1-9]|1[0-2])$"),
                     sex_code: Literal["MAN", "WMAN"] | None = None,
                     age_code: Literal["20G", "30G", "40G", "50G", "60GU"] | None = None,
                     limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)):
        query(request, ("analysis_report_id", "region_code", "dong_code", "month", "sex_code", "age_code", "limit", "offset"))
        if month is not None and previous_month(month) is None:
            raise HTTPException(422, detail={"code": "invalid_response_month", "message": "조회할 관측월을 확인해 주세요."})
        if dong_code is not None and region_code is not None and not dong_code.startswith(region_code):
            raise HTTPException(422, detail={"code": "response_dong_parent_mismatch", "message": "행정동과 상위 시군구가 일치하지 않습니다."})
        try:
            linked = family(analysis_report_id) if analysis_report_id else {}
            filters = {key: value for key, value in (("region_code", region_code), ("dong_code", dong_code), ("month", month),
                       ("sex_code", sex_code), ("age_code", age_code)) if value is not None}
            return store.list_reports(analysis_report_id=analysis_report_id, scope_filters=filters, limit=limit, offset=offset, **linked)
        except (WorkspaceInputError, sqlite3.Error, OSError) as exc:
            raise failure(exc) from None

    @router.get("/api/model/response-reports/{report_id}")
    def get_report(report_id: str, request: Request):
        query(request)
        try:
            report = store.get_report(report_id)
        except (sqlite3.Error, OSError) as exc:
            raise failure(exc) from None
        if report is None:
            raise HTTPException(404, detail={"code": "response_report_not_found", "message": "저장된 예방 대응 보고서를 찾을 수 없습니다."})
        return report

    return router
