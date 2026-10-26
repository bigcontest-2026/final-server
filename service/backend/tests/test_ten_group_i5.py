"""Published ten-group I5 through the service, report and agent boundaries."""
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import DEFAULT_I5_BUNDLE, DEFAULT_MODEL_ROOT, Settings
from app.final_model import FinalModel
from app.main import create_app

CONTRACT = "survey_area_isolation_index_10group_v1"
DISPLAY_VERSION = "i5_10group_sgg_202512_equal_weight_v1"
TEN_BUNDLE = DEFAULT_MODEL_ROOT / "data/i5/national_2025_10group"
LEGACY_BUNDLE = DEFAULT_MODEL_ROOT / "data/i5/national_2025"
MEAN, STD = 35.24084915654042, 0.8708522905523021


def test_default_i5_selects_ten_group_bundle():
    assert DEFAULT_I5_BUNDLE == TEN_BUNDLE
    assert FinalModel(Settings()).bundle_path("survey") == TEN_BUNDLE


@pytest.fixture
def settings(tmp_path):
    return Settings(survey_area_bundle=TEN_BUNDLE, final_model_bundle=DEFAULT_MODEL_ROOT.parent / "bundles/activity",
                    investigation_db_path=tmp_path / "cases.sqlite3", investigation_worker_enabled=False)


@pytest.mark.parametrize("code,raw", [("11680", 34.11978719425335), ("51110", 34.744958785034456)])
def test_api_serves_recomputed_ten_group_scores_and_matching_display(settings, code, raw):
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/model/survey-area", params={"region_code": code, "month": "202512"})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["contract_version"] == CONTRACT
        row = result["records"][0]
        assert row["i5_reference"] == raw
        assert len(row["group_populations"]) == len(row["group_weights"]) == len(row["group_profiles"]) == 10
        assert sum(row["group_populations"].values()) == row["population_20plus"]
        assert row["i5_reference"] == pytest.approx(sum(p["i5_reference"] * p["population_share"] for p in row["group_profiles"]))
        assert row["i5_display_score"] == pytest.approx(5 + (raw - MEAN) / STD)
        assert row["i5_display_reference_version"] == DISPLAY_VERSION
        assert row["local_survey_measured"] is False and result["operational_alert_allowed"] is False
        assert client.get("/api/model/status").json()["sources"]["survey"]["contract_version"] == CONTRACT


def test_province_filter_and_report_keep_ten_group_values(settings):
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/model/survey-area", params={"province_code": "11", "month": "202512", "limit": 500})
        assert response.status_code == 200, response.text
        rows = response.json()["records"]
        assert len(rows) == 25
        assert all(len(row["group_profiles"]) == 10 and row["i5_display_reference_version"] == DISPLAY_VERSION for row in rows)
        response = client.post("/api/model/analysis-reports", json={"region_code": "11680", "month": "202512"})
        assert response.status_code == 200, response.text
        report = response.json()
        i5 = report["context"]["i5"]["record"]
        assert i5["i5_reference"] == 34.11978719425335
        assert i5["i5_display_reference_version"] == DISPLAY_VERSION
        assert len(i5["group_profiles"]) == 10
        assert len(report["assessments"]) == len(i5["group_populations"]) == 10
        assert report["context"]["mois"]["changes_category"] is False


def test_agent_reads_ten_group_area_and_national_profiles(settings):
    from donghaeng_agent.tools import InvestigationTools
    tools = InvestigationTools(settings.research_path, settings.support_manifest_path,
                               i5_bundle=TEN_BUNDLE, final_model_bundle=settings.final_model_bundle)
    area = tools.get_survey_area_isolation_index(region_code="11680", month="202512")
    assert area["contract_version"] == CONTRACT
    assert len(area["records"][0]["group_profiles"]) == 10
    assert area["records"][0]["i5_display_reference_version"] == DISPLAY_VERSION
    national = tools.get_survey_mois_reference()
    assert national["table"] == "national_ten_group_profile"
    assert len(national["records"]) == 10
    assert national["operational_alert_allowed"] is False


def test_explicit_six_group_bundle_keeps_original_reference(settings):
    with TestClient(create_app(replace(settings, survey_area_bundle=LEGACY_BUNDLE))) as client:
        response = client.get("/api/model/survey-area?region_code=11680&month=202512")
        assert response.status_code == 200, response.text
        row = response.json()["records"][0]
        assert row["i5_reference"] == 34.12395682037197
        assert len(row["group_weights"]) == 6
        assert row["i5_display_reference_version"] == "i5_sgg_202512_equal_weight_v1"


def test_explicit_missing_ten_bundle_does_not_fall_back(settings, tmp_path):
    with TestClient(create_app(replace(settings, survey_area_bundle=tmp_path / "missing"))) as client:
        assert client.get("/api/model/survey-area").status_code == 503


def test_ten_group_welfare_reader_preserves_activity_rule(settings):
    from donghaeng_agent.welfare_pipeline import run
    result = run(i5_bundle=TEN_BUNDLE, activity_bundle=settings.final_model_bundle,
                 db_path="/unused", publication_id=None, region_code="11680", month="202508",
                 sex_code="MAN", age_code="20G", as_of="2026-10-26", research_context=True)
    assert result["status"] == "no_signal"
    assert len(result["i5"]["group_weights"]) == 10
    assert result["mapping"]["activity_assessment"]["category"] == "no_threshold_decline"
    assert result["operational_alert_allowed"] is False


def test_corrupt_ten_group_bundle_is_not_served_as_six(settings, tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    manifest = json.loads((TEN_BUNDLE / "manifest.json").read_text())
    manifest["outputs"] = {}
    (broken / "manifest.json").write_text(json.dumps(manifest))
    with TestClient(create_app(replace(settings, survey_area_bundle=broken))) as client:
        assert client.get("/api/model/survey-area").status_code == 503
