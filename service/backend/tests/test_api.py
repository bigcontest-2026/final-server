import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("donghaeng_agent", reason="optional legacy agent tests: install requirements-legacy.txt")
pytest.importorskip("donghaeng_model", reason="optional legacy model_mori snapshot tests")

from app.config import Settings
from app.main import create_app


def test_health_and_dataset(client):
    assert client.get("/api/health").json() == {"status": "ok", "mode": "mock", "graph": "langgraph"}
    body = client.get("/api/districts").json()
    assert body["mode"] == "mock" and body["districts"][0]["code"] == "demo-001"


@pytest.mark.parametrize("scenario", ["normal", "risk_spike", "missing_data", "expired_policy", "no_policy"])
def test_run_and_fetch(client, scenario):
    response = client.post("/api/runs", json={"district_code": "demo-001", "scenario": scenario})
    assert response.status_code == 200
    result = response.json()
    assert client.get(f"/api/runs/{result['run_id']}").json() == result
    assert result["mode"] == "mock" and result["scenario"] == scenario


@pytest.mark.parametrize("body,status", [
    ({"district_code": "unknown"}, 404), ({"district_code": ""}, 422),
    ({"district_code": "demo-001", "scenario": "fake"}, 422), ({}, 422),
    ({"district_code": "demo-001", "mode": "live"}, 422),
])
def test_invalid_requests(client, body, status):
    assert client.post("/api/runs", json=body).status_code == status
    assert client.get("/api/runs/not-found").status_code == 404


def test_lazy_missing_dataset(tmp_path):
    with TestClient(create_app(Settings(mode="mock", dataset_path=tmp_path / "absent.json"))) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/districts").status_code == 503
        assert client.post("/api/runs", json={"district_code": "demo-001"}).status_code == 503


@pytest.mark.parametrize("bad", ["{", json.dumps({"mode": "live"}), "null"])
def test_invalid_dataset_is_503(dataset_path, bad):
    dataset_path.write_text(bad)
    with TestClient(create_app(Settings(mode="mock", dataset_path=dataset_path))) as client:
        assert client.get("/api/districts").status_code == 503


def test_live_never_falls_back_to_mock(dataset_path):
    with TestClient(create_app(Settings(mode="live", dataset_path=dataset_path))) as client:
        for endpoint in ("/api/health", "/api/districts", "/api/runs/any"):
            assert client.get(endpoint).status_code == 501
        response = client.post("/api/runs", json={"district_code": "demo-001"})
        assert response.status_code == 501
        assert "자동 전환하지 않습니다" in response.json()["detail"]


def test_cors_configured_origins_only(dataset_path):
    with TestClient(create_app(Settings(mode="mock", dataset_path=dataset_path, cors_origins=("https://demo.example",)))) as client:
        allowed = client.options("/api/runs", headers={"Origin": "https://demo.example", "Access-Control-Request-Method": "POST"})
        assert allowed.headers["access-control-allow-origin"] == "https://demo.example"
        denied = client.options("/api/runs", headers={"Origin": "https://other.example", "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in denied.headers


def test_capacity_eviction_and_process_local_storage(dataset_path):
    settings = Settings(mode="mock", dataset_path=dataset_path, max_runs=2)
    with TestClient(create_app(settings)) as client:
        ids = [client.post("/api/runs", json={"district_code": "demo-001"}).json()["run_id"] for _ in range(3)]
        assert client.get(f"/api/runs/{ids[0]}").status_code == 404
        assert client.get(f"/api/runs/{ids[1]}").status_code == 200
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(f"/api/runs/{ids[2]}").status_code == 404


def test_concurrent_requests_keep_separate_state(client):
    scenarios = ["normal", "risk_spike", "missing_data", "expired_policy", "no_policy"] * 3
    def submit(scenario):
        response = client.post("/api/runs", json={"district_code": "demo-001", "scenario": scenario})
        assert response.status_code == 200
        return response.json()
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(submit, scenarios))
    assert len({r["run_id"] for r in results}) == len(scenarios)
    for result, scenario in zip(results, scenarios):
        assert result["scenario"] == scenario
        assert result["risk_score"] == (None if scenario == "missing_data" else 85 if scenario == "risk_spike" else 55)
