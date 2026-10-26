"""Summaries use saved evidence, deduplicate work, and preserve original reports."""
from dataclasses import replace
import json
import sqlite3
from copy import deepcopy

from fastapi.testclient import TestClient
import pytest

from donghaeng_agent import activity_summary
from donghaeng_agent.provider import ProviderUnavailable
from test_analysis_api import application, report_settings, request, PATH


class Provider:
    model = "test-model"

    def __init__(self):
        self.count = 0
        self.error = None
        self.change = lambda result: result

    def decide(self, messages, allow_tools=True):
        assert allow_tools is False
        self.count += 1
        if self.error:
            raise ProviderUnavailable(self.error)
        payload = json.loads(messages[0]["content"])
        self.payload = payload
        result = self.change({
            "report_id": payload["report_id"], "scope": payload["analysis"]["scope"],
            "selected_fact_ids": [payload["analysis"]["facts"][0]["id"]],
            "interpretation": {
                "meaning": "통신과 카드 활동이 함께 줄어 지역의 이동과 소비 활동이 위축된 모습입니다.",
                "possible_background": "계절이나 생활 일정의 변화가 영향을 줄 수 있습니다.",
                "suggested_review": "감소가 이어지는지 살펴보고 지역 프로그램의 참여 여건을 점검하면 좋겠습니다.",
            },
            "quality_status": payload["analysis"]["quality_status"],
            "operational_alert_allowed": False,
        })
        return {"text": json.dumps(result, ensure_ascii=False), "calls": [],
                "evidence_ids": [payload["report_id"]]}


@pytest.fixture
def setup_summary(report_settings, monkeypatch):
    provider = Provider()
    monkeypatch.setattr(activity_summary, "create_provider", lambda: provider)
    with TestClient(application(report_settings)) as client:
        report = client.post(PATH, json=request(dong_code="1168051000")).json()
        yield client, report, provider


def summarize(client, report, **kwargs):
    return client.post(f"{PATH}/{report['report_id']}/summary", **kwargs)


def test_summary_reuses_success_after_restart_and_preserves_report(setup_summary, report_settings):
    client, report, provider = setup_summary
    before = client.get(f"{PATH}/{report['report_id']}").json()
    first = summarize(client, report)
    assert first.status_code == 200, first.text
    data = first.json()
    assert set(data) == {"report_id", "text", "created_at", "reused"}
    assert data["report_id"] == report["report_id"] and data["reused"] is False
    assert "이동과 소비 활동" in data["text"]
    assert "운영 경보" not in data["text"]
    assert provider.payload["analysis"]["scope"]["dong_code"] == "1168051000"
    assert "markdown" not in provider.payload["analysis"]
    assert client.get(f"{PATH}/{report['report_id']}").json() == before
    with TestClient(application(report_settings)) as restarted:
        again = summarize(restarted, report)
    assert again.status_code == 200
    assert again.json() == {**data, "reused": True}
    assert provider.count == 1


@pytest.mark.parametrize("code,status", [("model_timeout", 503), ("model_concurrency_limit", 409)])
def test_failure_is_not_cached_and_can_retry(setup_summary, code, status):
    client, report, provider = setup_summary
    provider.error = code
    result = summarize(client, report)
    assert result.status_code == status
    assert "test-model" not in result.text
    provider.error = None
    assert summarize(client, report).status_code == 200


@pytest.mark.parametrize("change", [
    lambda r: {**r, "scope": {**r["scope"], "month": "202501"}},
    lambda r: {**r, "operational_alert_allowed": True},
    lambda r: {**r, "quality_status": "ok"},
    lambda r: {**r, "sentences": ["고립 가능성이 99.99%입니다."]},
    lambda r: {**r, "selected_fact_ids": ["https://example.com"]},
    lambda r: {**r, "selected_fact_ids": ["unknown_observation"]},
    lambda r: {**r, "selected_fact_ids": ["observation_0", "observation_0"]},
])
def test_invalid_generation_is_rejected_without_caching(setup_summary, change):
    client, report, provider = setup_summary
    provider.change = change
    assert summarize(client, report).status_code == 503
    provider.change = lambda r: r
    assert summarize(client, report).json()["reused"] is False


def test_does_not_accept_client_report_or_unknown_filters(setup_summary):
    client, report, provider = setup_summary
    assert summarize(client, report, json={"scope": {"month": "202501"}}).status_code == 422
    assert summarize(client, report, params={"month": "202501"}).status_code == 422
    assert client.post(f"{PATH}/missing/summary").status_code == 404
    assert provider.count == 0


def test_cache_invalidates_when_model_changes(setup_summary):
    client, report, provider = setup_summary
    assert summarize(client, report).status_code == 200
    provider.model = "another-model"
    assert summarize(client, report).json()["reused"] is False
    assert provider.count == 2


def test_human_readable_summary_explains_meaning_without_boilerplate(setup_summary):
    client, report, provider = setup_summary
    result = summarize(client, report).json()["text"]
    assert "20대 남성" in result and "-20.00%" in result
    assert "강남구" in result and "이동과 소비 활동" in result
    assert "계절이나 생활 일정" in result and "참여 여건" in result
    for text in ("자료 품질", "운영 경보", "근거로", "해석하지", "quality_status", "operational_alert_allowed"):
        assert text not in result
    assert "MAN" not in result and "hold" not in result


def test_prose_cannot_override_the_scope_or_alert_limit(setup_summary):
    client, report, provider = setup_summary
    provider.change = lambda r: {**r, "sentences": [
        "부산 해운대구 여성 집단의 활동이 감소했습니다.",
        "운영 경보를 발령해야 합니다.", "품질 한계는 없습니다."]}
    assert summarize(client, report).status_code == 503


def test_duplicate_and_expired_lease(setup_summary, report_settings):
    client, report, provider = setup_summary
    from donghaeng_agent.analysis_report_store import AnalysisReportStore
    store = AnalysisReportStore(report_settings.investigation_db_path.with_name("analysis_reports.sqlite3"))
    key = activity_summary.summary_key(report, provider)
    claim = store.claim_summary(report["report_id"], key)
    assert claim["status"] == "claimed"
    assert summarize(client, report).status_code == 409
    with store.connection(write=True) as db:
        db.execute("UPDATE analysis_report_summaries SET lease_until=0")
    assert summarize(client, report).status_code == 200
    with pytest.raises(sqlite3.DatabaseError):
        store.finish_summary(report["report_id"], key, claim["token"], "stale")


def test_institution_mode_does_not_call_subscription(report_settings, monkeypatch):
    from app.security import Principal
    import app.security as security
    settings = replace(report_settings, access_mode="institution", auth_tenant_id="tenant",
                       auth_issuer="test", auth_audience="test", auth_public_key_path=report_settings.investigation_db_path)
    # Keep the real permission dispatch; inject only the external authentication boundary.
    monkeypatch.setattr(security.AccessController, "__init__", lambda self, settings: None)
    monkeypatch.setattr(security.AccessController, "authenticate", lambda self, req:
                        Principal("test", "tenant", frozenset({"investigate"}), "institution"))
    with TestClient(application(settings)) as client:
        response = client.post(f"{PATH}/missing/summary")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "institution_provider_not_configured"


@pytest.mark.parametrize("interpretation", [
    {"meaning": " ", "possible_background": "계절 변화", "suggested_review": "살펴보기"},
    {"meaning": "활동이 99% 감소했습니다.", "possible_background": "계절 변화", "suggested_review": "살펴보기"},
    {"meaning": "quality_status=hold를 유지합니다.", "possible_background": "계절 변화", "suggested_review": "살펴보기"},
])
def test_invalid_interpretation_is_not_cached(setup_summary, interpretation):
    client, report, provider = setup_summary
    provider.change = lambda r: {**r, "interpretation": interpretation}
    assert summarize(client, report).status_code == 503
    provider.change = lambda r: r
    assert summarize(client, report).json()["reused"] is False


def test_missing_activity_stays_brief_without_inventing_an_interpretation(setup_summary):
    client, report, provider = setup_summary
    missing = client.post(PATH, json=request(region_code="51110", sex_code=None, age_code=None)).json()
    response = summarize(client, missing)
    assert response.status_code == 200
    assert "비교 자료가 없습니다" in response.json()["text"]
    assert "계절" not in response.json()["text"]
    assert "운영 경보" not in response.json()["text"]
    assert provider.count == 0


def test_prompt_change_invalidates_old_summary(setup_summary, monkeypatch):
    client, report, provider = setup_summary
    assert summarize(client, report).status_code == 200
    monkeypatch.setattr(activity_summary, "PROMPT_VERSION", "changed-summary-prompt")
    assert summarize(client, report).json()["reused"] is False
    assert provider.count == 2


@pytest.mark.parametrize("field,text", [
    ("meaning", "부산 해운대구 여성의 통신과 카드 활동이 모두 줄었습니다."),
    ("meaning", "개인의 고립 위험이 확정됐습니다."),
    ("possible_background", "지역 축제가 취소됐기 때문입니다."),
    ("suggested_review", "운영 경보를 발령해야 합니다."),
])
def test_interpretation_fields_cannot_replace_scope_or_assert_local_events(setup_summary, field, text):
    client, report, provider = setup_summary
    provider.change = lambda r: {**r, "interpretation": {**r["interpretation"], field: text}}
    assert summarize(client, report).status_code == 503
    provider.change = lambda r: r
    assert summarize(client, report).json()["reused"] is False


@pytest.mark.parametrize("skt,card", [(10, 20), (-10, 20), (-10, None)])
def test_joint_decline_prose_is_rejected_when_sources_do_not_both_decline(setup_summary, skt, card):
    _, report, provider = setup_summary
    altered = deepcopy(report)
    for source, value in (("skt", skt), ("card_a", card)):
        altered["assessments"][0]["sources"][source]["change_percent"] = value
    with pytest.raises(ProviderUnavailable, match="activity_summary_output_invalid"):
        activity_summary.summarize_activity(altered, provider)


@pytest.mark.parametrize("skt,card,meaning", [
    (10, 20, "통신과 카드 활동이 함께 늘어 이동과 소비가 활발해진 모습입니다."),
    (-10, 20, "통신 활동은 줄고 카드 거래는 늘어 이동과 소비 흐름이 엇갈립니다."),
    (-10, None, "통신 활동은 줄었으며 카드 비교 자료는 없습니다."),
])
def test_valid_source_directions_keep_exact_numbers_and_allow_plain_explanations(setup_summary, skt, card, meaning):
    _, report, provider = setup_summary
    altered = deepcopy(report)
    for source, value in (("skt", skt), ("card_a", card)):
        altered["assessments"][0]["sources"][source]["change_percent"] = value
    provider.change = lambda r: {**r, "interpretation": {**r["interpretation"], "meaning": meaning}}
    result = activity_summary.summarize_activity(altered, provider)
    assert meaning in result
    assert f"통신 {skt:+.2f}%" in result
    assert ("카드 비교 자료 없음" if card is None else f"카드 {card:+.2f}%") in result
    assert report["quality_status"] == "hold" and report["operational_alert_allowed"] is False


@pytest.mark.parametrize("meaning", [
    "통신 활동은 줄었지만 카드 활동은 늘어, 방문과 소비가 함께 위축된 모습은 아닙니다.",
    "통신은 감소했고 카드 거래는 줄지 않고 늘어 흐름이 엇갈렸습니다.",
    "통신 유동인구는 줄었지만 카드 거래는 늘어 방문과 소비의 흐름이 엇갈렸습니다.",
])
def test_natural_possibility_and_negated_joint_decline_are_allowed(setup_summary, meaning):
    _, report, provider = setup_summary
    altered = deepcopy(report)
    altered["assessments"][0]["sources"]["skt"]["change_percent"] = -10
    altered["assessments"][0]["sources"]["card_a"]["change_percent"] = 20
    provider.change = lambda r: {**r, "interpretation": {**r["interpretation"], "meaning": meaning,
        "possible_background": "계절이나 생활 일정의 변화가 영향을 줬을 수도 있습니다."}}
    assert meaning in activity_summary.summarize_activity(altered, provider)


def test_multi_group_meaning_can_describe_other_groups_from_the_overview(setup_summary):
    _, report, provider = setup_summary
    altered = deepcopy(report)
    other = deepcopy(altered["assessments"][0])
    other["scope"]["age_code"] = "40G"
    for source in ("skt", "card_a"):
        other["sources"][source]["change_percent"] = 10
    altered["assessments"].append(other)
    altered["summary"]["total"] = 2
    meaning = "선택한 집단에서는 장소 유동과 거래가 함께 줄었습니다. 다른 집단에서는 두 활동이 함께 늘어난 흐름도 나타납니다."
    provider.change = lambda r: {**r, "selected_fact_ids": ["overview", "observation_0"],
        "interpretation": {**r["interpretation"], "meaning": meaning}}
    assert meaning in activity_summary.summarize_activity(altered, provider)
