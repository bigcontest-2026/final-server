"""Service lifecycle wiring only; no external collector is started or contacted."""
from dataclasses import replace

from fastapi.testclient import TestClient

from app.main import create_app
from test_investigation_evidence import evidence_settings


def test_tracing_initializes_before_worker_and_closes_after_worker(evidence_settings, monkeypatch):
    import app.legacy_main as legacy

    events = []
    monkeypatch.setattr(legacy, "init_observability", lambda: events.append("init"))
    monkeypatch.setattr(legacy, "flush_observability", lambda: events.append("flush"))
    monkeypatch.setattr(legacy, "shutdown_observability", lambda: events.append("shutdown"))
    monkeypatch.setattr("app.investigation_api.observability_status", lambda: {"enabled": True, "status": "configured"})
    application = create_app(replace(evidence_settings, investigation_worker_enabled=True))
    worker = application.state.investigation_worker
    original_close = worker.close
    monkeypatch.setattr(worker, "start", lambda: events.append("start"))

    def close():
        events.append("close")
        original_close()

    monkeypatch.setattr(worker, "close", close)
    with TestClient(application) as client:
        assert events == ["init", "start"]
        assert client.get("/api/investigations/readiness").json()["observability"]["status"] == "configured"
    assert events == ["init", "start", "close", "flush", "shutdown"]


def test_default_service_tracing_remains_disabled(evidence_settings):
    with TestClient(create_app(evidence_settings)) as client:
        state = client.get("/api/investigations/readiness").json()["observability"]
        assert state["enabled"] is False and state["status"] == "disabled"
