import json

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("donghaeng_agent", reason="optional legacy agent tests: install requirements-legacy.txt")
pytest.importorskip("donghaeng_model", reason="optional legacy model_mori snapshot tests")

from app.config import Settings
from app.main import create_app


@pytest.fixture
def research_payload():
    # Explicitly synthetic fixture: never used as an application snapshot.
    return {
        "schema_version": "research-1.0", "mode": "research",
        "as_of": "2026-10-03T00:00:00+09:00", "disclaimer": "테스트용 합성 fixture",
        "survey": {"year": 2025, "area_level": "district", "population": "서울 테스트",
                   "formula": "I=0.5S+0.5L", "records": [{
                       "district": "테스트구", "age_group": "20대", "score": 30,
                       "ci_low": 20, "ci_high": 40, "support_absence": 20,
                       "loneliness": 40, "n": 100, "n_eff": 80, "quality": "test", "notes": [],
                   }]},
        "mobility": {"area_level": "dong", "periods": ["2025-07"], "records": [{
            "city": "서울", "district": "테스트구", "dong_code": "1168051000",
            "dong_name": "테스트동", "age_group": "20대", "period": "2025-07",
            "flow_total": None, "change_pct": None, "quality": "missing", "notes": ["관측 부족"],
        }]}, "sources": [], "limitations": ["테스트용 합성 자료"],
    }


@pytest.fixture
def research_path(tmp_path, research_payload):
    path = tmp_path / "research.json"
    path.write_text(json.dumps(research_payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_research_read_keeps_measurement_and_missingness(research_path):
    with TestClient(create_app(Settings(mode="research", research_path=research_path))) as client:
        assert client.get("/api/health").json()["mode"] == "research"
        snapshot = client.get("/api/research").json()
        assert snapshot["survey"]["records"][0]["score"] == 30
        assert snapshot["mobility"]["records"][0]["flow_total"] is None
        assert snapshot["mode"] == "research"
        assert "policies" not in snapshot


def test_research_never_runs_fake_spike_or_policy(research_path, dataset_path):
    with TestClient(create_app(Settings(mode="research", research_path=research_path, dataset_path=dataset_path))) as client:
        for scenario in ["normal", "risk_spike", "missing_data", "expired_policy", "no_policy"]:
            response = client.post("/api/runs", json={"district_code": "demo-001", "scenario": scenario})
            assert response.status_code == 409
        assert client.get("/api/districts").status_code == 409
        assert client.get("/api/runs/any").status_code == 409


@pytest.mark.parametrize("mutation", ["duplicate", "bad_interval", "wrong_weights", "missing_change", "mock"])
def test_invalid_research_snapshot_fails_without_mock_fallback(research_path, research_payload, mutation):
    if mutation == "duplicate":
        research_payload["survey"]["records"].append(research_payload["survey"]["records"][0])
    elif mutation == "bad_interval":
        research_payload["survey"]["records"][0]["ci_low"] = 90
    elif mutation == "wrong_weights":
        research_payload["survey"]["records"][0]["score"] = 60
    elif mutation == "missing_change":
        research_payload["mobility"]["records"][0]["change_pct"] = -100
    else:
        research_payload["mode"] = "mock"
    research_path.write_text(json.dumps(research_payload, ensure_ascii=False), encoding="utf-8")
    with TestClient(create_app(Settings(mode="research", research_path=research_path))) as client:
        assert client.get("/api/research").status_code == 503


def test_missing_research_file_and_live_mode_are_explicit(tmp_path):
    with TestClient(create_app(Settings(mode="research", research_path=tmp_path / "absent.json"))) as client:
        assert client.get("/api/health").json()["snapshot_ready"] is False
        assert client.get("/api/research").status_code == 503
    with TestClient(create_app(Settings(mode="live"))) as client:
        assert client.get("/api/research").status_code == 501


@pytest.fixture
def behavior_payload(research_payload):
    research_payload["paper_behavior_index"] = {
        "area_level": "dong", "period": "2025-12", "population": "20~39세 합성 테스트",
        "method": "테스트용 발표 가중치 재적용", "normalization": "테스트 기준월 공간 표준화",
        "full_paper_reproduction": False, "mapping_counts": {"mapped": 1, "held": 1},
        "records": [{
            "city": "서울", "district": "테스트구", "dong_code": "1168051000",
            "dong_name": "테스트동", "age_group": "20~39세", "period": "2025-12", "score": 35,
            "dimensions": {"economic_instability": 20, "social_activity": 30,
                           "isolated_lifestyle": 40, "social_relations": 50},
            "quality": "test", "notes": ["합성 테스트 자료"],
        }],
    }
    return research_payload


def test_behavior_read_preserves_fixed_month_population_and_audit_metadata(tmp_path, behavior_payload):
    path = tmp_path / "behavior.json"
    path.write_text(json.dumps(behavior_payload, ensure_ascii=False), encoding="utf-8")
    with TestClient(create_app(Settings(mode="research", research_path=path))) as client:
        response = client.get("/api/research")
        assert response.status_code == 200
        result = response.json()["paper_behavior_index"]
        assert result["records"][0]["age_group"] == "20~39세"
        assert result["records"][0]["period"] == "2025-12"
        assert result["records"][0]["score"] == 35
        assert result["mapping_counts"] == {"mapped": 1, "held": 1}
        assert result["full_paper_reproduction"] is False


@pytest.mark.parametrize("mutation", ["duplicate", "period", "age", "incomplete", "out_of_range", "full_reproduction"])
def test_behavior_invalid_contract_fails_explicitly(tmp_path, behavior_payload, mutation):
    index = behavior_payload["paper_behavior_index"]
    row = index["records"][0]
    if mutation == "duplicate":
        index["records"].append(row)
    elif mutation == "period":
        row["period"] = "2025-08"
    elif mutation == "age":
        row["age_group"] = "20대"
    elif mutation == "incomplete":
        row["dimensions"]["social_relations"] = None
    elif mutation == "out_of_range":
        row["score"] = 101
    else:
        index["full_paper_reproduction"] = True
    path = tmp_path / "bad-behavior.json"
    path.write_text(json.dumps(behavior_payload, ensure_ascii=False), encoding="utf-8")
    with TestClient(create_app(Settings(mode="research", research_path=path))) as client:
        assert client.get("/api/research").status_code == 503


def test_behavior_withheld_dimensions_are_not_zero_filled(tmp_path, behavior_payload):
    row = behavior_payload["paper_behavior_index"]["records"][0]
    row["score"] = None
    row["dimensions"]["social_relations"] = None
    row["quality"] = "missing"
    path = tmp_path / "held-behavior.json"
    path.write_text(json.dumps(behavior_payload, ensure_ascii=False), encoding="utf-8")
    with TestClient(create_app(Settings(mode="research", research_path=path))) as client:
        response = client.get("/api/research")
        assert response.status_code == 200
        result = response.json()["paper_behavior_index"]["records"][0]
        assert result["score"] is None
        assert result["dimensions"]["social_relations"] is None
        assert result["dimensions"]["economic_instability"] == 20
