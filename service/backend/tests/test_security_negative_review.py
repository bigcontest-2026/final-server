"""Independent negative review of institution and loopback access boundaries."""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from test_investigation_evidence import evidence_settings, request
from test_security import institution, signing_key, headers


def test_local_development_rejects_nonlocal_host_even_from_loopback(evidence_settings):
    # DNS rebinding can connect to loopback while retaining an attacker Host.
    with TestClient(create_app(evidence_settings), client=("127.0.0.1", 50000),
                    base_url="http://attacker.invalid") as client:
        response = client.get("/api/security/context")
        assert response.status_code == 403


@pytest.mark.parametrize("proxy_header", ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host"])
def test_local_development_rejects_proxy_forwarding_headers(evidence_settings, proxy_header):
    with TestClient(create_app(evidence_settings), client=("127.0.0.1", 50000),
                    base_url="http://127.0.0.1") as client:
        response = client.get("/api/security/context", headers={proxy_header: "203.0.113.10"})
        assert response.status_code == 403


def test_local_development_rejects_cross_origin_simple_json_post(evidence_settings):
    # A browser can send a Blob with no Content-Type without CORS preflight.
    # CORS response headers alone do not prevent the server mutating state.
    application = create_app(evidence_settings)
    with TestClient(application, client=("127.0.0.1", 50000),
                    base_url="http://127.0.0.1") as client:
        response = client.post("/api/investigations", content=json.dumps(request()),
                               headers={"Origin": "https://attacker.invalid"})
        # Either the access boundary or strict JSON content-type validation can
        # reject this; the important property is no new case is created.
        assert response.status_code in (403, 422)
        assert application.state.investigation_repository.list() == []


def test_followup_actor_comes_from_verified_identity(institution, signing_key):
    application = create_app(institution)
    with TestClient(application) as client:
        investigator = headers(signing_key, ["read", "investigate"])
        created = client.post("/api/investigations", json=request(), headers=investigator).json()
        cid = created["case_id"]
        response = client.post(f"/api/investigations/{cid}/runs", headers=investigator, json={
            "strategy": "rule_baseline", "expected_case_version": created["case_version"],
            "idempotency_key": "negative-review-run"})
        assert response.status_code == 202
        application.state.investigation_worker.drain_once()
        saved = client.get(f"/api/investigations/{cid}", headers=investigator).json()
        response = client.post(f"/api/investigations/{cid}/followups", headers=headers(signing_key, ["review"], sub="followup-reviewer"), json={
            "status": "referral_reported", "evidence_ids": [saved["evidence"][0]["evidence_id"]],
            "expected_case_version": saved["case_version"], "note": "격리된 검증용 후속 보고",
            "idempotency_key": "negative-review-followup"})
        assert response.status_code == 200
        followup = client.get(f"/api/investigations/{cid}", headers=investigator).json()["followups"][0]
        assert followup["actor"] == {"subject": "followup-reviewer", "tenant_id": "institution-a", "auth_mode": "institution"}


def test_strict_audience_and_duplicate_authorization_are_rejected(institution, signing_key):
    with TestClient(create_app(institution)) as client:
        assert client.get("/api/security/context", headers=headers(signing_key, aud=["hermes-test", "other"])).status_code == 401
        token = headers(signing_key)["Authorization"]
        assert client.get("/api/security/context", headers=[("Authorization", token), ("Authorization", token)]).status_code == 401


def test_nonfinite_signed_token_times_fail_closed(institution, signing_key):
    with TestClient(create_app(institution), raise_server_exceptions=False) as client:
        response = client.get("/api/security/context", headers=headers(signing_key, exp=float("inf")))
        assert response.status_code == 401
