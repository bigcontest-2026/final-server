"""Research evidence boundaries, without external model calls or internet."""
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from donghaeng_agent import analysis_research as research
from donghaeng_agent.analysis_research_provider import ResearchEvents, CodexResearchProvider, create_research_provider
from donghaeng_agent.provider import ProviderUnavailable
from donghaeng_agent.lifecycle_guide import build_lifecycle_interpretation


@pytest.fixture
def report():
    return {"scope": {"region_code": "51110", "month": "202510", "sex_code": "MAN", "age_code": "20G"},
            "assessments": [{"scope": {"region_code": "51110", "month": "202510", "sex_code": "MAN", "age_code": "20G"},
                "category": "priority_review", "observation": {"region_name": "춘천시"},
                "sources": {"skt": {"change_percent": -5.1}, "card_a": {"change_percent": -6.2}}}],
            "context": {"i5": {"record": None}, "mois": {"record": None}}, "quality_status": "hold",
            "operational_alert_allowed": False, "rule_version": "activity-review-5pct-v1",
            "limitations": ["품질 검토가 필요합니다."]}


def draft(report):
    return {"scope": deepcopy(report["scope"]), "status": "completed", "summary": "지역의 행사 일정은 설명 후보이며 인과 근거가 아닙니다.",
            "observed_context": [{"text": "해당 기간에 지역 행사가 안내되었습니다.", "source_ids": ["source_1"]}],
            "possible_causes": [{"title": "일정에 따른 활동 변화 가능성", "explanation": "일정의 영향을 추가 검토할 수 있습니다.",
                                 "source_ids": ["source_1"], "limitations": ["집단별 활동 감소와의 인과관계는 입증되지 않았습니다."]}],
            "verification_questions": ["일정과 집단별 활동의 관계를 확인할 자료가 있습니까?"],
            "sources": [{"source_id": "source_1", "title": "지역 행사 안내", "url": "https://www.chuncheon.go.kr/news/123",
                         "publisher": "춘천시", "published_at": "2025-10-01", "reference_period": "2025-10",
                         "region_scope": "춘천시", "geography_relation": "same_region", "period_relation": "same_period",
                         "supporting_excerpt": "2025년 10월 지역 행사 일정을 안내합니다."}],
            "limitations": ["행사 정보는 활동 변화의 원인을 확정하지 않습니다."]}


class FakeProvider:
    def __init__(self, result, evidence=None):
        self.result = result
        self.evidence = evidence if evidence is not None else {"search_count": 1, "search_queries": ["춘천시 2025년 10월 행사"], "consulted_urls": []}
        self.seen = None

    def research(self, payload):
        self.seen = deepcopy(payload)
        payload["observations"][0]["category"] = "malicious_mutation"
        return {"result": deepcopy(self.result), "web_evidence": deepcopy(self.evidence),
                "model": {"provider": "fake", "actual_model_call": False}}


@pytest.fixture
def verify(monkeypatch):
    seen = []
    def checked(source):
        seen.append(deepcopy(source))
        return {**source, "content_sha256": "a" * 64, "accessed_at": "2026-10-25T12:00:00+00:00",
                "verification_method": "public_page_excerpt_match", "causal_evidence": False}
    monkeypatch.setattr(research, "verify_public_source", checked)
    return seen


def test_research_preserves_frozen_scope_numbers_and_categories(report, verify):
    before = deepcopy(report)
    provider = FakeProvider(draft(report))
    result = research.research_analysis_report(report, provider=provider)
    assert report == before
    assert provider.seen["scope"] == report["scope"]
    assert provider.seen["observations"] == report["assessments"]
    assert result["scope"] == report["scope"]
    assert result["status"] == "completed" and result["web_search_count"] == 1
    assert result["sources"][0]["content_sha256"] == "a" * 64
    assert result["sources"][0]["causal_evidence"] is False
    assert result["prompt_version"] == research.RESEARCH_PROMPT_VERSION
    assert len(verify) == 1


def test_dong_research_keeps_selected_context_and_parent_activity_scopes(report, verify):
    report["scope"]["dong_code"] = "5111053000"
    report["region_name"] = "춘천시 테스트동"
    report["context"]["i5"]["record"] = {"region_name": "춘천시", "area_name": "테스트동",
        "area_code": "5111053000", "spatial_unit": "administrative_dong", "i5_reference": 37.2}
    report["context"]["activity_scope"] = {"region_code": "51110", "spatial_unit": "sgg",
        "role": "parent_sgg_reference", "is_dong_observation": False}
    report["lifecycle_interpretation"] = build_lifecycle_interpretation(report["scope"], report["assessments"])
    provider = FakeProvider(draft(report))
    result = research.research_analysis_report(report, provider=provider)
    assert provider.seen["scope"]["dong_code"] == "5111053000"
    assert provider.seen["region_name"] == "춘천시 테스트동"
    assert provider.seen["observations"][0]["scope"]["region_code"] == "51110"
    assert "dong_code" not in provider.seen["observations"][0]["scope"]
    assert provider.seen["lifecycle_interpretation"]["scope"]["region_code"] == "51110"
    assert result["scope"]["dong_code"] == "5111053000"


def with_guide(report):
    report["lifecycle_interpretation"] = build_lifecycle_interpretation(report["scope"], report["assessments"])
    return report


def lifecycle_check(report, status="supported_context", source_ids=None):
    return {"hypothesis_id": "lifecycle:20G:context", "scope": deepcopy(report["scope"]),
            "status": status, "text": "지역의 관련 배경은 확인되지만 실제 생애사건과 지원 필요는 미확인입니다.",
            "source_ids": ["source_1"] if source_ids is None else source_ids}


def test_research_payload_freezes_literature_questions_without_counting_as_sources(report, verify):
    with_guide(report)
    before = deepcopy(report)
    provider = FakeProvider(draft(report))
    result = research.research_analysis_report(report, provider=provider)
    assert report == before
    guide = provider.seen["lifecycle_interpretation"]
    assert guide == report["lifecycle_interpretation"]
    assert guide["groups"][0]["questions"]
    assert guide["source"]["evidence_role"] == "interpretive_literature"
    assert guide["source"]["regional_confirmation"] is False
    assert result["lifecycle_guide_version"] == guide["guide_version"]
    assert result["lifecycle_guide_sha256"] == guide["guide_sha256"]
    assert result["lifecycle_checks"] == [] and len(result["sources"]) == len(verify) == 1
    encoded = json.dumps(provider.seen, ensure_ascii=False)
    assert "/Users/" not in encoded and "local_path" not in encoded and "full_text" not in encoded
    for instruction in ("반증", "달력", "경계 변경", "19~29세", "65세 이상", "지역 확인 근거"):
        assert instruction in research.RESEARCH_INSTRUCTIONS


@pytest.mark.parametrize("status,source_ids", [("supported_context", ["source_1"]),
    ("counter_context", ["source_1"]), ("unverified", [])])
def test_lifecycle_check_binds_to_exact_question_group_and_official_sources(report, verify, status, source_ids):
    with_guide(report)
    expected = lifecycle_check(report, status, source_ids)
    result = draft(report)
    result["lifecycle_checks"] = [expected]
    got = research.research_analysis_report(report, provider=FakeProvider(result))
    assert got["lifecycle_checks"] == [expected]
    assert report["lifecycle_interpretation"]["support_need_status"] == "unconfirmed"
    assert report["lifecycle_interpretation"]["groups"][0]["hypotheses"][0]["status"] == "unverified"


@pytest.mark.parametrize("mutate,code", [
    (lambda c: c.update(hypothesis_id="lifecycle:40G:context"), "research_lifecycle_scope_mismatch"),
    (lambda c: c["scope"].update(sex_code="WMAN"), "research_lifecycle_scope_mismatch"),
    (lambda c: c["scope"].update(region_code="11680"), "research_lifecycle_scope_mismatch"),
    (lambda c: c["scope"].update(month="202509"), "research_lifecycle_scope_mismatch"),
    (lambda c: c.update(source_ids=["literature:kihasa:2025-07"]), "research_unknown_source_reference"),
    (lambda c: c.update(source_ids=[]), "research_lifecycle_context_without_source"),
    (lambda c: c.update(status="confirmed"), "research_output_schema_error"),
    (lambda c: c.update(support_need_status="confirmed"), "research_output_schema_error"),
])
def test_lifecycle_cross_scope_or_unverified_claims_rejected(report, verify, mutate, code):
    with_guide(report)
    result = draft(report)
    result["lifecycle_checks"] = [lifecycle_check(report)]
    mutate(result["lifecycle_checks"][0])
    with pytest.raises(ProviderUnavailable, match=code):
        research.research_analysis_report(report, provider=FakeProvider(result))
    assert not verify


def test_invalid_frozen_lifecycle_scope_fails_before_model_call(report, verify):
    with_guide(report)
    report["lifecycle_interpretation"]["groups"][0]["scope"]["month"] = "202509"
    provider = FakeProvider(draft(report))
    with pytest.raises(ProviderUnavailable, match="research_lifecycle_scope_invalid"):
        research.research_analysis_report(report, provider=provider)
    assert provider.seen is None and not verify


def test_old_report_without_guide_remains_usable_but_cannot_invent_checks(report, verify):
    provider = FakeProvider(draft(report))
    got = research.research_analysis_report(report, provider=provider)
    assert got["lifecycle_checks"] == []
    assert "lifecycle_interpretation" not in provider.seen and "lifecycle_guide_version" not in got
    result = draft(report)
    result["lifecycle_checks"] = [lifecycle_check(report)]
    with pytest.raises(ProviderUnavailable, match="research_lifecycle_scope_mismatch"):
        research.research_analysis_report(report, provider=FakeProvider(result))


def test_no_evidence_can_record_unverified_lifecycle_question(report, verify):
    with_guide(report)
    result = draft(report)
    result.update(status="no_evidence", sources=[], observed_context=[], possible_causes=[],
                  lifecycle_checks=[lifecycle_check(report, "unverified", [])])
    got = research.research_analysis_report(report, provider=FakeProvider(result))
    assert got["lifecycle_checks"][0]["status"] == "unverified" and not verify


def test_new_provider_schema_is_strict_while_old_saved_output_can_omit_checks(report):
    schema = research.ResearchDraft.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])
    assert research.ResearchDraft.model_validate(draft(report)).lifecycle_checks == []


def test_no_evidence_requires_search_but_has_no_invented_facts(report, verify):
    result = draft(report)
    result.update(status="no_evidence", sources=[], observed_context=[], possible_causes=[],
                  summary="관측 기간에 맞는 원문 근거를 확보하지 못했습니다.")
    got = research.research_analysis_report(report, provider=FakeProvider(result))
    assert got["status"] == "no_evidence" and not got["sources"] and not verify
    assert "출처 원문과 인용 구절을 확인한" not in " ".join(got["limitations"])
    assert "원문 근거를 확보하지 못했습니다" in " ".join(got["limitations"])
    result["observed_context"] = [{"text": "추정 사실", "source_ids": ["source_1"]}]
    with pytest.raises(ProviderUnavailable):
        research.research_analysis_report(report, provider=FakeProvider(result))


@pytest.mark.parametrize("evidence", [{}, {"search_count": 0}, {"search_count": True}, {"search_count": 13}])
def test_missing_or_excess_search_is_not_success(report, evidence, verify):
    with pytest.raises(ProviderUnavailable, match="research_search_not_verified"):
        research.research_analysis_report(report, provider=FakeProvider(draft(report), evidence))
    assert not verify


@pytest.mark.parametrize("mutate,code", [
    (lambda d: d["scope"].update(month="202610"), "research_scope_mismatch"),
    (lambda d: d["scope"].update(region_code="11680"), "research_scope_mismatch"),
    (lambda d: d["observed_context"][0].update(source_ids=["invented"]), "research_unknown_source_reference"),
    (lambda d: d["possible_causes"][0].update(source_ids=["invented"]), "research_unknown_source_reference"),
    (lambda d: d.update(summary="https://invented.example/claim"), "research_unlisted_source_link"),
    (lambda d: d.update(sources=[], observed_context=[], possible_causes=[]), "research_completed_without_evidence"),
    (lambda d: d.update(risk_score=90), "research_output_schema_error"),
    (lambda d: d["possible_causes"][0].update(limitations=[]), "research_output_schema_error"),
])
def test_malformed_or_unscoped_claims_rejected(report, verify, mutate, code):
    result = draft(report)
    mutate(result)
    with pytest.raises(ProviderUnavailable, match=code):
        research.research_analysis_report(report, provider=FakeProvider(result))
    assert not verify


def test_citations_cannot_claim_unconsulted_urls_when_transport_provides_sources(report, verify):
    evidence = {"search_count": 1, "search_queries": ["query"], "consulted_urls": ["https://www.chuncheon.go.kr/news/999"]}
    with pytest.raises(ProviderUnavailable, match="research_source_not_consulted"):
        research.research_analysis_report(report, provider=FakeProvider(draft(report), evidence))
    assert not verify


@pytest.mark.parametrize("url", ["http://www.chuncheon.go.kr/a", "https://127.0.0.1/", "https://localhost/", "file:///secret",
    "https://www.chuncheon.go.kr.evil.example/a", "https://user:password@www.chuncheon.go.kr/a",
    "https://www.chuncheon.go.kr:8443/a", "https://www.chuncheon.go.kr/login", "https://www.chuncheon.go.kr/search?q=x"])
def test_source_url_public_official_allowlist(url):
    with pytest.raises(ProviderUnavailable, match="research_source_url_invalid"):
        research.public_source_url(url)


def test_only_actual_completed_web_events_count_and_deduplicate():
    events = ResearchEvents()
    item = {"id": "web1", "type": "web_search", "query": "춘천 2025년 10월", "status": "completed"}
    events.accept({"type": "item.started", "item": item})
    assert events.evidence()["search_count"] == 0
    events.accept({"type": "item.completed", "item": item})
    events.accept({"type": "item.completed", "item": item})
    assert events.evidence() == {"search_count": 1, "search_queries": ["춘천 2025년 10월"], "consulted_urls": []}


@pytest.mark.parametrize("kind", ["command_execution", "file_change", "mcp_tool_call", "browser_use", "plan_update"])
def test_even_started_forbidden_tools_fail_closed(kind):
    with pytest.raises(ProviderUnavailable, match="research_forbidden_tool"):
        ResearchEvents().accept({"type": "item.started", "item": {"id": "bad", "type": kind}})


def test_web_tool_failure_does_not_become_no_evidence():
    with pytest.raises(ProviderUnavailable, match="research_web_search_failed"):
        ResearchEvents().accept({"type": "item.completed", "item": {"id": "w", "type": "web_search", "status": "failed"}})


def test_codex_args_isolate_host_and_enable_only_native_web(tmp_path):
    provider = CodexResearchProvider(executable="/fake/codex")
    args = provider._argv(tmp_path)
    assert 'web_search="live"' in args and 'web_search="disabled"' not in args
    assert "--ignore-user-config" in args and "--strict-config" in args and "--ephemeral" in args
    assert "read-only" in args and 'approval_policy="never"' in args
    assert "shell_tool" in args and "plugins" in args and "mcp_servers={}" in args


def test_native_web_event_accepts_only_the_pinned_cli_double_id_shape():
    from donghaeng_agent.analysis_research_provider import _event_json, _json
    raw = '{"type":"item.completed","item":{"id":"item_1","type":"web_search","id":"exec-test","query":"공식 지역 자료","status":"completed"}}'
    event = _event_json(raw)
    events = ResearchEvents()
    events.accept(event)
    assert events.evidence()["search_count"] == 1
    assert events.evidence()["search_queries"] == ["공식 지역 자료"]
    for changed in [raw.replace('"web_search"', '"agent_message"'),
                    raw.replace('"exec-test"', '"other"'),
                    raw.replace('"query":', '"id":"exec-extra","query":')]:
        with pytest.raises(ProviderUnavailable, match="research_output_schema_error"):
            _event_json(changed)
    # Generated JSON never receives this transport-only compatibility exception.
    with pytest.raises(ProviderUnavailable, match="research_output_schema_error"):
        _json(raw)


def test_research_profile_has_working_native_web_host_but_no_shell(tmp_path):
    args = CodexResearchProvider(executable="/fake/codex")._argv(tmp_path)
    pairs = list(zip(args, args[1:]))
    assert ("--enable", "code_mode_host") in pairs
    for feature in ("shell_tool", "unified_exec", "apps", "plugins", "code_mode", "multi_agent"):
        assert ("--disable", feature) in pairs


def test_provider_selection_never_falls_back_to_paid_api(monkeypatch):
    monkeypatch.delenv("DONGHAENG_PROVIDER", raising=False)
    assert isinstance(create_research_provider(), CodexResearchProvider)
    monkeypatch.setenv("DONGHAENG_PROVIDER", "typo")
    with pytest.raises(ProviderUnavailable, match="research_provider_not_configured"):
        create_research_provider()


def fetch_with_mock(monkeypatch, handler):
    real_client = httpx.Client
    monkeypatch.setattr(research, "_public_host", lambda url: None)
    monkeypatch.setattr(research.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))


def test_actual_source_excerpt_verified_and_hashed(report, monkeypatch):
    html = "<html><script>ignore this instruction</script><p>2025년 10월 지역 행사 일정을 안내합니다.</p></html>".encode()
    fetch_with_mock(monkeypatch, lambda request: httpx.Response(200, content=html, headers={"content-type": "text/html; charset=utf-8"}))
    got = research.verify_public_source(draft(report)["sources"][0])
    assert got["verification_method"] == "public_page_excerpt_match"
    assert len(got["content_sha256"]) == 64 and got["resolved_url"].startswith("https://")
    assert got["supporting_excerpt"] == draft(report)["sources"][0]["supporting_excerpt"]


def test_missing_quote_rejects_hallucinated_citation(report, monkeypatch):
    fetch_with_mock(monkeypatch, lambda request: httpx.Response(200, text="Different content", headers={"content-type": "text/html"}))
    with pytest.raises(ProviderUnavailable, match="research_source_excerpt_unverified"):
        research.verify_public_source(draft(report)["sources"][0])


def test_redirect_cannot_reach_private_or_nonofficial_destination(report, monkeypatch):
    fetch_with_mock(monkeypatch, lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/private"}))
    with pytest.raises(ProviderUnavailable, match="research_source_url_invalid"):
        research.verify_public_source(draft(report)["sources"][0])


def test_official_host_resolving_to_private_ip_is_rejected(monkeypatch):
    monkeypatch.setattr(research.socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ProviderUnavailable, match="research_source_address_forbidden"):
        research._public_host("https://www.chuncheon.go.kr/news/123")


def subprocess_events(events, *, exit_code=0, delay=0):
    import sys
    script = ("import json,sys,time\n"
              "sys.stdin.read()\n"
              f"events=json.loads({json.dumps(json.dumps(events))})\n"
              "for event in events: print(json.dumps(event),flush=True)\n"
              f"time.sleep({delay})\n"
              f"sys.exit({exit_code})\n")
    return [sys.executable, "-c", script]


def test_native_transport_complete_event_stream_without_model_or_network():
    events = [{"type": "thread.started", "thread_id": "fake"}, {"type": "turn.started"},
              {"type": "item.completed", "item": {"id": "w", "type": "web_search", "query": "public context"}},
              {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "{}"}},
              {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}]
    got = CodexResearchProvider(executable="/unused")._run_research(subprocess_events(events), "synthetic input")
    assert got.completed and got.final == "{}" and got.evidence()["search_count"] == 1
    assert got.usage == {"input_tokens": 10, "output_tokens": 5}


@pytest.mark.parametrize("events,exit_code", [([], 1), ([{"type": "turn.started"}], 0),
    ([{"type": "turn.completed", "usage": {}}], 0)])
def test_native_transport_incomplete_exit_is_not_success(events, exit_code):
    with pytest.raises(ProviderUnavailable, match="research_incomplete_turn"):
        CodexResearchProvider(executable="/unused")._run_research(subprocess_events(events, exit_code=exit_code), "synthetic input")


def test_native_transport_forbidden_event_terminates_child():
    events = [{"type": "item.started", "item": {"id": "x", "type": "command_execution"}}]
    with pytest.raises(ProviderUnavailable, match="research_forbidden_tool"):
        CodexResearchProvider(executable="/unused")._run_research(subprocess_events(events, delay=20), "synthetic input")


def test_native_transport_deadline_terminates_child():
    with pytest.raises(ProviderUnavailable, match="research_timeout"):
        CodexResearchProvider(executable="/unused", timeout_seconds=1)._run_research(subprocess_events([], delay=20), "synthetic input")


def test_server_verified_comparison_observations_reach_provider_and_survive_mutation(report, verify):
    comparison = {"report_ids": ["analysis_saved"], "reports": [{
        "report_id": "analysis_saved", "created_at": "2026-10-25T00:00:00+00:00",
        "scope": {"region_code": "51130", "month": "202510", "sex_code": "MAN", "age_code": "20G"},
        "region_name": "원주시", "rule_version": report["rule_version"], "threshold_percent": 5.0,
        "provenance": {"activity": {"run_id": "activity-test", "manifest_sha256": "a" * 64},
                       "survey": {"run_id": "survey-test", "manifest_sha256": "b" * 64}},
        "observations": [{"category": "no_decline", "sources": {"skt": {"change_percent": 1.5}}}],
        "context": {}, "quality_status": "hold", "operational_alert_allowed": False}]}
    report["comparison_context"] = deepcopy(comparison)
    before = deepcopy(report)
    class MutatingComparisonProvider(FakeProvider):
        def research(self, payload):
            got = super().research(payload)
            payload["comparison_context"]["reports"][0]["observations"].clear()
            return got
    provider = MutatingComparisonProvider(draft(report))
    result = research.research_analysis_report(report, provider=provider)
    assert provider.seen["comparison_context"] == comparison
    assert result["comparison_context"] == comparison
    assert report == before
