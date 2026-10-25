"""The first report remains useful when optional external research fails."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import threading

from fastapi.testclient import TestClient
import pytest

from app import analysis_api as api
from app.security import AccessController
from donghaeng_agent.analysis_report_store import AnalysisReportStore
from donghaeng_agent.provider import ProviderUnavailable
from test_analysis_api import application, report_settings, request, PATH


def research_result(status="completed"):
    return {"status": status, "summary": "해당 기간 지역 맥락을 검토했습니다. 원인으로 확정하지 않습니다.",
        "observed_context": [{"text": "공식 지역 통계의 대상 기간을 확인했습니다.", "source_ids": ["S1"]}] if status == "completed" else [],
        "possible_causes": [{"title": "달력 구성 차이", "explanation": "비교할 설명 후보입니다.",
            "source_ids": ["S1"], "limitations": ["집단별 활동 감소와 인과관계는 미확인입니다."]}] if status == "completed" else [],
        "verification_questions": ["동일 집단·기간의 근거가 있는지 추가 확인해 주세요."],
        "sources": [{"source_id": "S1", "title": "공식 지역 통계", "url": "https://www.mois.go.kr/example",
                     "publisher": "합성 시험 자료", "published_at": "2025-12-01", "reference_period": "2025-12",
                     "accessed_at": "2026-10-25T00:00:00+00:00", "excerpt": "합성 시험용 원문"}] if status == "completed" else [],
        "search_queries": ["강남구 2025년 12월 공식 통계"], "limitations": ["실제 외부 검색을 하지 않은 합성 시험입니다."],
        "model": {"provider": "test", "model_call_succeeded": True}, "prompt_version": api.RESEARCH_PROMPT_VERSION}


def test_researched_report_is_separate_immutable_and_reused_after_restart(report_settings, monkeypatch):
    calls = []
    def research(base):
        calls.append(deepcopy(base))
        # Even an accidentally mutating research implementation cannot change the frozen base.
        base["assessments"][0]["category_label"] = "must not persist"
        return research_result()
    monkeypatch.setattr(api, "research_analysis_report", research)
    with TestClient(application(report_settings)) as client:
        base = client.post(PATH, json=request()).json()
        response = client.post(f"{PATH}/{base['report_id']}/investigate", json={})
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["report_id"] != base["report_id"]
        assert report["parent_report_id"] == base["report_id"]
        assert report["context_investigation_status"] == "completed"
        for field in ("scope", "provenance", "summary", "assessments", "context", "rule_version", "threshold_percent",
                      "quality_status", "operational_alert_allowed", "recommendation_status"):
            assert report[field] == base[field]
        assert "# 현황·원인 분석 보고서" in report["markdown"]
        assert "가능한 원인" in report["markdown"] and "https://www.mois.go.kr/example" in report["markdown"]
        assert "외부 원인 조사는 수행하지 않았습니다" not in report["markdown"]
        assert client.get(f"{PATH}/{base['report_id']}").json()["markdown"] == base["markdown"]
        assert client.post(PATH, json=request()).json()["report_id"] == base["report_id"]
        listing = client.get(PATH).json()
        assert listing["total_matched"] == 2
        assert listing["reports"][0]["context_investigation_status"] == "completed"
    with TestClient(application(report_settings)) as client:
        repeated = client.post(f"{PATH}/{base['report_id']}/investigate", json={}).json()
        assert repeated["reused"] is True and repeated["report_id"] == report["report_id"]
        child = client.post(f"{PATH}/{report['report_id']}/investigate", json={}).json()
        assert child["report_id"] == report["report_id"]
    assert len(calls) == 1


def test_failed_research_keeps_baseline_and_allows_retry(report_settings, monkeypatch):
    def unavailable(_):
        raise ProviderUnavailable("private-provider-secret-must-not-leak")
    monkeypatch.setattr(api, "research_analysis_report", unavailable)
    with TestClient(application(report_settings)) as client:
        base = client.post(PATH, json=request()).json()
        url = f"{PATH}/{base['report_id']}/investigate"
        failed = client.post(url, json={})
        assert failed.status_code == 503
        assert "private-provider-secret" not in failed.text
        assert failed.json()["detail"]["saved_report_id"] == base["report_id"]
        assert client.get(PATH).json()["total_matched"] == 1
        assert client.get(f"{PATH}/{base['report_id']}").json()["context_investigation_status"] == "not_performed"
        monkeypatch.setattr(api, "research_analysis_report", lambda _: research_result("no_evidence"))
        completed = client.post(url, json={})
        assert completed.status_code == 200, completed.text
        report = completed.json()
        assert report["context_investigation_status"] == "no_evidence"
        assert report["context_investigation"]["possible_causes"] == []
        assert "충분한 근거를 확보하지 못했습니다" in report["markdown"]


def test_duplicate_concurrent_research_calls_only_one_model(report_settings, monkeypatch):
    started, finish = threading.Event(), threading.Event()
    calls = []
    def research(_):
        calls.append(1)
        started.set()
        assert finish.wait(10)
        return research_result()
    monkeypatch.setattr(api, "research_analysis_report", research)
    with TestClient(application(report_settings)) as client, ThreadPoolExecutor(max_workers=1) as pool:
        base = client.post(PATH, json=request()).json()
        url = f"{PATH}/{base['report_id']}/investigate"
        future = pool.submit(client.post, url, json={})
        try:
            assert started.wait(10)
            duplicate = client.post(url, json={})
            assert duplicate.status_code == 409
            assert duplicate.json()["detail"]["code"] == "analysis_research_in_progress"
        finally:
            finish.set()
        assert future.result(timeout=10).status_code == 200
    assert calls == [1]


def test_stale_lease_is_recoverable_and_old_worker_cannot_overwrite(report_settings):
    store = AnalysisReportStore(report_settings.investigation_db_path.with_name("lease-test.sqlite3"))
    first = store.claim_research("base", "v1", lease_seconds=-1)
    second = store.claim_research("base", "v1")
    assert first["token"] != second["token"]
    store.release_research("base", "v1", first["token"])
    assert store.claim_research("base", "v1")["status"] == "running"
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError, match="lease_lost"):
        store.finish_research("base", "v1", first["token"], {})


@pytest.mark.parametrize("body", [{"scope": "new scope"}, {"message": "change the numbers"}, {"provider": "other"}])
def test_caller_cannot_change_research_scope_or_provider(report_settings, body):
    with TestClient(application(report_settings)) as client:
        base = client.post(PATH, json=request()).json()
        assert client.post(f"{PATH}/{base['report_id']}/investigate", json=body).status_code == 422


def test_no_observation_or_unknown_report_never_calls_model(report_settings, monkeypatch):
    monkeypatch.setattr(api, "research_analysis_report", lambda _: pytest.fail("must not call model"))
    with TestClient(application(report_settings)) as client:
        assert client.post(f"{PATH}/missing/investigate", json={}).status_code == 404
        base = client.post(PATH, json=request(region_code="51110")).json()
        assert client.post(f"{PATH}/{base['report_id']}/investigate", json={}).status_code == 422


def test_research_requires_investigate_permission():
    assert AccessController.required_permission("POST", f"{PATH}/analysis_abc/investigate") == "investigate"
    assert AccessController.required_permission("GET", f"{PATH}/analysis_abc") == "read"


def test_markdown_treats_external_prose_as_untrusted(report_settings, monkeypatch):
    result = research_result()
    result["observed_context"][0]["text"] = '<script>bad()</script> [fake](https://evil.example)'
    result["verification_questions"] = ['<img src=x onerror=bad()> [fake](//evil.example)']
    monkeypatch.setattr(api, "research_analysis_report", lambda _: result)
    with TestClient(application(report_settings)) as client:
        base = client.post(PATH, json=request()).json()
        report = client.post(f"{PATH}/{base['report_id']}/investigate", json={}).json()
        assert '<script>' not in report["markdown"]
        assert '[fake](https://evil.example)' not in report["markdown"]
        assert '<img src=x onerror=bad()>' not in report["markdown"]
        assert r'\<img src=x onerror=bad\(\)\>' in report["markdown"]
        assert '[fake](//evil.example)' not in report["markdown"]


def test_institution_does_not_start_personal_subscription(report_settings, monkeypatch):
    monkeypatch.setattr(api, "research_analysis_report", lambda _: pytest.fail("must not call model"))
    # This dedicated router test isolates the provider boundary; existing security tests cover JWT/tenant binding.
    from fastapi import FastAPI
    settings = replace(report_settings, access_mode="institution", auth_issuer="test", auth_audience="test",
                       auth_tenant_id="test", auth_public_key_path=report_settings.investigation_db_path)
    app = FastAPI()
    app.include_router(api.analysis_router(settings))
    with TestClient(app) as client:
        assert client.post(f"{PATH}/any/investigate", json={}).status_code == 503


def saved_comparison(settings, base, region_code="11110", **changes):
    """Freeze synthetic comparison observations in the same temporary report store."""
    store = AnalysisReportStore(settings.investigation_db_path.with_name("analysis_reports.sqlite3"))
    report = deepcopy(base)
    for key in ("report_id", "created_at", "storage_status", "reused"):
        report.pop(key, None)
    report["scope"]["region_code"] = region_code
    report["region_name"] = "합성 비교 지역"
    for item in report["assessments"]:
        item["scope"]["region_code"] = region_code
        if item.get("observation"):
            item["observation"]["region_name"] = "합성 비교 지역"
    report.update(changes)
    return store.save(report)


def test_comparison_saved_observations_and_provenance_are_frozen_without_changing_base(report_settings, monkeypatch):
    seen = []
    def research(base):
        seen.append(deepcopy(base))
        base["comparison_context"]["reports"][0]["observations"].clear()
        return research_result()
    monkeypatch.setattr(api, "research_analysis_report", research)
    with TestClient(application(report_settings)) as client:
        base = client.post(PATH, json=request()).json()
        other = saved_comparison(report_settings, base)
        response = client.post(f"{PATH}/{base['report_id']}/investigate", json={"comparison_report_ids": [other["report_id"]]})
        assert response.status_code == 200, response.text
        report = response.json()
        comparison = report["comparison_context"]
        assert comparison["report_ids"] == [other["report_id"]]
        snapshot = comparison["reports"][0]
        assert snapshot["observations"] == other["assessments"]
        for field in ("report_id", "created_at", "scope", "region_name", "rule_version", "threshold_percent", "provenance", "context", "quality_status"):
            assert snapshot[field] == other[field]
        assert snapshot["operational_alert_allowed"] is False
        assert seen[0]["comparison_context"] == comparison
        assert report["context_investigation"]["comparison_context"] == comparison
        assert "합성 비교 지역" in report["markdown"]
        assert "동일 조건으로 검증한 저장 보고서" in report["markdown"]
        for field in ("scope", "provenance", "summary", "assessments", "context", "rule_version", "quality_status"):
            assert report[field] == base[field]
        assert client.get(f"{PATH}/{base['report_id']}").json()["markdown"] == base["markdown"]
        assert client.get(f"{PATH}/{other['report_id']}").json()["assessments"] == other["assessments"]


@pytest.mark.parametrize("body", [
    {"comparison_report_ids": "analysis_other"}, {"comparison_report_ids": [1]},
    {"comparison_report_ids": [""]}, {"comparison_report_ids": [" "]},
    {"comparison_report_ids": ["a", "b", "c", "d"]}, {"comparison_report_ids": ["a", "a"]},
    {"comparison_report_ids": None},
])
def test_comparison_request_rejects_invalid_or_duplicate_ids(report_settings, monkeypatch, body):
    monkeypatch.setattr(api, "research_analysis_report", lambda _: pytest.fail("invalid request must not research"))
    with TestClient(application(report_settings)) as client:
        base = client.post(PATH, json=request()).json()
        assert client.post(f"{PATH}/{base['report_id']}/investigate", json=body).status_code == 422


@pytest.mark.parametrize("change", ["month", "sex_code", "age_code", "spatial_unit", "rule_version", "threshold_percent", "activity_version", "survey_version", "missing_version", "no_observations"])
def test_comparison_incompatible_saved_report_never_calls_model(report_settings, monkeypatch, change):
    monkeypatch.setattr(api, "research_analysis_report", lambda _: pytest.fail("incompatible comparison must not research"))
    with TestClient(application(report_settings)) as client:
        base = client.post(PATH, json=request()).json()
        candidate = deepcopy(base)
        candidate["scope"]["region_code"] = "11110"
        if change in ("month", "sex_code", "age_code"):
            candidate["scope"][change] = {"month": "202511", "sex_code": "WMAN", "age_code": "40G"}[change]
        elif change == "spatial_unit":
            candidate["scope"]["dong_code"] = "1111051500"
        elif change == "rule_version":
            candidate["rule_version"] = "other-rule"
        elif change == "threshold_percent":
            candidate["threshold_percent"] = 7.0
        elif change in ("activity_version", "survey_version"):
            candidate["provenance"][change.removesuffix("_version")]["manifest_sha256"] = "b" * 64
        elif change == "missing_version":
            candidate["provenance"].pop("activity")
        elif change == "no_observations":
            candidate["assessments"] = []
            candidate["activity_availability"] = {"status": "unavailable"}
        other = saved_comparison(report_settings, candidate)
        response = client.post(f"{PATH}/{base['report_id']}/investigate", json={"comparison_report_ids": [other["report_id"]]})
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "analysis_comparison_incompatible"


def test_comparison_missing_self_and_duplicate_family_are_rejected(report_settings, monkeypatch):
    monkeypatch.setattr(api, "research_analysis_report", lambda _: research_result())
    with TestClient(application(report_settings)) as client:
        base = client.post(PATH, json=request()).json()
        other = saved_comparison(report_settings, base)
        child = client.post(f"{PATH}/{base['report_id']}/investigate", json={}).json()
        other_child = client.post(f"{PATH}/{other['report_id']}/investigate", json={}).json()
        monkeypatch.setattr(api, "research_analysis_report", lambda _: pytest.fail("invalid comparison must not research"))
        cases = [(["missing"], 404, "analysis_comparison_not_found"),
                 ([base["report_id"]], 422, "analysis_comparison_self"),
                 ([child["report_id"]], 422, "analysis_comparison_self"),
                 ([other["report_id"], other_child["report_id"]], 422, "analysis_comparison_duplicate")]
        for ids, status, code in cases:
            response = client.post(f"{PATH}/{base['report_id']}/investigate", json={"comparison_report_ids": ids})
            assert response.status_code == status, response.text
            assert response.json()["detail"]["code"] == code


def test_changed_comparisons_do_not_reuse_completed_report_and_child_can_change_selection(report_settings, monkeypatch):
    seen = []
    def research(base):
        seen.append(deepcopy(base.get("comparison_context", {})))
        return research_result()
    monkeypatch.setattr(api, "research_analysis_report", research)
    with TestClient(application(report_settings)) as client:
        base = client.post(PATH, json=request()).json()
        a = saved_comparison(report_settings, base)
        b = saved_comparison(report_settings, base, "11140")
        url = f"{PATH}/{base['report_id']}/investigate"
        plain = client.post(url, json={}).json()
        first = client.post(url, json={"comparison_report_ids": [a["report_id"]]})
        assert first.status_code == 200, first.text
        first = first.json()
        second = client.post(f"{PATH}/{first['report_id']}/investigate", json={"comparison_report_ids": [b["report_id"]]})
        assert second.status_code == 200, second.text
        second = second.json()
        assert len({plain["report_id"], first["report_id"], second["report_id"]}) == 3
        assert first["parent_report_id"] == second["parent_report_id"] == base["report_id"]
        assert second["comparison_context"]["report_ids"] == [b["report_id"]]
        assert client.post(url, json={"comparison_report_ids": [a["report_id"]]}).json()["report_id"] == first["report_id"]
        assert client.post(f"{PATH}/{first['report_id']}/investigate", json={}).json()["report_id"] == first["report_id"]
        assert client.post(f"{PATH}/{first['report_id']}/investigate", json={"comparison_report_ids": []}).json()["report_id"] == plain["report_id"]
        pair = client.post(url, json={"comparison_report_ids": [b["report_id"], a["report_id"]]}).json()
        reordered = client.post(url, json={"comparison_report_ids": [a["report_id"], b["report_id"]]}).json()
        assert reordered["report_id"] == pair["report_id"] and reordered["reused"] is True
    assert len(seen) == 4


def test_distinct_comparison_requests_have_independent_running_leases(report_settings, monkeypatch):
    started, finish = threading.Event(), threading.Event()
    first_ids = []
    def research(base):
        if base["comparison_context"]["report_ids"] == first_ids:
            started.set()
            assert finish.wait(10)
        return research_result()
    monkeypatch.setattr(api, "research_analysis_report", research)
    with TestClient(application(report_settings)) as client, ThreadPoolExecutor(max_workers=1) as pool:
        base = client.post(PATH, json=request()).json()
        a = saved_comparison(report_settings, base)
        b = saved_comparison(report_settings, base, "11140")
        first_ids.append(a["report_id"])
        url = f"{PATH}/{base['report_id']}/investigate"
        future = pool.submit(client.post, url, json={"comparison_report_ids": first_ids})
        try:
            assert started.wait(10)
            duplicate = client.post(url, json={"comparison_report_ids": first_ids})
            assert duplicate.status_code == 409
            distinct = client.post(url, json={"comparison_report_ids": [b["report_id"]]})
            assert distinct.status_code == 200, distinct.text
        finally:
            finish.set()
        assert future.result(timeout=10).status_code == 200
