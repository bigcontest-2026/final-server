"""Raw ten-group reference, provenance, and compatible display projection."""
import csv
import importlib
import importlib.util
import json
import shutil
import statistics
from pathlib import Path

import jsonschema
import pytest

from final_model.src import i5_display

MODEL = Path(__file__).resolve().parents[2]
BUNDLE = MODEL / "data/i5/national_2025_10group"
SIX_BUNDLE = MODEL / "data/i5/national_2025"
PROJECT = MODEL.parents[2]
SOURCE = PROJECT / "isolation-index/i5_10group_rebuild"
LEGACY = PROJECT / "model/data/i5/national_2025"
CONTRACT = "survey_area_isolation_index_10group_v1"


def ten_module():
    assert importlib.util.find_spec("final_model.src.survey_area_index_ten") is not None, "ten-group reader must be available"
    return importlib.import_module("final_model.src.survey_area_index_ten")


def rows(name):
    assert BUNDLE.is_dir(), "verified ten-group bundle must be published separately"
    return [json.loads(line) for line in (BUNDLE / name).open(encoding="utf-8")]


def test_all_ten_group_population_and_score_reproduction():
    ten_module()
    with (BUNDLE / "national_ten_group_profile.csv").open(encoding="utf-8-sig", newline="") as stream:
        national = {(r["sex"], r["age_group"]): r for r in csv.DictReader(stream)}
    assert len(national) == 10
    assert sum(int(r["n_valid"]) for r in national.values()) == 31817
    schema = json.loads((MODEL / "final_model/schemas/survey_area_index_ten.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)
    sgg, dong = rows("sgg_isolation_index.jsonl"), rows("dong_isolation_index.jsonl")
    assert (len(sgg), len(dong)) == (1584, 21698)
    for row in sgg + dong:
        validator.validate(row)
        assert len(row["group_populations"]) == len(row["group_weights"]) == 10
        assert sum(row["group_populations"].values()) == row["population_20plus"]
        assert sum(row["group_weights"].values()) == pytest.approx(1, abs=1e-12)
        for sex, age in national:
            key = sex + "_" + age
            assert row["group_weights"][key] == pytest.approx(row["group_populations"][key] / row["population_20plus"], abs=1e-12)
        for measure in ("i5", "E", "P", "O", "C", "U"):
            expected = sum(row["group_weights"][sex + "_" + age] * float(profile[measure + "_mean"])
                           for (sex, age), profile in national.items())
            observed = row["i5_reference"] if measure == "i5" else row["domain_scores"][measure]
            assert observed == pytest.approx(expected, abs=1e-9)
        assert row["isolation_index"] == row["i5_reference"]
        assert row["i5_reference"] == pytest.approx(sum(row["domain_scores"].values()) / 5, abs=1e-9)
        assert "i5_display_score" not in row
        assert row["local_survey_measured"] is False
        assert row["operational_alert_allowed"] is False


@pytest.mark.parametrize("code,raw,display", [
    ("11680", 34.11978719425335, 3.7126841435117717),
    ("51110", 34.744958785034456, 4.430568907166258),
])
def test_december_query_returns_ten_profiles_and_standard_availability(code, raw, display):
    m = ten_module()
    result = m.query(BUNDLE, month="202512", region_code=code)
    row = result["records"][0]
    assert result["contract_version"] == CONTRACT
    assert result["run_id"]
    assert result["total_matched"] == 1
    assert result["availability"]["status"] == "available"
    assert result["availability"]["region_code"] == code
    assert result["source_population_coverage_verified"] is False
    assert row["isolation_index"] == raw
    assert len(row["group_profiles"]) == 10
    assert sum(p["population"] for p in row["group_profiles"]) == row["population_20plus"]
    assert sum(p["population_share"] * p["i5_reference"] for p in row["group_profiles"]) == pytest.approx(raw, abs=1e-9)
    projected = i5_display.project_i5_record(row)
    assert projected["i5_display_score"] == pytest.approx(display, abs=1e-12)
    assert "i5_display_score" not in row
    profiles = m.national_profiles(BUNDLE / "national_ten_group_profile.csv")
    assert m.with_group_profiles({k: v for k, v in row.items() if k != "group_profiles"}, profiles) == row


def test_ten_display_uses_one_fixed_229_area_reference():
    ten_module()
    december = [r for r in rows("sgg_isolation_index.jsonl") if r["month"] == "202512"]
    reference = [r["isolation_index"] for r in december
                 if not ("시 " in r["region_name"] and r["region_name"].endswith("구"))]
    assert len(reference) == 229
    assert statistics.mean(reference) == pytest.approx(35.24084915654042, abs=1e-12)
    assert statistics.pstdev(reference) == pytest.approx(0.8708522905523021, abs=1e-12)
    assert hasattr(i5_display, "project_i5_record"), "projection must select the record's source contract"
    for r in december:
        projected = i5_display.project_i5_record(r)
        assert projected["i5_display_reference_version"] == "i5_10group_sgg_202512_equal_weight_v1"
        assert projected["i5_display_score"] == pytest.approx(5 + (r["isolation_index"] - 35.24084915654042) / 0.8708522905523021)


def test_historical_six_projection_keeps_its_original_reference():
    assert hasattr(i5_display, "project_i5_record"), "compatible record projection must be available"
    row = next(json.loads(line) for line in (SIX_BUNDLE / "sgg_isolation_index.jsonl").open()
               if json.loads(line)["month"] == "202512" and json.loads(line)["area_code"] == "11680")
    original = dict(row)
    projected = i5_display.project_i5_record(row)
    assert projected["i5_display_reference_version"] == "i5_sgg_202512_equal_weight_v1"
    assert projected["i5_display_score"] == pytest.approx(3.7177290667943907, abs=1e-12)
    assert projected == original
    assert projected is not row
    assert i5_display.calculate_i5_display_score(34.12395682037197)["i5_display_score"] == pytest.approx(3.7177290667943907)
    source_only = {"source_contract": "survey_mois_reference_10group_v1", "i5_reference": 35.24084915654042}
    assert i5_display.project_i5_record(source_only)["i5_display_score"] == 5
    for invalid in (True, float("nan"), float("inf"), -1, 101):
        with pytest.raises(ValueError):
            i5_display.project_i5_record({"schema_version": CONTRACT, "isolation_index": invalid})


def test_zero_adult_and_unknown_dong_remain_unavailable():
    m = ten_module()
    result = m.query(BUNDLE, level="administrative_dong", month="202512", dong_code="4375025400")
    assert result["records"] == []
    assert result["availability"]["status"] == "data_unavailable"
    assert result["availability"]["reason"] == "zero_registered_adult_population"
    assert result["operational_alert_allowed"] is False
    unknown = m.query(BUNDLE, level="administrative_dong", month="202512", dong_code="1199999999")
    assert unknown["availability"]["reason"] == "no_matching_observations_in_bundle"
    groups = m.query_national_groups(BUNDLE)
    assert groups["total_matched"] == 10
    assert {r["age_group"] for r in groups["records"]} == {"20-29", "30-39", "40-49", "50-59", "60+"}


def test_bundle_tamper_is_rejected(tmp_path):
    m = ten_module()
    altered = tmp_path / "tampered"
    shutil.copytree(BUNDLE, altered)
    with (altered / "national_ten_group_profile.csv").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="bundle_output_hash_mismatch"):
        m.query_national_groups(altered)


@pytest.mark.parametrize("name", ["national_ten_group_profile.csv", "unscored_areas.json", "area_registry.json"])
def test_required_files_cannot_be_untracked_even_when_present(tmp_path, name):
    m = ten_module()
    altered = tmp_path / "untracked"
    shutil.copytree(BUNDLE, altered)
    manifest_path = altered / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["outputs"][name]
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="untracked_required_output"):
        m.query_national_groups(altered)


def test_builder_accepts_verified_legacy_path_and_copies_registry(tmp_path):
    m = ten_module()
    if not SOURCE.is_dir() or not LEGACY.is_dir():
        pytest.skip("upstream aggregate rebuild checkpoints are optional outside the source workspace")
    output = m.build(SOURCE, tmp_path / "built", legacy_bundle=LEGACY)
    manifest = json.loads((output / "manifest.json").read_text())
    published = json.loads((BUNDLE / "manifest.json").read_text())
    assert manifest["run_id"] == published["run_id"]
    assert manifest["outputs"] == published["outputs"]
    assert (output / "area_registry.json").read_bytes() == (LEGACY / "area_registry.json").read_bytes()
    assert manifest["source_population_coverage_verified"] is False
    assert manifest["validation"]["july_november_official_40_column_direct_reconciliation"] == "not_verified"
    assert manifest["validation"]["display_conversion_applied"] is False
    assert manifest["validation"]["area_rows"] == {"sgg": 1584, "administrative_dong": 21698}
