"""Actual captured-source integration tests. No fake policy enters application data."""
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("donghaeng_agent", reason="optional legacy agent tests: install requirements-legacy.txt")
pytest.importorskip("donghaeng_model", reason="optional legacy model_mori snapshot tests")

from app.config import DEFAULT_CAPTURE_ROOT, DEFAULT_RESEARCH, DEFAULT_SUPPORT, Settings
from donghaeng_agent.tools import InvestigationTools
from app.main import create_app

# Full official captures are local resources; ordinary Git checkouts exclude them.
pytestmark = pytest.mark.skipif(
    not all((DEFAULT_CAPTURE_ROOT / name).is_file() for name in (
        "raw/seoul-youth-isolation.html", "raw/seoul-isolated-household.html",
        "raw/chuncheon-mental-health.html", "raw/seoul-family-training-ended.html",
        "text/seoul-youth-isolation.txt", "text/seoul-isolated-household.txt",
        "text/chuncheon-mental-health.txt", "text/seoul-family-training-ended.txt",
    )),
    reason="official capture integration requires local agent/evidence/support resources",
)


def request(key="create-real-case"):
    return {"region_code": "1168066000", "age_group": "20대", "city": "서울", "purpose": "공식 지원 조건 검토",
            "measurement_as_of": "2025-12", "support_as_of": "2026-10-05", "sk_mode": "provided", "idempotency_key": key}


@pytest.fixture
def settings(tmp_path):
    return Settings(mode="research", investigation_db_path=tmp_path / "cases.sqlite3",
                    investigation_checkpoint_path=tmp_path / "checkpoints.sqlite3", investigation_worker_enabled=False)


def start(client, application, payload=None, strategy="rule_baseline"):
    case = client.post("/api/investigations", json=payload or request()).json()
    response = client.post(f"/api/investigations/{case['case_id']}/runs", json={"strategy": strategy, "expected_case_version": case["case_version"], "idempotency_key": "start-real-case"})
    assert response.status_code == 202, response.json()
    application.state.investigation_worker.drain_once()
    result = client.get(f"/api/investigations/{case['case_id']}").json()
    return result


def test_real_source_to_brief_review_and_restart(settings):
    application = create_app(settings)
    with TestClient(application) as client:
        case = start(client, application)
        assert case["status"] == "awaiting_review", case["executions"]
        brief = case["briefs"][0]
        assert brief["model_called"] is False
        assert any(c["id"] == "seoul-youth-isolation" for c in brief["support_candidates"])
        assert any(c["eligibility_status"] == "excluded" for c in brief["excluded_candidates"])
        assert all(c["eligibility_status"] == "needs_confirmation" for c in brief["support_candidates"])
        assert not any(c["currently_accepting_confirmed"] for c in brief["support_candidates"])
        calls_before = len(case["executions"][0]["tool_calls"])
        case_id = case["case_id"]
    # A fresh app, connection and graph restore the real SQLite interrupt.
    application = create_app(settings)
    with TestClient(application) as client:
        case = client.get(f"/api/investigations/{case_id}").json()
        response = client.post(f"/api/investigations/{case_id}/reviews", json={"brief_id": brief["brief_id"], "brief_version": brief["brief_version"],
            "expected_case_version": case["case_version"], "decision": "accept", "reason": "개발자 영속성 확인", "reviewer_type": "developer_verification", "idempotency_key": "review-real-case"})
        assert response.status_code == 202, response.json()
        application.state.investigation_worker.drain_once()
        result = client.get(f"/api/investigations/{case_id}").json()
        assert result["status"] == "reviewed", result["executions"]
        assert result["executions"][0]["status"] == "succeeded"
        assert len(result["executions"][0]["tool_calls"]) == calls_before
        assert len(result["briefs"]) == 1
        assert result["reviews"][0]["outcome_scope"] == "brief_review_only"


def test_idempotency_version_conflict_and_one_execution(settings):
    application = create_app(settings)
    with TestClient(application) as client:
        a = client.post("/api/investigations", json=request())
        b = client.post("/api/investigations", json=request())
        assert a.json()["case_id"] == b.json()["case_id"]
        different = {**request(), "age_group": "30대"}
        assert client.post("/api/investigations", json=different).status_code == 409
        cid = a.json()["case_id"]
        command = {"strategy": "rule_baseline", "expected_case_version": 1, "idempotency_key": "run-deduplicate"}
        x = client.post(f"/api/investigations/{cid}/runs", json=command)
        y = client.post(f"/api/investigations/{cid}/runs", json=command)
        assert x.json() == y.json()
        assert client.post(f"/api/investigations/{cid}/runs", json={**command, "idempotency_key": "run-another-one"}).status_code == 409
        assert client.post(f"/api/investigations/{cid}/runs", json={**command, "expected_case_version": 2, "idempotency_key": "run-another-one"}).json()["detail"]["code"] == "active_execution"


@pytest.mark.parametrize("strategy", ["agent", "single_search"])
def test_missing_model_key_is_failed_not_mock(settings, monkeypatch, strategy):
    monkeypatch.setenv("DONGHAENG_PROVIDER", "openai_responses")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    application = create_app(settings)
    with TestClient(application) as client:
        case = start(client, application, strategy=strategy)
        assert case["executions"][0]["status"] == "failed"
        assert case["executions"][0]["error"]["code"] == "model_key_missing"
        assert case["briefs"] == []
        assert client.get("/api/investigations/readiness").json()["model"]["key_present"] is False


def test_sk_exclusion_and_effective_window_mutation():
    tools = InvestigationTools(DEFAULT_RESEARCH, DEFAULT_SUPPORT)
    provided = tools.measurements(request(), "provided")
    excluded = tools.measurements({**request(), "sk_mode": "excluded"}, "excluded")
    changed = tools.measurements({**request(), "sk_mode": "quality_mutation"}, "changed")
    assert all(o["kind"] != "sk" for o in excluded["observations"])
    assert "flow_total" not in json.dumps(excluded)
    real_sk = next(o for o in provided["observations"] if o["kind"] == "sk")
    changed_sk = next(o for o in changed["observations"] if o["kind"] == "sk")
    assert real_sk["data"]["selected"]["flow_total"] is not None
    assert changed_sk["data"]["selected"]["flow_total"] is None
    assert changed_sk["status"] == "held"
    assert changed_sk["data"]["window"][-2] == real_sk["data"]["window"][-2]
    assert tools.quality(changed)["questions"]
    baseline_rows = [r for r in real_sk["data"]["window"] if r["period"] in ("2025-07", "2025-08")]
    assert real_sk["data"]["baseline_numeric"] == sum(r["flow_total"] for r in baseline_rows) / 2
    assert real_sk["data"]["baseline_complete"] is True


def test_chuncheon_does_not_borrow_seoul_and_wrong_region_rejected(settings):
    application = create_app(settings)
    with TestClient(application) as client:
        payload = {**request("chuncheon-case"), "region_code": "5111038000", "city": "춘천"}
        # The exact real Sabuk code is obtained from the snapshot, never guessed.
        snapshot = json.loads(DEFAULT_RESEARCH.read_text())
        payload["region_code"] = next(r["dong_code"] for r in snapshot["mobility"]["records"] if r["city"] == "춘천" and r["dong_name"] == "사북면")
        case = start(client, application, payload)
        brief = case["briefs"][0]
        survey = next(o for o in brief["observations"] if o["kind"] == "survey")
        assert survey["data"] is None and survey["status"] == "held"
        assert all(c["id"] != "seoul-youth-isolation" for c in brief["support_candidates"])
        assert client.post("/api/investigations", json={**request("wrong-city-case"), "city": "춘천"}).status_code == 422


def test_expired_job_lease_resumes_durable_mid_execution_checkpoint(settings):
    application = create_app(settings)
    with TestClient(application) as client:
        case = client.post("/api/investigations", json=request()).json()
        repo, worker = application.state.investigation_repository, application.state.investigation_worker
        command = {"strategy": "rule_baseline", "expected_case_version": case["case_version"], "idempotency_key": "mid-stage-run"}
        response = client.post(f"/api/investigations/{case['case_id']}/runs", json=command).json()
        job = repo.claim()
        worker.graph.invoke({"case_id": case["case_id"], "execution_id": response["execution_id"]}, {"configurable": {"thread_id": response["execution_id"]}}, interrupt_after=["load"])
        # Simulate process loss between checkpoint commit and job completion.
        with repo.connection() as db:
            db.execute("UPDATE jobs SET lease_until=0 WHERE job_id=?", (job["job_id"],))
        cid = case["case_id"]
    application = create_app(settings)
    with TestClient(application) as client:
        assert application.state.investigation_worker.drain_once()
        result = client.get(f"/api/investigations/{cid}").json()
        assert result["status"] == "awaiting_review"
        assert len(result["briefs"]) == 1
        assert sum(t["tool_name"] == "get_measurements" for t in result["executions"][0]["tool_calls"]) == 1


def test_failed_same_execution_can_retry_after_connection_condition_changes(settings, monkeypatch):
    monkeypatch.setenv("DONGHAENG_PROVIDER", "openai_responses")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    application = create_app(settings)
    with TestClient(application) as client:
        case = start(client, application, strategy="agent")
        eid = case["executions"][0]["execution_id"]
        command = {"execution_id": eid, "expected_case_version": case["case_version"], "idempotency_key": "retry-failed-model"}
        a = client.post(f"/api/investigations/{case['case_id']}/resume", json=command)
        b = client.post(f"/api/investigations/{case['case_id']}/resume", json=command)
        assert a.status_code == 202 and a.json() == b.json()
        application.state.investigation_worker.drain_once()
        result = client.get(f"/api/investigations/{case['case_id']}").json()
        assert len(result["executions"]) == 1
        assert result["executions"][0]["execution_id"] == eid
        assert result["executions"][0]["error"]["code"] == "model_key_missing"
        assert sum(t["tool_name"] == "get_measurements" for t in result["executions"][0]["tool_calls"]) == 1


def test_capture_hash_mismatch_and_modified_source_affect_prior_brief(settings, tmp_path):
    application = create_app(settings)
    with TestClient(application) as client:
        case = start(client, application)
        real_tools = application.state.investigation_worker.tools
        original_details = real_tools.details
        def changed_detail(candidate_id):
            result = original_details(candidate_id)
            if result["status"] == "ok":
                result["content_hash"] = "f" * 64
            return result
        real_tools.details = changed_detail
        response = client.post(f"/api/investigations/{case['case_id']}/recheck", json={"expected_case_version": case["case_version"], "idempotency_key": "recheck-modified"})
        assert response.status_code == 200
        assert response.json()["requires_new_run"] is True
        assert all(c["affected_brief_ids"] for c in response.json()["changes"])
        updated = client.get(f"/api/investigations/{case['case_id']}").json()
        assert updated["support_status"] == "stale"
        assert updated["briefs"][0] == case["briefs"][0]


def test_actual_capture_integrity_and_expiration():
    tools = InvestigationTools(DEFAULT_RESEARCH, DEFAULT_SUPPORT)
    manifest = tools.manifest()
    assert len(manifest["sources"]) >= 4
    ended = []
    for source in manifest["sources"]:
        detail = tools.details(source["id"])
        assert detail["status"] == "ok", detail
        check = tools.check_conditions(request(), detail)
        assert {c["id"] for c in source["conditions"]} <= {c["id"] for c in check["conditions"]}
        if source.get("period", {}).get("end") and source["period"]["end"] < "2026-10-05":
            ended.append(check)
    assert ended and all(x["excluded"] for x in ended)


def test_stale_metadata_or_extraction_blocks_accept(settings):
    application = create_app(settings)
    with TestClient(application) as client:
        case = start(client, application)
        tools = application.state.investigation_worker.tools
        original_details = tools.details
        def modified_metadata(cid):
            result = original_details(cid)
            if result["status"] == "ok":
                result["metadata_hash"] = "e" * 64
            return result
        tools.details = modified_metadata
        brief = case["briefs"][0]
        response = client.post(f"/api/investigations/{case['case_id']}/reviews", json={"brief_id": brief["brief_id"], "brief_version": brief["brief_version"], "expected_case_version": case["case_version"],
            "decision": "accept", "reason": "변경조건 수락 차단검증", "reviewer_type": "developer_verification", "idempotency_key": "stale-accept-block"})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "stale_support"
        changes = tools.recheck(case)
        official_source_ids = {source["id"] for source in tools.manifest()["sources"]}
        official_changes = [c for c in changes if c["source_id"] in official_source_ids]
        assert official_changes
        assert all("structured_conditions" in c["changed_fields"] for c in official_changes)
        assert all(c["status"] == "changed" for c in official_changes)


def test_corrupted_raw_capture_is_not_candidate(tmp_path):
    manifest = json.loads(DEFAULT_SUPPORT.read_text())
    first = manifest["sources"][0]
    damaged = tmp_path / "damaged.html"
    damaged.write_text("Controlled capture-integrity failure test. No policy facts.")
    first["capture_path"] = str(damaged)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
    tools = InvestigationTools(DEFAULT_RESEARCH, manifest_path)
    result = tools.details(first["id"])
    assert result["status"] == "failed"
    assert result["error"]["code"] == "capture_hash_mismatch"


def test_low_sample_score_never_leaves_published_aggregate_tool(tmp_path):
    payload = json.loads(DEFAULT_RESEARCH.read_text())
    row = next(r for r in payload["survey"]["records"] if r["district"] == "강남구" and r["age_group"] == "20대")
    row.update(n=5, n_eff=3, quality="insufficient_sample")
    path = tmp_path / "withheld_research_fixture.json"
    path.write_text(json.dumps(payload, ensure_ascii=False))
    tools = InvestigationTools(path, DEFAULT_SUPPORT)
    result = tools.measurements(request(), "withheld_test")
    survey = next(o for o in result["observations"] if o["kind"] == "survey")
    assert survey["status"] == "held"
    assert survey["data"]["score"] is None and survey["data"]["ci_low"] is None
    assert survey["data"]["n"] == 5 and survey["data"]["n_eff"] == 3
    ev = next(e for e in result["evidence"] if e["kind"] == "survey")
    assert ev["extracted_values"]["score"] is None


def test_research_snapshot_changed_recheck_affects_brief(settings, tmp_path):
    application = create_app(settings)
    with TestClient(application) as client:
        case = start(client, application)
        tools = application.state.investigation_worker.tools
        changed_path = tmp_path / "changed_snapshot.json"
        changed_path.write_bytes(DEFAULT_RESEARCH.read_bytes() + b"\n")
        tools.research_path = changed_path
        response = client.post(f"/api/investigations/{case['case_id']}/recheck", json={"expected_case_version": case["case_version"], "idempotency_key": "snapshot-version-recheck"})
        assert response.status_code == 200
        changed = [c for c in response.json()["changes"] if c["source_id"] == "published_research_snapshot"]
        assert changed and all(c["status"] == "changed" and c["affected_brief_ids"] for c in changed)
        updated = client.get(f"/api/investigations/{case['case_id']}").json()
        assert updated["measurement_status"] == "stale"


def test_actual_461_catalog_search_is_metadata_only(settings):
    application = create_app(settings)
    with TestClient(application) as client:
        response = client.get("/api/investigations/catalog/search", params={"query": "청년", "limit": 3})
        assert response.status_code == 200, response.json()
        result = response.json()
        assert result["actual_rows"] == 461
        assert result["items"] and len(result["items"]) <= 3
        assert result["verification_level"] == "metadata_only"
        assert all(x["detail_verified"] is False and x["eligibility_status"] == "unknown" for x in result["items"])
        assert client.get("/api/investigations/catalog/search", params={"limit": 101}).status_code == 422


def test_followup_cannot_mark_field_outcome_from_support_notice(settings):
    application = create_app(settings)
    with TestClient(application) as client:
        case = start(client, application)
        response = client.post(f"/api/investigations/{case['case_id']}/followups", json={"expected_case_version": case["case_version"], "status": "participation_confirmed", "evidence_ids": [case["evidence"][0]["evidence_id"]], "note": "공고가 참여 확인을 대신할 수 없음", "idempotency_key": "followup-refused"})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "field_evidence_required"
