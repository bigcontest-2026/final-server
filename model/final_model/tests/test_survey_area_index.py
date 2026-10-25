"""Synthetic boundary checks and separately marked actual-data regression checks."""
import copy
import json
from pathlib import Path
import pytest
import jsonschema

from final_model.src.common import dump, sha, write_jsonl
from final_model.src.survey_area_index import (
    CONTRACT, FINAL, GROUPS, make_record, profiles, query, read_frozen,
)


def synthetic_inputs():
    national_rows = []
    row = dict(month="202512", region_code="11680", region="강남구",
               dong_code="1168051000", dong="신사동",
               population_20plus="210", population_total="300")
    weighted = 0.0
    for position, key in enumerate(GROUPS, 1):
        score = position * 10.0
        population = position * 10
        national_rows.append(dict(sex=key[0], age_group=key[1], n_valid="100",
                                 i5_mean=str(score),
                                 **{name + "_mean": str(score) for name in ("E", "P", "O", "C", "U")}))
        row["pop_" + "_".join(key)] = str(population)
        row["share_" + "_".join(key)] = str(population / 210)
        weighted += population / 210 * score
    for field in ("i5_reference", "E_reference", "P_reference", "O_reference", "C_reference", "U_reference"):
        row[field] = str(weighted)
    return row, national_rows


def record(level="sgg"):
    row, national = synthetic_inputs()
    return make_record(row, level, profiles(national), "synthetic_test_only")


def test_population_weighted_index_preserves_survey_definition():
    result = record()
    assert result["isolation_index"] == pytest.approx(43.33333333333333)
    assert result["isolation_index"] == result["i5_reference"]
    assert result["population_20plus"] == 210
    assert sum(result["group_weights"].values()) == pytest.approx(1)
    assert result["local_survey_measured"] is False
    assert result["local_survey_n_valid"] is None
    assert result["is_isolation_rate"] is False
    assert result["behavior_signals_included"] is False


def test_dong_code_and_scope_are_preserved():
    result = record("administrative_dong")
    assert result["area_code"] == result["dong_code"] == "1168051000"
    assert result["area_name"] == "신사동"
    assert result["region_code"] == "11680"
    assert record()["dong_code"] is None


@pytest.mark.parametrize("field,value,error", [
    ("population_20plus", "300", "denominator_mismatch"),
    ("i5_reference", "99", "recalculation_mismatch"),
    ("share_남_20-39", "0.5", "share_mismatch"),
    ("dong_code", "5111051000", "invalid_dong_code"),
])
def test_inconsistent_source_not_exported(field, value, error):
    row, national = synthetic_inputs()
    row[field] = value
    with pytest.raises(ValueError, match=error):
        make_record(row, "administrative_dong", profiles(national), "synthetic_test_only")


def test_missing_national_group_not_imputed():
    _, national = synthetic_inputs()
    with pytest.raises(ValueError, match="six_national_groups"):
        profiles(national[:-1])


def test_schema_blocks_false_local_measurement_and_alerts():
    schema = json.loads((FINAL / "schemas/survey_area_index.schema.json").read_text(encoding="utf-8-sig"))
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate(record())
    validator.validate(record("administrative_dong"))
    for field in ("local_survey_measured", "is_isolation_rate", "behavior_signals_included", "operational_alert_allowed"):
        invalid = copy.deepcopy(record())
        invalid[field] = True
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(invalid)


def synthetic_bundle(tmp_path):
    write_jsonl(tmp_path / "sgg_isolation_index.jsonl", [record()])
    write_jsonl(tmp_path / "dong_isolation_index.jsonl", [record("administrative_dong")])
    dump(tmp_path / "manifest.json", dict(contract_version=CONTRACT, status="completed",
         is_synthetic=False, outputs={p.name: sha(p) for p in tmp_path.glob("*.jsonl")}))
    return tmp_path


def test_exact_area_queries_and_mismatched_filters(tmp_path):
    bundle = synthetic_bundle(tmp_path)
    result = query(bundle, month="202512", region_code="11680")
    assert result["total_matched"] == 1
    assert query(bundle, region_code="51110")["total_matched"] == 0
    assert query(bundle, level="administrative_dong", dong_code="1168051000")["total_matched"] == 1
    with pytest.raises(ValueError, match="invalid_dong_filter"):
        query(bundle, dong_code="1168051000")
    with pytest.raises(ValueError, match="unsupported_month"):
        query(bundle, month="202601")


def test_query_rejects_modified_area_output(tmp_path):
    bundle = synthetic_bundle(tmp_path)
    (bundle / "sgg_isolation_index.jsonl").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="hash_mismatch"):
        query(bundle)


def test_frozen_source_tamper_rejected(tmp_path):
    files = {}
    for name in ("national_six_group_profile.csv", "dong_month_six_group_population.csv",
                 "region_month_summary.csv", "dong_month_reference.csv"):
        path = tmp_path / name
        path.write_bytes(b"key\nvalue\n")
        files[name] = {"sha256": sha(path), "rows": 1}
    dump(tmp_path / "manifest.json", dict(index_version="I5_2025_final_v1", files=files))
    read_frozen(tmp_path)
    (tmp_path / "region_month_summary.csv").write_text("key\naltered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="frozen_hash_mismatch"):
        read_frozen(tmp_path)


def test_actual_area_coverage_and_december_values():
    latest = FINAL / "outputs/survey_area_index_latest.json"
    if not latest.exists():
        pytest.skip("actual survey-area bundle not present")
    bundle = FINAL / "outputs" / json.loads(latest.read_text(encoding="utf-8"))["run_id"]
    # The latest bundle may be nationwide; the original two-region regression remains fixed.
    regions=("11680","51110")
    assert sum(query(bundle,region_code=code)["total_matched"] for code in regions) == 12
    assert sum(query(bundle, level="administrative_dong",region_code=code)["total_matched"] for code in regions) == 282
    december=[r for code in regions for r in query(bundle, month="202512",region_code=code)["records"]]
    values = {r["region_code"]: r["isolation_index"] for r in december}
    assert values["11680"] == pytest.approx(34.12395682037197)
    assert values["51110"] == pytest.approx(34.74343912705784)
    assert sum(query(bundle, level="administrative_dong", month="202512",region_code=code)["total_matched"] for code in regions) == 47


def test_existing_behavior_outputs_unchanged():
    before = FINAL / "outputs/survey_area_protected_before.json"
    if not before.exists():
        pytest.skip("existing-output preservation audit unavailable")
    expected = json.loads(before.read_text(encoding="utf-8"))
    protected = FINAL / "outputs/final_20261014_224511_915286"
    actual = {p.relative_to(protected).as_posix(): sha(p) for p in sorted(protected.rglob("*")) if p.is_file()}
    assert actual == expected
