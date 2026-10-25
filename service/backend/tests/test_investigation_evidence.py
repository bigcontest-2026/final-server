"""Agent evidence contracts; fabricated bundles stay inside isolated test folders."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import DEFAULT_RESEARCH, DEFAULT_SUPPORT, Settings
from app.main import create_app
from donghaeng_agent.tools import InvestigationTools
from test_final_model import behavior, survey_row, write_bundle


def request(**changes):
    return {"region_code": "1168051000", "city": "서울", "age_group": "20대",
            "purpose": "서로 다른 공간·모집단 근거의 분리 검증", "measurement_as_of": "2025-12",
            "support_as_of": "2026-10-21", "sk_mode": "excluded", "idempotency_key": "evidence-test-create", **changes}


@pytest.fixture
def evidence_settings(tmp_path):
    activity = write_bundle(tmp_path / "activity", {
        "behavior_evidence.jsonl": [behavior(), behavior(age="40G")],
        "historical_reference.jsonl": [],
    }, "activity")
    survey = write_bundle(tmp_path / "survey", {
        "sgg_isolation_index.jsonl": [survey_row()],
        "dong_isolation_index.jsonl": [survey_row("administrative_dong", "1168051000", "신사동")],
    }, "survey")
    manifest = tmp_path / "support.json"
    manifest.write_text('{"sources": []}', encoding="utf-8")
    return Settings(mode="research", final_model_bundle=activity, i5_bundle_path=survey,
                    support_manifest_path=manifest, investigation_worker_enabled=False,
                    investigation_db_path=tmp_path / "cases.sqlite3",
                    investigation_checkpoint_path=tmp_path / "checkpoints.sqlite3")


def test_startup_and_distinct_geography_population_evidence(evidence_settings):
    application = create_app(evidence_settings)
    with TestClient(application) as client:
        created = client.post("/api/investigations", json=request())
        assert created.status_code == 201, created.text
        case = created.json()
        response = client.post(f"/api/investigations/{case['case_id']}/runs", json={
            "strategy": "rule_baseline", "expected_case_version": case["case_version"],
            "idempotency_key": "evidence-test-run"})
        assert response.status_code == 202, response.text
        assert application.state.investigation_worker.drain_once()
        saved = client.get(f"/api/investigations/{case['case_id']}").json()
        assert saved["status"] == "awaiting_review", saved.get("executions")
        brief = saved["briefs"][0]
        i5 = next(o for o in brief["observations"] if o["kind"] == "survey_i5_population_reference")
        activity = next(o for o in brief["observations"] if o["kind"] == "final_activity_context")
        assert i5["data"] == survey_row("administrative_dong", "1168051000", "신사동")
        assert i5["data"]["behavior_signals_included"] is False
        assert activity["data"]["records"][0]["behavior"] == behavior()
        assert activity["data"]["total_matched"] == 1  # 40G is not copied into requested 20G.
        assert activity["geography"] == {"spatial_unit": "sgg", "region_code": "11680",
                                         "scope_relation": "containing_region_context"}
        assert brief["quality_status"] == "hold" and brief["operational_alert_allowed"] is False
        assessment = brief["activity_assessments"][0]
        assert assessment["category"] == "joint_decline"
        assert assessment["threshold_percent"] == 5
        assert assessment["signals"]["two_month_streak"] is None
        assert assessment["scope"]["region_code"] == "11680"
        assert assessment["scope"]["age_code"] == "20G"
        assert assessment["operational_alert_allowed"] is False
        assert assessment["reasons"]
        assert activity["signal_assessments"] == brief["activity_assessments"]
        assert brief["model_called"] is False
        version = saved["executions"][0]["versions"]
        assert version["i5_manifest_sha256"] and version["final_model_manifest_sha256"]
        assert version["signal_rule_version"] == assessment["rule_version"]


@pytest.mark.parametrize("changes", [
    {"city": "부산"}, {"region_code": "11680"}, {"region_code": "1199999999"},
    {"measurement_as_of": "2024-12"},
])
def test_invalid_investigation_scope_is_rejected(evidence_settings, changes):
    with TestClient(create_app(evidence_settings)) as client:
        assert client.post("/api/investigations", json=request(**changes)).status_code == 422


def test_activity_api_requires_exact_original_dimensions(evidence_settings):
    with TestClient(create_app(evidence_settings)) as client:
        query = {"region_code": "11680", "month": "202512", "sex_code": "MAN", "age_code": "20G"}
        result = client.get("/api/investigations/evidence/activity", params=query)
        assert result.status_code == 200, result.text
        assert result.json()["records"][0]["behavior"] == behavior()
        for mutation in ({"region_code": "1168051000"}, {"sex_code": "all"}, {"age_code": "20-39"},
                         {"month": "2025-12"}, {"region_code": "99999"}, {"dong_code": "1168051000"}):
            assert client.get("/api/investigations/evidence/activity", params={**query, **mutation}).status_code == 422
        missing = client.get("/api/investigations/evidence/activity", params={**query, "sex_code": "WMAN"})
        assert missing.json()["records"] == []
        assert missing.json()["quality_status"] == "hold"
        assert missing.json()["operational_alert_allowed"] is False


def test_missing_activity_bundle_is_explicit_failure(evidence_settings, tmp_path):
    settings = replace(evidence_settings, final_model_bundle=tmp_path / "missing")
    with TestClient(create_app(settings)) as client:
        result = client.get("/api/investigations/evidence/activity", params={
            "region_code": "11680", "month": "202512", "sex_code": "MAN", "age_code": "20G"})
        assert result.status_code == 503
        assert str(tmp_path) not in result.text
        assert "records" not in result.json()


def test_tampered_output_is_not_served_or_accepted(evidence_settings):
    tools = InvestigationTools(DEFAULT_RESEARCH, DEFAULT_SUPPORT, i5_bundle=evidence_settings.i5_bundle_path)
    measurements = tools.measurements(request(), "integrity-test")
    original = next(e for e in measurements["evidence"] if e["source_id"] == "national_i5_population_reference")
    path = evidence_settings.i5_bundle_path / "dong_isolation_index.jsonl"
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="hash_mismatch"):
        tools.get_survey_area_isolation_index(dong_code="1168051000", month="202512")
    changes = tools.recheck({"request": request(), "evidence": [original],
                            "briefs": [{"brief_id": "test-brief", "evidence_ids": [original["evidence_id"]]}]})
    assert changes[0]["status"] == "unavailable"
    assert changes[0]["affected_brief_ids"] == ["test-brief"]


def test_source_change_during_measurement_read_is_rejected(evidence_settings, monkeypatch):
    tools = InvestigationTools(DEFAULT_RESEARCH, DEFAULT_SUPPORT, i5_bundle=evidence_settings.i5_bundle_path)
    original = tools.get_survey_area_isolation_index
    changed = False

    def replace_manifest(**filters):
        nonlocal changed
        result = original(**filters)
        if not changed:
            path = evidence_settings.i5_bundle_path / "manifest.json"
            path.write_bytes(path.read_bytes() + b"\n")
            changed = True
        return result

    monkeypatch.setattr(tools, "get_survey_area_isolation_index", replace_manifest)
    # The verified region registry may detect the changed manifest before the
    # outer measurement guard. Both paths must reject the mixed-version read.
    with pytest.raises(ValueError, match=r"^(source_version_changed_during_measurement_read|bundle_changed_during_query)$"):
        tools.measurements(request(), "version-race")


def test_readiness_and_run_versions_use_provider_factory(evidence_settings, monkeypatch):
    class LocalProvider:
        model = "local-test-model"

        def readiness(self):
            return {"provider": "test_local", "ready": True}

    monkeypatch.setattr("app.investigation_api.create_provider", lambda: LocalProvider())
    with TestClient(create_app(evidence_settings)) as client:
        assert client.get("/api/investigations/readiness").json()["model"]["provider"] == "test_local"
        case = client.post("/api/investigations", json=request()).json()
        response = client.post(f"/api/investigations/{case['case_id']}/runs", json={
            "strategy": "rule_baseline", "expected_case_version": case["case_version"],
            "idempotency_key": "factory-version-run"})
        assert response.status_code == 202
        saved = client.get(f"/api/investigations/{case['case_id']}").json()
        assert saved["executions"][0]["versions"]["model"] == "local-test-model"
