"""Expanded C3 fixtures pass through the actual model and HTTP contracts."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from test_final_model import behavior, settings, write_bundle


@pytest.mark.parametrize(("age", "c3_age"), [("40G", "40"), ("50G", "50"), ("60GU", "60+")])
def test_expanded_age_returns_its_own_c3_context(settings, age, c3_age):
    base = Path(__file__).resolve().parents[3] / "bundles/activity/historical_reference.jsonl"
    history = next(json.loads(line) for line in base.read_text().splitlines()
                   if json.loads(line)["view"] == "age_sex")
    history.update(record_id="C3:expanded-fixture", region="강남구", month="2025-12",
                   age=c3_age, sex="남성", calendar_type="all")
    row = behavior(age=age)
    row["historical_reference_id"] = history["record_id"]
    write_bundle(settings.final_model_bundle, {"behavior_evidence.jsonl": [row],
                 "historical_reference.jsonl": [history]}, "activity")
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/model/behavior", params={"age_code": age})
    assert response.status_code == 200
    returned = response.json()["records"][0]
    assert returned["historical_reference"]["age"] == c3_age
    assert returned["behavior"]["sources"] == row["sources"]


@pytest.mark.parametrize("wrong", ["age", "month", "sex", "region", "missing"])
def test_mismatched_c3_reference_is_rejected(settings, wrong):
    base = Path(__file__).resolve().parents[3] / "bundles/activity/historical_reference.jsonl"
    history = next(json.loads(line) for line in base.read_text().splitlines()
                   if json.loads(line)["view"] == "age_sex")
    history.update(record_id="C3:mismatch", region="강남구", month="2025-12",
                   age="60+", sex="남성", calendar_type="all")
    if wrong == "age": history["age"] = "20"
    elif wrong == "month": history["month"] = "2025-11"
    elif wrong == "sex": history["sex"] = "여성"
    elif wrong == "region": history["region"] = "춘천시"
    row = behavior(age="60GU")
    row["historical_reference_id"] = history["record_id"]
    if wrong == "missing": row["historical_reference_id"] = "C3:nonexistent"
    write_bundle(settings.final_model_bundle, {"behavior_evidence.jsonl": [row],
                 "historical_reference.jsonl": [history]}, "activity")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/model/behavior?age_code=60GU").status_code == 503
