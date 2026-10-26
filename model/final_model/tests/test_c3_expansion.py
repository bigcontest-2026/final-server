"""C3 must join the same adult age cell and sum seniors before the baseline."""
import csv
import copy
import io
from datetime import date, timedelta
from collections import defaultdict
from zipfile import ZipFile
import json
from pathlib import Path

import jsonschema
import pytest

from final_model.src.common import load_module, read_jsonl
from final_model.src.pipeline import historical_key

MODEL = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(("age", "expected"), [
    ("20", "20G"), ("30", "30G"), ("40", "40G"), ("50", "50G"), ("60+", "60GU"),
])
def test_c3_joins_the_corresponding_adult_age_cell(age, expected):
    row = dict(dataset="data2", view="age_sex", calendar_type="all", month="2025-10",
               region="강남구", sex="여성", age=age)
    assert historical_key(row) == ("202510", "11680", "WMAN", expected)


@pytest.mark.parametrize("age", ["40", "50", "60+"])
def test_c3_schema_accepts_expanded_age_evidence(age):
    row = read_jsonl(MODEL.parent / "bundles/activity/historical_reference.jsonl")[0]
    row["age"] = age
    schema = json.loads((MODEL / "final_model/schemas/historical.schema.json").read_text())
    jsonschema.validate(row, schema)


def raw_input(tmp_path, *, duplicate=False):
    path = tmp_path / "신한카드_빅콘테스트2026_데이터2.txt"
    rows = [["20250701", "강남구", "업종", "남성", f"{age}대", str(count*100), str(count), "서울"]
            for age, count in [(20, 7), (40, 8), (50, 9), (60, 10), (70, 20), (80, 30), (90, 40)]]
    if duplicate:
        rows.append(rows[-1])
    with path.open("w", encoding="cp949", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(["TA_YMD", "MCT_SGG_CD", "MCT_RY_CD", "SEX_CCD", "AGE_CCD", "TS_AT", "USE_CNT", "CLN_SGG_CD"])
        writer.writerows(rows)
    return path


def c3_module():
    root = MODEL / "C3_Shinhan_baseline"
    module = load_module("test_c3_expanded", root / "c3_baseline.py")
    return module, json.loads((root / "config_c3.json").read_text())


def test_senior_raw_counts_are_summed_before_weekday_baseline(tmp_path):
    c3, config = c3_module()
    daily, profile = c3.aggregate_sources(c3.discover_sources(raw_input(tmp_path), "data2"), "data2", config)
    by_age = {row.age: row.count for row in daily[daily.view == "age_sex"].itertuples()}
    assert by_age == {"20": 7, "40": 8, "50": 9, "60+": 100}
    assert profile["age_aggregation_stage"] == "daily_raw_sum_before_weekday_baseline"


def test_senior_aggregation_still_rejects_duplicate_original_rows(tmp_path):
    c3, config = c3_module()
    with pytest.raises(ValueError, match="중복"):
        c3.aggregate_sources(c3.discover_sources(raw_input(tmp_path, duplicate=True), "data2"), "data2", config)


@pytest.fixture
def expanded_cells():
    """Test fixtures only: no fabricated history is written to application bundles."""
    base = MODEL.parent / "bundles/activity"
    behaviors = [row for row in read_jsonl(base / "behavior_evidence.jsonl")
                 if row["key"]["region_code"] == "11680" and row["key"]["month"] in ("202509", "202510")]
    observations = read_jsonl(base / "monthly_observations.jsonl")
    template = next(row for row in read_jsonl(base / "historical_reference.jsonl")
                    if row["view"] == "age_sex" and row["calendar_type"] == "all")
    history = []
    for row in behaviors:
        key = row["key"]
        if key["month"] != "202510":
            continue
        item = copy.deepcopy(template)
        item.update(region="강남구", month="2025-10", sex="남성" if key["sex_code"] == "MAN" else "여성",
                    age="60+" if key["age_code"] == "60GU" else key["age_code"][:2],
                    group_id=row["record_id"], record_id="C3:fixture:" + row["record_id"])
        item["count"]["reported_sum"] = row["observation"]["card_count"]
        history.append(item)
    config = dict(evaluation_start="2025-10-01", evaluation_end="2025-10-31",
                  age_groups=["20", "30", "40", "50", "60+"])
    return behaviors, observations, history, config, {"11680": "강남구"}


def test_linking_expanded_history_preserves_v3_and_leaves_reference_month_unlinked(expanded_cells):
    from final_model.src.c3_expansion import link_history
    behaviors, observations, history, config, regions = expanded_cells
    original = copy.deepcopy(behaviors)
    updated = link_history(behaviors, observations, history, config, regions)
    assert behaviors == original
    assert sum(row["historical_reference_id"] is not None for row in updated) == 10
    for old, new in zip(original, updated):
        assert {k: v for k, v in old.items() if k != "historical_reference_id"} == {
            k: v for k, v in new.items() if k != "historical_reference_id"}
        if new["key"]["month"] == "202509":
            assert new["historical_reference_id"] is None


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong_month", "count_mismatch"])
def test_expansion_rejects_incomplete_or_incorrect_raw_reconciliation(expanded_cells, mutation):
    from final_model.src.c3_expansion import link_history
    behaviors, observations, history, config, regions = expanded_cells
    if mutation == "missing": history.pop()
    elif mutation == "duplicate": history.append(copy.deepcopy(history[0]))
    elif mutation == "wrong_month": history[0]["month"] = "2025-11"
    else: history[0]["count"]["reported_sum"] += 1
    with pytest.raises(ValueError, match="C3_"):
        link_history(behaviors, observations, history, config, regions)


def test_expansion_rejects_changes_to_old_c3_baselines(expanded_cells):
    from final_model.src.c3_expansion import verify_previous_history
    history = expanded_cells[2]
    updated = copy.deepcopy(history)
    updated[0]["count"]["baseline_sum"] += 1
    with pytest.raises(ValueError, match="C3_previous_values_changed"):
        verify_previous_history(history, updated)


def test_reference_period_counts_must_match_saved_v3(expanded_cells):
    from final_model.src.c3_expansion import reconcile_monthly_observations
    _, observations, _, config, regions = expanded_cells
    config["reference_start"] = "2025-09-01"
    descriptive = []
    for row in observations:
        key = row["key"]
        if key["month"] not in ("202509", "202510") or key["region_code"] != "11680":
            continue
        descriptive.append(dict(view="age_sex", month=key["month"][:4] + "-" + key["month"][4:],
            region="강남구", sex="남성" if key["sex_code"] == "MAN" else "여성",
            age="60+" if key["age_code"] == "60GU" else key["age_code"][:2],
            count_reported_sum=row["card_count"]))
    descriptive[0]["count_reported_sum"] += 1
    with pytest.raises(ValueError, match="C3_month_count_mismatch"):
        reconcile_monthly_observations(descriptive, observations, config, regions)


def test_missing_input_cannot_modify_or_create_a_bundle(tmp_path):
    from final_model.src.c3_expansion import regenerate
    before = (MODEL.parent / "bundles/activity/manifest.json").read_bytes()
    with pytest.raises(FileNotFoundError):
        regenerate(MODEL.parent / "bundles/activity", tmp_path / "missing.zip", tmp_path / "output")
    assert not (tmp_path / "output").exists()
    assert (MODEL.parent / "bundles/activity/manifest.json").read_bytes() == before


def test_regeneration_writes_a_verified_separate_bundle_from_raw_fixture(tmp_path):
    from final_model.src.c3_expansion import regenerate
    from final_model.src.common import dump, sha, verify_bundle, write_jsonl
    from final_model.src.query import query
    text = io.StringIO(newline="")
    writer = csv.writer(text, delimiter="\t")
    writer.writerow(["TA_YMD", "MCT_SGG_CD", "MCT_RY_CD", "SEX_CCD", "AGE_CCD", "TS_AT", "USE_CNT", "CLN_SGG_CD"])
    counts = defaultdict(int)
    day = date(2025, 7, 1)
    while day <= date(2025, 12, 31):
        for region, region_name in [("11680", "강남구"), ("51110", "춘천시")]:
            for sex, sex_name in [("MAN", "남성"), ("WMAN", "여성")]:
                for age in (20, 30, 40, 50, 60, 70, 80, 90):
                    count = 10 + age // 10 + day.day % 11
                    writer.writerow([day.strftime("%Y%m%d"), region_name, "업종", sex_name,
                                     f"{age}대", count*100, count, "서울"])
                    age_code = f"{age}G" if age < 60 else "60GU"
                    counts[day.strftime("%Y%m"), region, sex, age_code] += count
        day += timedelta(days=1)
    raw = tmp_path / "test-only-card.zip"
    with ZipFile(raw, "w") as archive:
        archive.writestr("신한카드_빅콘테스트2026_데이터2.txt", text.getvalue().encode("cp949"))
    base = tmp_path / "base"
    base.mkdir()
    source = MODEL.parent / "bundles/activity"
    behaviors = read_jsonl(source / "behavior_evidence.jsonl")
    observations = read_jsonl(source / "monthly_observations.jsonl")
    for row in observations:
        row["card_count"] = counts[tuple(row["key"][k] for k in ("month", "region_code", "sex_code", "age_code"))]
    write_jsonl(base / "behavior_evidence.jsonl", behaviors)
    write_jsonl(base / "monthly_observations.jsonl", observations)
    c3, old_config = c3_module()
    old_config["age_groups"] = ["20", "30"]
    old_daily, old_profile = c3.aggregate_sources(c3.discover_sources(raw, "data2"), "data2", old_config)
    holidays, _ = c3.load_calendar(MODEL / "C3_Shinhan_baseline", old_config)
    old_results, _ = c3.make_daily_results(old_daily, old_config, holidays)
    old_results, _ = c3.fit_isolation_forest(old_results, old_config)
    _, old_monthly, _ = c3.monthly_summaries(old_results, old_config)
    previous = c3.make_agent_records(old_monthly, "data2", old_profile, old_config, False, "fixture-only-previous")
    write_jsonl(base / "historical_reference.jsonl", previous)
    (base / "survey_mois_reference").mkdir()
    dump(base / "survey_mois_reference/manifest.json", {"fixture_only": True})
    dump(base / "old_comparison.json", {"linked_cells": 24})
    (base / "untracked.txt").write_text("not a serving artifact")
    files = ["behavior_evidence.jsonl", "monthly_observations.jsonl", "historical_reference.jsonl",
             "survey_mois_reference/manifest.json", "old_comparison.json"]
    dump(base / "manifest.json", {"run_id": "fixture-only", "status": "completed", "is_synthetic": False,
         "inputs": {raw.name: sha(raw)}, "validation": {"historical_rows": 880, "historical_join_rows": 24},
         "outputs": {name: sha(base / name) for name in files}})
    before = {name: (base / name).read_bytes() for name in [*files, "manifest.json"]}
    output = tmp_path / "expanded"
    report = regenerate(base, raw, output)
    assert report["linked_cells"] == 60 and report["unlinked_cells"] == 40
    assert report["V3_cells_preserved"] == 100
    assert report["raw_month_count_matches"] == 120
    assert report["previous_history_values_preserved"] == len(previous) > 0
    manifest = verify_bundle(output)
    assert manifest["validation"]["historical_join_rows"] == 60
    assert manifest["source_validation"] == {"historical_rows": 880, "historical_join_rows": 24}
    assert not (output / "old_comparison.json").exists()
    assert not (output / "untracked.txt").exists()
    assert "survey_mois_reference/manifest.json" in manifest["outputs"]
    for age in ("20G", "30G", "40G", "50G", "60GU"):
        page = query(output, month="202510", age_code=age)
        assert len(page["records"]) == 4
        assert all(item["historical_reference"] is not None for item in page["records"])
    assert all(item["historical_reference"] is None for item in query(output, month="202509")["records"])
    assert all((base / name).read_bytes() == content for name, content in before.items())
    for row in read_jsonl(output / "historical_reference.jsonl"):
        assert all((output / name).is_file() for name in row["evidence_files"].values())
