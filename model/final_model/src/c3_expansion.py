"""Rebuild C3 from real card inputs without recalculating V3 or publishing in place."""
import copy
import json
import math
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from .c3_contract import expected_link_keys
from .common import dump, load_module, read_jsonl, sha, verify_bundle, write_jsonl
from .pipeline import historical_key

FIELDS = ("month", "region_code", "sex_code", "age_code")
FINAL = Path(__file__).resolve().parents[1]
C3_ROOT = FINAL.parent / "C3_Shinhan_baseline"


def cell_key(row):
    return tuple(row["key"][field] for field in FIELDS)


def reconcile_monthly_observations(descriptive, observations, config, regions):
    monthly = {cell_key(row): row for row in observations}
    if len(monthly) != len(observations):
        raise ValueError("C3_duplicate_observation_key")
    seen = set()
    for row in descriptive:
        if row["view"] != "age_sex":
            continue
        key = historical_key({**row, "dataset": "data2", "calendar_type": "all"}, regions)
        if key in seen:
            raise ValueError("C3_duplicate_observed_key")
        seen.add(key)
        count = monthly.get(key, {}).get("card_count")
        if count is None or row["count_reported_sum"] != count:
            raise ValueError("C3_month_count_mismatch")
    expected = expected_link_keys({**config, "evaluation_start": config["reference_start"]}, regions)
    if seen != expected:
        raise ValueError("C3_observed_key_set_mismatch")
    return len(seen)


def link_history(behaviors, observations, history, config, regions):
    links = {}
    ids = set()
    validator = Draft202012Validator(json.loads((FINAL / "schemas/historical.schema.json").read_text()))
    for row in history:
        validator.validate(row)
        if row["record_id"] in ids:
            raise ValueError("C3_duplicate_record_id")
        ids.add(row["record_id"])
        key = historical_key(row, regions)
        if key is not None:
            if key in links:
                raise ValueError("C3_duplicate_link_key")
            links[key] = row
    expected = expected_link_keys(config, regions)
    if set(links) != expected:
        raise ValueError("C3_expected_link_key_set_mismatch")
    keys = {cell_key(row) for row in behaviors}
    if len(keys) != len(behaviors) or not expected <= keys:
        raise ValueError("C3_behavior_key_set_mismatch")
    monthly = {cell_key(row): row for row in observations}
    if len(monthly) != len(observations):
        raise ValueError("C3_duplicate_observation_key")
    for key, row in links.items():
        count = monthly.get(key, {}).get("card_count")
        if count is None or row["count"]["reported_sum"] != count:
            raise ValueError("C3_month_count_mismatch")
    updated = copy.deepcopy(behaviors)
    for row in updated:
        history_row = links.get(cell_key(row))
        row["historical_reference_id"] = history_row["record_id"] if history_row else None
    return updated


def _same_values(old, new):
    if isinstance(old, dict):
        return isinstance(new, dict) and old.keys() == new.keys() and all(_same_values(v, new[k]) for k, v in old.items())
    if isinstance(old, (int, float)) and not isinstance(old, bool):
        return isinstance(new, (int, float)) and not isinstance(new, bool) and math.isclose(old, new, rel_tol=0, abs_tol=1e-8)
    return old == new


def verify_previous_history(previous, history):
    def identity(row):
        return row["group_id"], row["month"], row["calendar_type"]
    index = {identity(row): row for row in history}
    if len(index) != len(history):
        raise ValueError("C3_duplicate_history_key")
    for old in previous:
        new = index.get(identity(old))
        if new is None:
            raise ValueError("C3_previous_history_missing")
        for field in ("count", "amount", "review_flag", "quality_review_required", "source_fingerprint"):
            if not _same_values(old[field], new[field]):
                raise ValueError("C3_previous_values_changed:" + field)


def regenerate(base, card_zip, output):
    base, card_zip, output = Path(base).resolve(), Path(card_zip).resolve(), Path(output).resolve()
    if not card_zip.is_file():
        raise FileNotFoundError(card_zip)
    if output.exists() or output.is_relative_to(base):
        raise ValueError("C3_output_must_be_new_and_outside_base")
    manifest = verify_bundle(base)
    source_hash = sha(card_zip)
    expected_hashes = {digest for name, digest in manifest["inputs"].items() if name.lower().endswith(".zip")}
    if source_hash not in expected_hashes:
        raise ValueError("C3_raw_card_hash_mismatch")
    manifest_hash = sha(base / "manifest.json")
    behaviors = read_jsonl(base / "behavior_evidence.jsonl")
    observations = read_jsonl(base / "monthly_observations.jsonl")
    previous = read_jsonl(base / "historical_reference.jsonl")
    regions = {row["key"]["region_code"]: row["region_name"] for row in behaviors}
    config = json.loads((C3_ROOT / "config_c3.json").read_text())
    c3 = load_module("c3_expansion_calculation", C3_ROOT / "c3_baseline.py")
    c3.validate_config(config)
    holidays, calendar_meta = c3.load_calendar(C3_ROOT, config)
    daily, profile = c3.aggregate_sources(c3.discover_sources(card_zip, "data2"), "data2", config)
    results, reference = c3.make_daily_results(daily, config, holidays)
    results, models = c3.fit_isolation_forest(results, config)
    descriptive, monthly, sensitivity = c3.monthly_summaries(results, config)
    matched_months = reconcile_monthly_observations(descriptive, observations, config, regions)
    if config["isolation_forest"]["enabled"] and (
        {model["view"] for model in models} != set(config["views"]["data2"])
        or any(model["status"] != "fitted" or model["fit_max_date"] > config["reference_end"] for model in models)
    ):
        raise ValueError("C3_reference_model_fit_invalid")
    run_id = datetime.now(timezone.utc).strftime("c3_20plus_%Y%m%d_%H%M%S_%f")
    history = c3.make_agent_records(monthly, "data2", profile, config, False, run_id)
    for row in history:
        row["evidence_files"] = {"monthly": "c3_details/monthly_results.jsonl", "daily": "c3_details/daily_results.csv",
                                 "reference": "c3_details/reference_points.csv", "if_models": "c3_details/model_metadata.json"}
    verify_previous_history(previous, history)
    updated = link_history(behaviors, observations, history, config, regions)
    report = {"status": "verified", "age_groups": config["age_groups"], "historical_rows": len(history),
              "linked_cells": sum(row["historical_reference_id"] is not None for row in updated),
              "unlinked_cells": sum(row["historical_reference_id"] is None for row in updated),
              "previous_history_values_preserved": len(previous), "V3_cells_preserved": len(behaviors),
              "raw_month_count_matches": matched_months,
              "raw_card_sha256": source_hash, "base_run_id": manifest["run_id"],
              "base_manifest_sha256": manifest_hash, "operational_alert_allowed": False}
    # A failing calculation never creates the output or touches the serving bundle.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".c3-expansion-", dir=output.parent) as temporary:
        staged = Path(temporary) / "bundle"
        staged.mkdir()
        serving_files = {"behavior_evidence.jsonl", "historical_reference.jsonl", "monthly_observations.jsonl",
                         "region_summary.json", "dong_skt_evidence.jsonl"}
        for relative in manifest["outputs"]:
            if relative in serving_files or relative.startswith("survey_mois_reference/"):
                destination = staged / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(base / relative, destination)
        details = staged / "c3_details"
        details.mkdir(exist_ok=True)
        results.to_csv(details / "daily_results.csv", index=False)
        reference.to_csv(details / "reference_points.csv", index=False)
        for name, value in (("config_used", config), ("data_profile", profile),
                            ("model_metadata", models), ("calendar_metadata", calendar_meta)):
            dump(details / (name + ".json"), value)
        write_jsonl(details / "monthly_results.jsonl", monthly)
        write_jsonl(details / "threshold_sensitivity.jsonl", sensitivity)
        write_jsonl(staged / "historical_reference.jsonl", history)
        write_jsonl(staged / "behavior_evidence.jsonl", updated)
        dump(staged / "c3_expansion_validation.json", report)
        manifest["source_validation"] = manifest.get("validation", {})
        manifest["validation"] = {**report, "historical_rows": len(history),
            "historical_join_rows": report["linked_cells"], "historical_schema_records": len(history),
            "no_future_values_in_C3_reference": config["reference_end"] < config["evaluation_start"]}
        manifest["validation_scope"] = "C3_raw_repeat_with_preserved_V3_observations"
        manifest["distribution"] = {"kind": "serving_subset", "source_run_id": report["base_run_id"],
            "source_manifest_sha256": manifest_hash,
            "excluded": "Original reproduction/comparison artifacts; V3 serving observations preserved, C3 recalculated."}
        manifest.update(run_id=run_id, c3_expansion={**report, "code_version": c3.VERSION,
            "source_code_sha256": {name: sha(C3_ROOT / name) for name in
                ("c3_baseline.py", "config_c3.json", "calendar_2025_H2.csv", "calendar_metadata.json")}})
        manifest["c3_expansion"]["integration_code_sha256"] = {name: sha(FINAL / name) for name in
            ("src/c3_contract.py", "src/c3_expansion.py", "src/pipeline.py", "src/query.py", "schemas/historical.schema.json")}
        manifest["outputs"] = {path.relative_to(staged).as_posix(): sha(path)
                               for path in sorted(staged.rglob("*")) if path.is_file() and path != staged / "manifest.json"}
        dump(staged / "manifest.json", manifest)
        verify_bundle(staged)
        verify_bundle(base)
        if sha(base / "manifest.json") != manifest_hash:
            raise ValueError("C3_base_changed_during_calculation")
        staged.rename(output)
    return report
