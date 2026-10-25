"""Actual nationwide published population and frozen two-region I5 regressions."""
import csv
import json
from pathlib import Path

import jsonschema
import pytest

from final_model.src.national_population import read_month
from final_model.src.survey_area_index import query

FINAL=Path(__file__).resolve().parents[1]
ROOT=FINAL.parents[1]


@pytest.fixture
def national_bundle():
    pointer=FINAL/"outputs/survey_area_index_latest.json"
    if not pointer.exists():pytest.skip("nationwide actual artifacts not installed")
    bundle=pointer.parent/json.loads(pointer.read_text(encoding="utf-8"))["run_id"]
    if not (bundle/"national_validation_report.json").exists():pytest.skip("latest I5 is not nationwide")
    return bundle


def test_all_six_official_months_reconcile_every_age_sex_total(national_bundle):
    report=json.loads((national_bundle/"national_validation_report.json").read_text(encoding="utf-8"))
    assert len(report["months"])==6
    assert all(m["provinces_checked"]==17 and m["all_39_population_fields_reconciled"] for m in report["months"])
    assert report["scored_dong_month_rows"]==21698 and report["sgg_month_rows"]==1584
    assert len(report["unscored_areas"])==16
    schema=json.loads((FINAL/"schemas/survey_area_index.schema.json").read_text(encoding="utf-8"))
    validator=jsonschema.Draft202012Validator(schema)
    for name in ("sgg_isolation_index.jsonl","dong_isolation_index.jsonl"):
        with (national_bundle/name).open(encoding="utf-8") as stream:
            for line in stream:validator.validate(json.loads(line))


def test_frozen_282_area_month_scores_match_national_population(national_bundle):
    frozen=ROOT/"isolation-index/v3/exports/2025_final_v1/dong_month_reference.csv"
    if not frozen.exists():pytest.skip("two-region regression data not installed")
    with frozen.open(encoding="utf-8-sig") as stream:old={(r["month"],r["dong_code"]):r for r in csv.DictReader(stream)}
    with (national_bundle/"dong_isolation_index.jsonl").open(encoding="utf-8") as stream:
        records={key:r for line in stream if (r:=json.loads(line)) and (key:=(r["month"],r["dong_code"])) in old}
    assert len(records)==282
    for key,row in old.items():
        assert records[key]["population_20plus"]==int(row["population_20plus"])
        assert records[key]["isolation_index"]==pytest.approx(float(row["i5_reference"]),abs=1e-9)


def test_parent_city_queries_and_zero_population_do_not_fabricate_scores(national_bundle):
    for code in ("26110","41110","36110","50130"):
        result=query(national_bundle,region_code=code,month="202512")
        assert result["total_matched"]==1 and result["records"][0]["operational_alert_allowed"] is False
    missing=json.loads((national_bundle/"unscored_areas.json").read_text(encoding="utf-8"))[0]
    result=query(national_bundle,level="administrative_dong",dong_code=missing["dong_code"],month=missing["month"])
    assert result["records"]==[] and result["availability"]["reason"]=="zero_registered_adult_population"


def test_corrupt_official_totals_are_rejected(tmp_path):
    source=ROOT/"data-set/mois_national/mois_age_sex_202507.csv"
    if not source.exists():pytest.skip("official raw CSV not installed")
    text=source.read_text(encoding="cp949");changed=tmp_path/"corrupt.csv"
    changed.write_text(text.replace('9,323,492','9,323,493',1),encoding="cp949")
    with pytest.raises(ValueError,match="official_age_sum_mismatch"):read_month(changed,"202507")
