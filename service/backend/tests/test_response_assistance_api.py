"""Model assistance is read-only, grounded and never replaced by canned drafts."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import response_api
from donghaeng_agent import response_assistance
from donghaeng_agent.provider import ProviderUnavailable
from test_response_api import application, base, plan
from test_analysis_api import report_settings


PATH = "/api/model/analysis-reports"


def model_result(messages, **kwargs):
    request = json.loads(messages[0]["content"])
    result = {"analysis_report_id": request["analysis_report_id"], "scope": request["analysis"]["scope"],
              "mode": request["mode"], "kind": request["kind"],
              "suggestion": None if request["mode"] == "review" else {
                  "title": "검토용 모델 제안", "target": "저장된 분석 범위의 참여 희망 집단",
                  "rationale": "활동 감소 관측을 바탕으로 실제 필요를 추가 확인합니다.",
                  "content": "기존 자원과 비식별 수요 확인 방안을 검토합니다.",
                  "checks": "담당기관과 사업의 대상·내용·전달·비용을 확인합니다."},
              "review_notes": ["사업 원문과 실제 필요의 연결 근거를 추가 확인하세요."]}
    return {"text": json.dumps(result, ensure_ascii=False), "calls": [], "evidence_ids": [request["analysis_report_id"]]}


@pytest.fixture
def setup_assistance(report_settings, monkeypatch):
    provider = Mock()
    provider.decide.side_effect = model_result
    factory = Mock(return_value=provider)
    monkeypatch.setattr(response_assistance, "create_provider", factory)
    with TestClient(application(report_settings)) as client:
        analysis = base(client)
        yield client, analysis, provider, factory


def assist(client, analysis, **changes):
    return client.post(f"{PATH}/{analysis['report_id']}/response-workspace/assist", json={
        "mode": "draft", "kind": "new_program", "instruction": "실행 전에 확인할 내용을 중심으로 초안을 제안해 줘", **changes})


@pytest.mark.parametrize("mode", ["draft", "revise", "review"])
def test_actual_provider_interface_is_called_and_suggestions_never_save_the_workspace(setup_assistance, mode):
    client, analysis, provider, factory = setup_assistance
    before = client.get(f"{PATH}/{analysis['report_id']}").json()
    response = assist(client, analysis, mode=mode, current_plan=plan() if mode != "draft" else None)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "completed" and result["model_called"] is True
    assert result["mode"] == mode and result["kind"] == "new_program"
    assert result["scope"] == analysis["scope"] and result["analysis_report_id"] == analysis["report_id"]
    assert result["suggestion"] is None if mode == "review" else result["suggestion"]["title"] == "검토용 모델 제안"
    assert result["review_notes"]
    assert provider.decide.call_args.kwargs == {"allow_tools": False}
    factory.assert_called_once_with()
    payload = json.loads(provider.decide.call_args.args[0][0]["content"])
    assert payload["analysis"]["assessments"] == analysis["assessments"]
    assert payload["analysis"]["lifecycle_interpretation"] == analysis["lifecycle_interpretation"]
    assert payload["analysis"]["quality_status"] == "hold"
    assert payload["analysis"]["operational_alert_allowed"] is False
    assert payload["analysis_report_id"] in payload["allowed_evidence_ids"]
    assert "provenance" not in payload["analysis"] and "markdown" not in payload["analysis"]
    assert client.get(f"{PATH}/{analysis['report_id']}").json() == before
    workspace = client.get(f"{PATH}/{analysis['report_id']}/response-workspace").json()
    assert workspace["version"] == 0 and workspace["plans"] == [] and workspace["reports"] == []


def test_whole_group_scope_is_sent_without_inventing_a_default_group(setup_assistance):
    client, _, provider, _ = setup_assistance
    analysis = base(client, sex_code=None, age_code=None)
    response = assist(client, analysis)
    assert response.status_code == 200
    payload = json.loads(provider.decide.call_args.args[0][0]["content"])
    assert payload["analysis"]["scope"]["sex_code"] is None
    assert payload["analysis"]["scope"]["age_code"] is None
    assert len(payload["analysis"]["assessments"]) == 10
    assert len(payload["analysis"]["lifecycle_interpretation"]["groups"]) == 10


def test_dong_assistance_keeps_selected_area_and_parent_observation_grain(setup_assistance):
    client, _, provider, _ = setup_assistance
    analysis = base(client, dong_code="1168051000")
    response = assist(client, analysis)
    assert response.status_code == 200, response.text
    assert response.json()["scope"] == analysis["scope"]
    payload = json.loads(provider.decide.call_args.args[0][0]["content"])
    assert payload["analysis"]["region_name"] == "강남구 신사동"
    assert payload["analysis"]["scope"]["dong_code"] == "1168051000"
    assert payload["analysis"]["context"]["i5"]["record"]["area_code"] == "1168051000"
    assert payload["analysis"]["context"]["activity_scope"]["spatial_unit"] == "sgg"
    assert payload["analysis"]["assessments"][0]["scope"]["region_code"] == "11680"
    assert "dong_code" not in payload["analysis"]["assessments"][0]["scope"]


def test_only_editable_draft_fields_enter_the_model_and_user_reference_is_not_authoritative(setup_assistance):
    client, analysis, provider, _ = setup_assistance
    draft = plan(kind="existing_program", feedback="검토 의견", program_reference={
        "program_version_id": "user-selected-id", "title": "사용자가 선택한 사업",
        "url": "https://example.go.kr/unverified", "publisher": "사용자 입력 기관"})
    draft["support_needs"] = [{"category_code": "housing", "status": "confirmed", "text": "확인한 주거 지원 필요",
                               "evidence": "담당자가 확인한 집계 기록"}]
    response = assist(client, analysis, mode="revise", kind="existing_program", current_plan=draft,
                      instruction="연락처 test@example.com api_key=sk-sensitivekey2026 는 사용하지 말아줘")
    assert response.status_code == 200, response.text
    payload = json.loads(provider.decide.call_args.args[0][0]["content"])
    assert set(payload["user_draft"]) == {"kind", "title", "target", "rationale", "content", "checks", "feedback",
                                         "policy_review", "support_needs", "user_selected_reference"}
    assert payload["user_draft"]["support_needs"][0]["origin"] == "user_entered"
    assert payload["user_draft"]["user_selected_reference"]["source_status"] == "user_reference_not_verified_in_this_call"
    assert "unverified" not in json.dumps(payload["user_draft"]["user_selected_reference"].get("url"))
    assert "test@example.com" not in json.dumps(payload) and "sk-sensitivekey2026" not in json.dumps(payload)


@pytest.mark.parametrize("changes", [
    {"mode": "execute"}, {"kind": "unknown"}, {"instruction": " "}, {"instruction": "가" * 2001},
    {"mode": "review"}, {"mode": "revise"}, {"scope": {"region_code": "51110"}},
    {"current_plan": plan(kind="existing_program")}, {"current_plan": {**plan(), "_key": "internal-key"}},
])
def test_invalid_requests_are_rejected_before_the_model(setup_assistance, changes):
    client, analysis, provider, _ = setup_assistance
    assert assist(client, analysis, **changes).status_code == 422
    provider.decide.assert_not_called()


def test_missing_report_and_extra_query_are_rejected_before_the_model(setup_assistance):
    client, analysis, provider, _ = setup_assistance
    body = {"mode": "draft", "kind": "new_program", "instruction": "초안 제안"}
    assert client.post(PATH + "/missing/response-workspace/assist", json=body).status_code == 404
    assert client.post(f"{PATH}/{analysis['report_id']}/response-workspace/assist?scope=other", json=body).status_code == 422
    provider.decide.assert_not_called()


def test_confirmation_records_require_content_and_evidence_before_model_use(setup_assistance):
    client, analysis, provider, _ = setup_assistance
    draft = plan(policy_review={"eligibility": {"status": "confirmed", "text": "", "evidence": ""}})
    response = assist(client, analysis, mode="revise", current_plan=draft)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "review_confirmation_evidence_required"
    provider.decide.assert_not_called()


@pytest.mark.parametrize("change", [
    lambda result: result.update(analysis_report_id="another-report"),
    lambda result: result["scope"].update(month="202511"),
    lambda result: result.update(mode="review", suggestion=None),
    lambda result: result.update(kind="existing_program"),
    lambda result: result["suggestion"].update(support_needs=[{"status": "confirmed"}]),
    lambda result: result["suggestion"].update(title=" "),
    lambda result: result["suggestion"].update(content="https://invented.example/program"),
    lambda result: result["review_notes"].append("문의 02-1234-5678"),
    lambda result: result.update(suggestion=None),
])
def test_wrong_scope_unsupported_confirmation_and_invented_links_never_return_as_suggestions(setup_assistance, change):
    client, analysis, provider, _ = setup_assistance
    def invalid(messages, **kwargs):
        response = model_result(messages, **kwargs)
        result = json.loads(response["text"])
        change(result)
        response["text"] = json.dumps(result, ensure_ascii=False)
        return response
    provider.decide.side_effect = invalid
    response = assist(client, analysis)
    assert response.status_code == 503 and response.json()["detail"]["code"] == "response_assistance_unavailable"
    assert "suggestion" not in response.json()


@pytest.mark.parametrize("bad", [
    {"text": "초안입니다.", "calls": [], "evidence_ids": []},
    {"text": "{}", "calls": [{"name": "write_file"}], "evidence_ids": []},
    {"text": "{}", "calls": [], "evidence_ids": ["invented-source"]},
    {"text": "가" * 8001, "calls": [], "evidence_ids": []},
])
def test_model_failure_has_no_canned_fallback_and_does_not_echo_internal_details(setup_assistance, bad):
    client, analysis, provider, _ = setup_assistance
    provider.decide.side_effect = None
    provider.decide.return_value = deepcopy(bad)
    response = assist(client, analysis)
    assert response.status_code == 503
    assert "model_called" not in response.json()
    assert "다시 시도" in response.json()["detail"]["message"]


def test_provider_errors_do_not_expose_secrets_or_create_a_plan(setup_assistance):
    client, analysis, provider, _ = setup_assistance
    provider.decide.side_effect = RuntimeError("postgresql://user:private-pass@example.test private provider log")
    response = assist(client, analysis)
    assert response.status_code == 503
    assert "private-pass" not in response.text and "postgresql" not in response.text
    assert client.get(f"{PATH}/{analysis['report_id']}/response-workspace").json()["plans"] == []


@pytest.mark.parametrize("claim", [
    "춘천시는 춘천청년활동지원금을 운영 중이며 신청자에게 매월 20만원을 지급합니다. 11월 1일부터 신청받습니다.",
    "신청자에게 매월 20만원을 지급합니다.",
    "수당은 월 이십만원입니다.",
    "11월 1일부터 신청받습니다.",
    "모집은 2026-11-01에 시작합니다.",
    "등록금의 50%를 지원합니다.",
    "춘천청년활동지원금 사업은 현재 운영 중입니다.",
    "춘천시는 춘천청년활동지원금을 운영합니다.",
    "춘천청년활동수당 제도가 있습니다.",
])
def test_new_program_amount_dates_and_current_operation_claims_are_held_with_an_explicit_reason(setup_assistance, claim):
    client, analysis, provider, _ = setup_assistance
    def invented(messages, **kwargs):
        response = model_result(messages, **kwargs)
        result = json.loads(response["text"])
        result["suggestion"].update(title="춘천청년활동지원금 활용", content=claim)
        response["text"] = json.dumps(result, ensure_ascii=False)
        return response
    provider.decide.side_effect = invented
    response = assist(client, analysis)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "response_assistance_unverified_program_claim"
    assert "제안을 보류" in response.json()["detail"]["message"]
    assert client.get(f"{PATH}/{analysis['report_id']}/response-workspace").json()["plans"] == []


@pytest.mark.parametrize("proposal", [
    "월 20만원을 지급하는 방안을 제안합니다. 실제 금액과 재원은 미정입니다.",
    "11월 1일 모집 시작은 가정이며 담당기관과 일정을 협의합니다.",
    "기존 사업이 월 20만원을 지급하는지 원문에서 확인해야 합니다.",
    "제안하는 관계 모임은 주 1회 운영하며 구체적인 일정은 미정입니다.",
    "2025년 10월 통신 -17.41%, 카드 -5.21%의 활동 변화가 있었으며 실제 지원 필요는 별도 확인합니다.",
])
def test_explicit_planning_and_verification_language_is_not_blocked_as_an_existing_program_fact(setup_assistance, proposal):
    client, analysis, provider, _ = setup_assistance
    def proposed(messages, **kwargs):
        response = model_result(messages, **kwargs)
        result = json.loads(response["text"])
        result["suggestion"]["content"] = proposal
        response["text"] = json.dumps(result, ensure_ascii=False)
        return response
    provider.decide.side_effect = proposed
    response = assist(client, analysis)
    assert response.status_code == 200, response.text
    assert response.json()["suggestion"]["content"] == proposal


def test_existing_program_facts_require_matching_cited_official_passage_not_a_user_reference():
    fact = "공개 사업의 지원금은 월 20만원입니다"
    result = {"suggestion": {"content": fact}, "review_notes": []}
    analysis = {"context_investigation": {"sources": [{"source_id": "official-1", "supporting_excerpt": fact}],
        "observed_context": [{"text": fact, "source_ids": ["official-1"]}]}}
    response_assistance._check_program_claims(result, analysis, ["official-1"])
    with pytest.raises(ProviderUnavailable, match="response_assistance_unverified_program_claim"):
        response_assistance._check_program_claims(result, analysis, ["analysis-only"])
    with pytest.raises(ProviderUnavailable, match="response_assistance_unverified_program_claim"):
        response_assistance._check_program_claims({"suggestion": {"content": fact.replace("20만원", "30만원")}, "review_notes": []},
                                                  analysis, ["official-1"])
