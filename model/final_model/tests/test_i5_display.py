"""Fixed projection boundaries and published nationwide regression."""
import json
import math
import statistics
from pathlib import Path

import pytest
import jsonschema

from final_model.src.i5_display import (
    REFERENCE_MEAN, REFERENCE_STD, REFERENCE_VERSION, calculate_i5_display_score,
)
from final_model.src.survey_area_index import query

BUNDLE = Path(__file__).resolve().parents[2] / "data/i5/national_2025"


def test_fixed_transform_boundaries():
    for raw, expected in ((REFERENCE_MEAN, 5), (REFERENCE_MEAN - REFERENCE_STD, 4),
                          (REFERENCE_MEAN + REFERENCE_STD, 6), (0, 0), (100, 10)):
        result = calculate_i5_display_score(raw)
        assert result["i5_display_score"] == pytest.approx(expected)
        assert result["i5_display_clipped"] is (raw in (0, 100))
        assert result["i5_display_reference_version"] == REFERENCE_VERSION
    assert calculate_i5_display_score(None)["i5_display_score"] is None
    assert calculate_i5_display_score(None)["i5_display_clipped"] is None
    for value in (True, float("nan"), float("inf"), -0.01, 100.01, "35"):
        with pytest.raises(ValueError):
            calculate_i5_display_score(value)


@pytest.mark.parametrize("code,raw,display", [
    ("11680", 34.12395682037197, 3.7177290667943907),
    ("51110", 34.74343912705784, 4.4361192468688815),
    ("41590", 33.73890594054642, 3.271200137864075),
    ("27720", 37.01875641748605, 7.074718574655971),
])
def test_published_december_examples(code, raw, display):
    response = query(BUNDLE, region_code=code, month="202512")
    assert response["total_matched"] == 1
    row = response["records"][0]
    assert row["isolation_index"] == row["i5_reference"] == raw
    assert row["i5_display_score"] == pytest.approx(display, abs=1e-10)
    assert row["i5_display_clipped"] is False
    assert response["quality_status"] == "hold"
    assert response["operational_alert_allowed"] is False


def test_national_coverage_reference_and_dong_application():
    sgg = [json.loads(line) for line in (BUNDLE / "sgg_isolation_index.jsonl").open()]
    dong = [json.loads(line) for line in (BUNDLE / "dong_isolation_index.jsonl").open()]
    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas/survey_area_index.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)
    for row in sgg + dong:
        validator.validate(row)
    assert len(sgg) == 1584 and len(dong) == 21698
    december = [row for row in sgg if row["month"] == "202512"]
    reference = [row["isolation_index"] for row in december
                 if not ("시 " in row["region_name"] and row["region_name"].endswith("구"))]
    assert len(reference) == 229
    assert math.isclose(statistics.mean(reference), REFERENCE_MEAN, abs_tol=1e-12)
    assert math.isclose(statistics.pstdev(reference), REFERENCE_STD, abs_tol=1e-12)
    for rows in (sgg, dong):
        assert all(set(("E", "P", "O", "C", "U")) == set(row["domain_scores"]) for row in rows)
        assert all(row["i5_display_score"] == calculate_i5_display_score(row["isolation_index"])["i5_display_score"] for row in rows)
        assert not any(row["i5_display_clipped"] for row in rows)
        assert all(row["local_survey_measured"] is False and row["operational_alert_allowed"] is False for row in rows)
    assert query(BUNDLE, level="administrative_dong", month="202512")["total_matched"] == 3617
    missing = json.loads((BUNDLE / "unscored_areas.json").read_text())[0]
    response = query(BUNDLE, level="administrative_dong", dong_code=missing["dong_code"], month=missing["month"])
    assert response["records"] == []
    assert response["availability"]["reason"] == "zero_registered_adult_population"


def test_same_raw_value_uses_same_fixed_reference_in_every_query():
    broad = query(BUNDLE, region_code="11680", month="202512")["records"][0]
    filtered = query(BUNDLE, region_code="11680")["records"][-1]
    assert broad["record_id"] == filtered["record_id"]
    assert broad["i5_display_score"] == filtered["i5_display_score"]
