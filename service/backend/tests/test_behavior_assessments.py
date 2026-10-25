"""The read-only activity API uses the same assessment as saved reports."""
from collections import Counter
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.main import create_app
from donghaeng_agent.signal_rules import SIGNAL_RULE_VERSION
from test_final_model import behavior, survey_row, write_bundle


ROOT = Path(__file__).resolve().parents[3]


def bundle_settings(tmp_path, rows=None):
    activity = write_bundle(tmp_path / "activity", {
        "behavior_evidence.jsonl": rows if rows is not None else [behavior("202511"), behavior()],
        "historical_reference.jsonl": [],
    }, "activity")
    survey = write_bundle(tmp_path / "survey", {
        "sgg_isolation_index.jsonl": [survey_row()], "dong_isolation_index.jsonl": [],
    }, "survey")
    return Settings(final_model_bundle=activity, survey_area_bundle=survey,
                    investigation_db_path=tmp_path / "investigations.sqlite3")


def actual_settings(tmp_path):
    return Settings(final_model_bundle=ROOT / "bundles/activity",
                    survey_area_bundle=ROOT / "model/data/i5/national_2025",
                    investigation_db_path=tmp_path / "investigations.sqlite3")


def test_get_all_real_assessments_is_read_only_and_preserves_originals(tmp_path, monkeypatch):
    from donghaeng_agent.analysis_report_store import AnalysisReportStore
    import donghaeng_agent.provider as provider

    def forbidden(*args, **kwargs):
        raise AssertionError("GET must not save a report or invoke a model")

    monkeypatch.setattr(AnalysisReportStore, "save", forbidden)
    monkeypatch.setattr(provider, "create_provider", forbidden)
    settings = actual_settings(tmp_path)
    root = settings.final_model_bundle
    rows = [json.loads(line) for line in (root / "behavior_evidence.jsonl").read_text().splitlines()]
    histories = {row["record_id"]: row for row in map(json.loads, (root / "historical_reference.jsonl").read_text().splitlines())}
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/model/behavior?limit=100")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["total_matched"] == 100 and result["next_offset"] is None
        assert result["classification_rule"] == {"rule_version": SIGNAL_RULE_VERSION, "threshold_percent": 5}
        assert result["manifest_sha256"] == hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
        assert [item["behavior"] for item in result["records"]] == rows
        counts = Counter(item["activity_assessment"]["category"] for item in result["records"])
        assert counts == {"no_threshold_decline": 70, "single_source_decline": 19, "joint_decline": 10, "priority_review": 1}
        for item in result["records"]:
            assert item["historical_reference"] == histories.get(item["behavior"]["historical_reference_id"])
            assert item["activity_assessment"]["operational_alert_allowed"] is False
        assert client.get("/api/model/analysis-reports").json()["total_matched"] == 0


def test_get_and_post_report_have_identical_assessment_evidence(tmp_path):
    settings = actual_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        scope = {"region_code": "51110", "month": "202510"}
        response = client.get("/api/model/behavior", params={**scope, "limit": 100})
        assert response.status_code == 200, response.text
        result = response.json()
        saved_response = client.post("/api/model/analysis-reports", json=scope)
        assert saved_response.status_code == 200, saved_response.text
        report = saved_response.json()
        assert result["manifest_sha256"] == report["provenance"]["activity"]["manifest_sha256"]
        saved = {(item["scope"]["sex_code"], item["scope"]["age_code"]): item for item in report["assessments"]}
        for record in result["records"]:
            assessment = record["activity_assessment"]
            key = assessment["scope"]
            saved_assessment = saved[(key["sex_code"], key["age_code"])]
            assert assessment == {field: saved_assessment[field] for field in assessment}
        priority = next(item["activity_assessment"] for item in result["records"] if item["activity_assessment"]["category"] == "priority_review")
        assert priority["scope"]["sex_code"] == "MAN" and priority["scope"]["age_code"] == "20G"
        assert priority["signals"]["two_month_streak"] is False
        assert priority["signals"]["robust_candidate"] is True
        assert priority["robust_evidence"]["cutoff"] == 3.5
        assert priority["robust_evidence"]["sources"]["skt"]["z"] > 3.5
        assert "SKT_SEP_OCT_BOUNDARY_VERSION_CHANGED" in priority["quality_reasons"]


def test_continuity_reads_previous_month_outside_filter_and_page(tmp_path):
    previous, current = behavior("202511"), behavior()
    settings = bundle_settings(tmp_path, [previous, current])
    with TestClient(create_app(settings)) as client:
        filtered = client.get("/api/model/behavior?month=202512&sex_code=MAN&age_code=20G&limit=1").json()
        paged = client.get("/api/model/behavior?limit=1&offset=1").json()
        first, second = filtered["records"][0]["activity_assessment"], paged["records"][0]["activity_assessment"]
        assert first == second
        assert first["category"] == "priority_review" and first["signals"]["two_month_streak"] is True
        assert first["sources"]["skt"]["previous_change_percent"] == -20
        assert first["evidence_ids"] == [current["record_id"], previous["record_id"]]


@pytest.mark.parametrize("skt,card,expected", [
    (-5, -4.999999, "single_source_decline"), (-5, -5, "joint_decline"),
    (-4.99, -0.2, "no_threshold_decline"), (-5, None, "single_source_decline"),
    (None, None, "assessment_hold"),
])
def test_threshold_boundary_and_missing_source_are_preserved(tmp_path, skt, card, expected):
    row = behavior()
    for name, change in (("skt", skt), ("card_a", card)):
        source = row["sources"][name]
        source["change_percent"] = change
        if change is None:
            source.update(calculation_status="unavailable", reason="test_missing", direction=None,
                          activity_daily=None, previous_activity_daily=None, Q=None, H=None, T=None)
            row["comparison_status"] = "unavailable"
    settings = bundle_settings(tmp_path, [row])
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/model/behavior?month=202512")
        assert response.status_code == 200, response.text
        assessment = response.json()["records"][0]["activity_assessment"]
        assert assessment["category"] == expected
        assert assessment["sources"]["skt"]["change_percent"] == skt
        assert assessment["sources"]["card_a"]["change_percent"] == card
        if card is None:
            assert assessment["sources"]["card_a"]["threshold_met"] is None


def test_original_candidate_filter_stays_independent_of_new_category(tmp_path):
    row = behavior()
    row["sources"]["skt"]["change_percent"] = -0.1
    row["sources"]["card_a"]["change_percent"] = -0.2
    assert row["rule"]["candidate"] is True
    settings = bundle_settings(tmp_path, [row])
    with TestClient(create_app(settings)) as client:
        result = client.get("/api/model/behavior?candidate_only=true").json()
        assert result["total_matched"] == 1
        assert result["records"][0]["behavior"]["rule"]["candidate"] is True
        assert result["records"][0]["activity_assessment"]["category"] == "no_threshold_decline"


def test_missing_rows_remain_empty_and_tampering_fails_closed(tmp_path):
    settings = bundle_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        missing = client.get("/api/model/behavior?region_code=51110").json()
        assert missing["records"] == [] and missing["total_matched"] == 0
        assert missing["classification_rule"]["threshold_percent"] == 5
        path = settings.final_model_bundle / "behavior_evidence.jsonl"
        path.write_text(path.read_text() + "\n")
        result = client.get("/api/model/behavior")
        assert result.status_code == 503 and result.json()["detail"]["code"] == "bundle_invalid"
