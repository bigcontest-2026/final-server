"""Independent 10-group, raw 0–100 I5 survey/population reference bundle.

The six-group survey_area_index contract and its display conversion are untouched.
Build only from the verified, aggregate-only I5 rebuild checkpoint.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

import jsonschema

from .common import sha
from .geography import availability, validate_region_code

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data/i5/national_2025_10group"
LEGACY = ROOT / "data/i5/national_2025"
SCHEMA = ROOT / "final_model/schemas/survey_area_index_ten.schema.json"
GROUP_SCHEMA = ROOT / "final_model/schemas/national_ten_group.schema.json"
CONTRACT = "survey_area_isolation_index_10group_v1"
GROUP_CONTRACT = "national_survey_ten_group_reference_v1"
AREA_VERSION = "survey_area_i5_10group_2025_v1"
INDEX_VERSION = "I5_2025_final_v1"
GROUPS = tuple((s, a) for s in ("남", "여") for a in ("20-29", "30-39", "40-49", "50-59", "60+"))
DOMAINS = ("E", "P", "O", "C", "U")
MONTHS = {f"2025{m:02d}" for m in range(7, 13)}
FORBIDDEN = {"i5_display_score", "i5_z", "i5_display_clipped", "i5_display_reference_version"}
SOURCES = {
    "02_survey_10group": ("national_ten_group_profile.csv", "age_group_mapping.json"),
    "03_nationwide_10group": ("sgg_10group_recomputed.jsonl", "dong_10group_recomputed.jsonl", "unscored_areas.json"),
}
EXPECTED_SOURCE_MANIFESTS = {
    "02_survey_10group": "77826953adba7d00ee5896ac2a51118204a82cccb39fb01886464be172f1daf4",
    "03_nationwide_10group": "3e3d2d3da3bb7c69a678ca237589e4b0ec6a69e86c81f4e7078779ad817b3ced",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def finite(value, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_number:" + label) from exc
    require(math.isfinite(result), "nonfinite:" + label)
    return result


def group_key(group: tuple[str, str]) -> str:
    return "_".join(group)


def parent_age(age: str) -> str:
    if age in ("20-29", "30-39"):
        return "20-39"
    if age in ("40-49", "50-59"):
        return "40-59"
    require(age == "60+", "unknown_age_group")
    return age


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def source_inputs(source_root: Path) -> dict:
    details = {}
    for folder, names in SOURCES.items():
        directory = source_root / folder
        manifest_path = directory / "manifest.json"
        require(sha(manifest_path) == EXPECTED_SOURCE_MANIFESTS[folder], "source_manifest_changed:" + folder)
        manifest = read_json(manifest_path)
        for name, digest in manifest["outputs"].items():
            require(sha(directory / name) == digest, "source_output_changed:" + folder + "/" + name)
        details[folder] = {"manifest_sha256": sha(manifest_path),
                           "inputs": {name: sha(directory / name) for name in names}}
    require(read_json(source_root / "03_nationwide_10group/validation_report.json")["checks"]["source_40_column_july_november_row_equivalence"] == "not_verified",
            "source_validation_status_changed")
    return details


def national_profiles(path: Path) -> dict:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    require(len(rows) == 10, "ten_national_groups_required")
    groups = {}
    for row in rows:
        key = (row["sex"], row["age_group"])
        require(key in GROUPS and key not in groups, "invalid_or_duplicate_national_group")
        require(row["index_version"] == INDEX_VERSION and row["survey_year"] == "2025", "national_version_mismatch")
        scores = {d: finite(row[d + "_mean"], d) for d in DOMAINS}
        i5 = finite(row["i5_mean"], "i5_mean")
        weight = finite(row["weight_sum"], "weight_sum")
        n = int(row["n_valid"])
        require(n > 0 and weight > 0, "invalid_national_weight_or_count")
        require(all(0 <= x <= 100 for x in (i5, *scores.values())), "national_score_out_of_bounds")
        require(math.isclose(i5, sum(scores.values()) / 5, rel_tol=0, abs_tol=1e-9), "national_domain_identity")
        groups[key] = {"i5": i5, "domains": scores, "n_valid": n, "weight_sum": weight}
    require(set(groups) == set(GROUPS), "ten_national_groups_required")
    require(sum(v["n_valid"] for v in groups.values()) == 31817, "national_sample_count_mismatch")
    overall = sum(v["weight_sum"] * v["i5"] for v in groups.values()) / sum(v["weight_sum"] for v in groups.values())
    require(math.isclose(overall, 34.50915112817933, rel_tol=0, abs_tol=1e-9), "national_overall_changed")
    return groups


def validate_ten_to_six(national: dict, legacy_profile: Path) -> float:
    with legacy_profile.open(encoding="utf-8-sig", newline="") as stream:
        six = {(r["sex"], r["age_group"]): r for r in csv.DictReader(stream)}
    require(len(six) == 6, "legacy_six_groups_missing")
    maximum = 0.0
    for key, row in six.items():
        parts = [v for (sex, age), v in national.items() if (sex, parent_age(age)) == key]
        require(sum(v["n_valid"] for v in parts) == int(row["n_valid"]), "ten_to_six_n_mismatch")
        total = sum(v["weight_sum"] for v in parts)
        maximum = max(maximum, abs(total - float(row["weight_sum"])))
        require(math.isclose(total, float(row["weight_sum"]), rel_tol=1e-9, abs_tol=1e-9), "ten_to_six_weight_mismatch")
        for measure in ("i5", *DOMAINS):
            observed = sum(v["weight_sum"] * (v["i5"] if measure == "i5" else v["domains"][measure]) for v in parts) / total
            expected = float(row[measure + "_mean"])
            maximum = max(maximum, abs(observed - expected))
            require(math.isclose(observed, expected, rel_tol=0, abs_tol=1e-9), "ten_to_six_score_mismatch:" + measure)
    return maximum


def verified_legacy_bundle(bundle: str | Path) -> tuple[Path, dict]:
    bundle = Path(bundle)
    manifest = read_json(bundle / "manifest.json")
    require(manifest["status"] == "completed" and manifest["geographic_scope"] == "nationwide", "legacy_bundle_not_completed")
    require(manifest.get("contract_version") == "survey_area_isolation_index_v1"
            and manifest.get("is_synthetic") is False, "invalid_legacy_bundle")
    for name, digest in manifest["outputs"].items():
        path = (bundle / name).resolve()
        require(path.is_relative_to(bundle.resolve()) and sha(path) == digest, "legacy_bundle_hash_mismatch:" + name)
    return bundle, manifest


def verified_legacy_rows(level: str, bundle: str | Path = LEGACY) -> dict:
    bundle, manifest = verified_legacy_bundle(bundle)
    name = "sgg_isolation_index.jsonl" if level == "sgg" else "dong_isolation_index.jsonl"
    require(sha(bundle / name) == manifest["outputs"][name], "legacy_bundle_hash_mismatch")
    with (bundle / name).open(encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream]
    indexed = {(r["month"], r["area_code"]): r for r in rows}
    require(len(indexed) == len(rows), "legacy_duplicate_area_key")
    return indexed


def make_record(source: dict, old: dict, national: dict, profile_sha: str) -> dict:
    month, area, level = source["month"], source["area_code"], source["spatial_unit"]
    require(month in MONTHS and level in ("sgg", "administrative_dong"), "unsupported_source_scope")
    require(old["month"] == month and old["area_code"] == area and old["spatial_unit"] == level, "area_key_mismatch")
    require(source["index_version"] == INDEX_VERSION and source["area_index_version"] == AREA_VERSION, "source_version_mismatch")
    require(source["source_survey_profile_sha256"] == profile_sha, "survey_profile_hash_mismatch")
    require(source["score_status"] == "calculated" and source["local_survey_measured"] is False
            and source["operational_alert_allowed"] is False, "source_quality_mismatch")
    adult, total = int(source["population_20plus"]), int(source["population_total"])
    require(adult > 0 and total >= adult and adult == old["population_20plus"]
            and total == old["population_total"], "adult_population_mismatch")
    counts = source["group_populations"]
    weights = source["group_weights"]
    require(set(counts) == set(weights) == {group_key(k) for k in GROUPS}, "ten_population_keys_mismatch")
    require(all(type(counts[k]) is int and counts[k] >= 0 for k in counts) and sum(counts.values()) == adult,
            "ten_population_sum_mismatch")
    require(all(math.isclose(finite(weights[k], k), counts[k] / adult, rel_tol=0, abs_tol=1e-12) for k in counts),
            "ten_population_share_mismatch")
    require(math.isclose(sum(weights.values()), 1, rel_tol=0, abs_tol=1e-12), "ten_share_sum_mismatch")
    for sex, age in GROUPS:
        key = group_key((sex, age))
        parent = f"{sex}_{parent_age(age)}"
        # Compare each old parent only once, below.
        require(parent in old["group_populations"], "legacy_group_population_missing")
        benchmark = source["national_group_reference"][key]
        expected = national[sex, age]
        require(math.isclose(finite(benchmark["I5"], "source_national_I5"), expected["i5"], rel_tol=0, abs_tol=1e-9), "source_national_profile_mismatch")
        for domain in DOMAINS:
            require(math.isclose(finite(benchmark[domain], "source_national_domain"), expected["domains"][domain], rel_tol=0, abs_tol=1e-9), "source_national_profile_mismatch")
    for sex in ("남", "여"):
        for parent, children in (("20-39", ("20-29", "30-39")), ("40-59", ("40-49", "50-59")), ("60+", ("60+",))):
            require(sum(counts[f"{sex}_{child}"] for child in children) == old["group_populations"][f"{sex}_{parent}"],
                    "ten_to_six_area_population_mismatch")
    calculated = {"i5_reference": sum(weights[group_key(k)] * national[k]["i5"] for k in GROUPS)}
    calculated.update({d: sum(weights[group_key(k)] * national[k]["domains"][d] for k in GROUPS) for d in DOMAINS})
    raw_i5 = finite(source["isolation_index"], "isolation_index")
    require(math.isclose(raw_i5, calculated["i5_reference"], rel_tol=0, abs_tol=1e-9), "source_area_i5_mismatch")
    domains = {d: finite(source["domain_scores"][d], d) for d in DOMAINS}
    require(set(source["domain_scores"]) == set(DOMAINS), "source_domains_mismatch")
    require(all(math.isclose(domains[d], calculated[d], rel_tol=0, abs_tol=1e-9) for d in DOMAINS), "source_area_domain_mismatch")
    require(math.isclose(raw_i5, sum(domains.values()) / 5, rel_tol=0, abs_tol=1e-9), "area_domain_identity")
    require(all(0 <= value <= 100 for value in (raw_i5, *domains.values())), "area_score_out_of_bounds")
    region = source["region_code"]
    require(region == old["region_code"] and area.startswith(region), "region_key_mismatch")
    require(len(area) == (5 if level == "sgg" else 10) and area.isdigit(), "invalid_area_code")
    result = {
        "schema_version": CONTRACT, "record_id": f"survey_i5_10:2025:{level}:{area}:{month}",
        "month": month, "survey_year": 2025, "spatial_unit": level,
        "region_code": region, "region_name": old["region_name"], "area_code": area,
        "area_name": source["area_name"], "dong_code": area if level == "administrative_dong" else None,
        "index_name": "사회조사 기반 인구구성 고립취약성 참고지수(I5)",
        "index_kind": "population_composition_weighted_survey_reference",
        "isolation_index": raw_i5, "i5_reference": raw_i5,
        "scale": {"min": 0, "max": 100, "higher_means": "higher_reference_vulnerability"},
        "domain_scores": domains, "population_20plus": adult, "population_total": total,
        "population_basis": "MOIS_month_end_registered_population_20plus",
        "group_weights": weights, "group_populations": counts,
        "national_survey_n_valid": 31817, "local_survey_n_valid": None,
        "local_survey_measured": False, "is_isolation_rate": False,
        "behavior_signals_included": False, "quality": {"status": "reference_only", "formula_verified": True},
        "operational_alert_allowed": False, "source_contract": "survey_mois_reference_10group_v1",
        "source_index_version": INDEX_VERSION, "source_data_version": AREA_VERSION,
        "source_survey_profile_sha256": profile_sha,
        "source_mois_member_sha256": source["source_mois_member_sha256"],
        "limitations": ["national_survey_group_means_weighted_by_local_population_composition",
                        "not_direct_local_survey_measurement_or_isolation_rate",
                        "monthly_change_reflects_population_composition_not_repeated_survey",
                        "behavior_SKT_card_signals_not_combined_into_this_index"],
    }
    require(not (set(result) & FORBIDDEN), "display_fields_forbidden")
    return result


def build(source_root: str | Path, output_root: str | Path = DATA,
          legacy_bundle: str | Path = LEGACY) -> Path:
    source_root, output_root = Path(source_root), Path(output_root)
    require(not output_root.exists(), "output_directory_already_exists")
    source = source_inputs(source_root)
    legacy_bundle, legacy_manifest = verified_legacy_bundle(legacy_bundle)
    registry = legacy_bundle / "area_registry.json"
    require(sha(registry) == legacy_manifest["outputs"].get("area_registry.json"), "legacy_area_registry_hash_mismatch")
    survey = source_root / "02_survey_10group"
    nationwide = source_root / "03_nationwide_10group"
    monthly_mois_sha = read_json(nationwide / "manifest.json")["monthly_member_sha256"]
    profile_path = survey / "national_ten_group_profile.csv"
    profile_sha = sha(profile_path)
    national = national_profiles(profile_path)
    maximum = validate_ten_to_six(national, legacy_bundle / "national_six_group_profile.csv")
    mapping = read_json(survey / "age_group_mapping.json")
    require("20-29" in json.dumps(mapping) and "60+" in json.dumps(mapping), "age_mapping_missing")
    schema = read_json(SCHEMA)
    group_schema = read_json(GROUP_SCHEMA)
    record_validator = jsonschema.Draft202012Validator(schema)
    group_validator = jsonschema.Draft202012Validator(group_schema)
    output_root.mkdir(parents=True)
    (output_root / "area_registry.json").write_bytes(registry.read_bytes())
    (output_root / "national_ten_group_profile.csv").write_bytes(profile_path.read_bytes())
    write_json(output_root / "age_group_mapping.json", mapping)
    counts_by_level = {}
    max_score_error = 0.0
    max_domain_error = 0.0
    for level, source_name, output_name in (("sgg", "sgg_10group_recomputed.jsonl", "sgg_isolation_index.jsonl"),
                                            ("administrative_dong", "dong_10group_recomputed.jsonl", "dong_isolation_index.jsonl")):
        old = verified_legacy_rows(level, legacy_bundle)
        seen = set()
        count = 0
        with (nationwide / source_name).open(encoding="utf-8") as stream, (output_root / output_name).open("w", encoding="utf-8") as out:
            for line in stream:
                source_row = json.loads(line)
                require(source_row["source_mois_member_sha256"] == monthly_mois_sha[source_row["month"]],
                        "monthly_mois_source_hash_mismatch")
                key = (source_row["month"], source_row["area_code"])
                require(key in old and key not in seen, "area_key_mismatch_or_duplicate")
                seen.add(key)
                record = make_record(source_row, old[key], national, profile_sha)
                record_validator.validate(record)
                max_score_error = max(max_score_error, abs(record["i5_reference"] - source_row["isolation_index"]))
                max_domain_error = max(max_domain_error, *(abs(record["domain_scores"][d] - source_row["domain_scores"][d]) for d in DOMAINS))
                out.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                count += 1
        require(set(old) == seen, "area_key_set_mismatch")
        require(count == (1584 if level == "sgg" else 21698), "area_row_count_mismatch")
        counts_by_level[level] = count
    unscored = read_json(nationwide / "unscored_areas.json")
    require(len(unscored["entries"]) == 16, "unscored_areas_changed")
    write_json(output_root / "unscored_areas.json", unscored)
    validation = {"status": "passed_with_known_source_limits", "national_groups": 10,
                  "national_complete_respondents": 31817,
                  "national_i5_weighted_mean": sum(v["weight_sum"] * v["i5"] for v in national.values()) / sum(v["weight_sum"] for v in national.values()),
                  "ten_to_six_max_abs_error": maximum, "area_rows": counts_by_level,
                  "max_area_i5_copy_error": max_score_error, "max_area_domain_copy_error": max_domain_error,
                  "zero_adult_unscored_rows": 16,
                  "july_november_official_40_column_direct_reconciliation": "not_verified",
                  "zero_population_july_november_13_rows": "not_verified",
                  "quality_status": "hold", "operational_alert_allowed": False,
                  "display_conversion_applied": False,
                  "legacy_bundle_manifest_sha256": sha(legacy_bundle / "manifest.json")}
    write_json(output_root / "validation_report.json", validation)
    outputs = {p.name: sha(p) for p in output_root.iterdir() if p.is_file()}
    run_id = AREA_VERSION + "_" + hashlib.sha256(json.dumps(outputs, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    manifest = {"contract_version": CONTRACT, "group_contract_version": GROUP_CONTRACT,
                "run_id": run_id,
                "status": "completed", "is_synthetic": False, "geographic_scope": "nationwide",
                "source_population_coverage_verified": False,
                "source_index_version": INDEX_VERSION, "area_index_version": AREA_VERSION,
                "score_scale": "0_to_100_raw_equal_five_domains", "source_rebuild": source,
                "source_rebuild_branch": "backup/i5-10group-rebuild-20261025",
                "legacy_bundle_manifest_sha256": sha(legacy_bundle / "manifest.json"),
                "code_sha256": sha(Path(__file__)), "schema_sha256": sha(SCHEMA),
                "group_schema_sha256": sha(GROUP_SCHEMA), "validation": validation,
                "outputs": outputs}
    write_json(output_root / "manifest.json", manifest)
    return output_root


def verified_bundle(bundle: str | Path) -> tuple[Path, dict]:
    bundle = Path(bundle)
    manifest = read_json(bundle / "manifest.json")
    require(manifest.get("contract_version") == CONTRACT and manifest.get("status") == "completed"
            and manifest.get("is_synthetic") is False, "invalid_ten_group_bundle")
    required = {"sgg_isolation_index.jsonl", "dong_isolation_index.jsonl", "national_ten_group_profile.csv",
                "age_group_mapping.json", "unscored_areas.json", "validation_report.json", "area_registry.json"}
    require(isinstance(manifest.get("outputs"), dict) and required.issubset(manifest["outputs"]),
            "untracked_required_output")
    require(manifest.get("schema_sha256") == sha(SCHEMA) and manifest.get("group_schema_sha256") == sha(GROUP_SCHEMA), "schema_hash_mismatch")
    for name, digest in manifest["outputs"].items():
        path = (bundle / name).resolve()
        require(path.is_relative_to(bundle.resolve()) and sha(path) == digest, "bundle_output_hash_mismatch:" + name)
    return bundle, manifest


def query_national_groups(bundle: str | Path = DATA, sex: str | None = None, age_group: str | None = None) -> dict:
    bundle, manifest = verified_bundle(bundle)
    require(sex is None or sex in ("남", "여"), "unsupported_sex")
    require(age_group is None or age_group in {age for _, age in GROUPS}, "unsupported_age_group")
    national = national_profiles(bundle / "national_ten_group_profile.csv")
    validator = jsonschema.Draft202012Validator(read_json(GROUP_SCHEMA))
    rows = []
    for s, age in GROUPS:
        if (sex is not None and s != sex) or (age_group is not None and age != age_group):
            continue
        data = national[s, age]
        row = {"sex": s, "age_group": age, "i5_reference": data["i5"],
               "domain_scores": data["domains"], "national_survey_n_valid": data["n_valid"],
               "survey_weight_sum": data["weight_sum"],
               "reference_kind": "national_survey_ten_group_mean", "local_survey_measured": False}
        validator.validate(row)
        rows.append(row)
    return {"contract_version": GROUP_CONTRACT, "source_index_version": INDEX_VERSION,
            "local_survey_measured": False, "total_matched": len(rows), "records": rows}


def with_group_profiles(record: dict, national: dict) -> dict:
    """Attach national ten-group benchmarks and the record's local composition."""
    profiles = []
    for sex, age in GROUPS:
        score = national[sex, age]
        key = group_key((sex, age))
        profiles.append({"sex": sex, "age_group": age, "i5_reference": score["i5"],
                         "domain_scores": score["domains"], "national_survey_n_valid": score["n_valid"],
                         "survey_weight_sum": score["weight_sum"],
                         "reference_kind": "national_survey_ten_group_mean", "local_survey_measured": False,
                         "population": record["group_populations"][key],
                         "population_share": record["group_weights"][key]})
    return {**record, "group_profiles": profiles}


def query(bundle: str | Path = DATA, level: str = "sgg", month: str | None = None,
          region_code: str | None = None, dong_code: str | None = None,
          offset: int = 0, limit: int = 100) -> dict:
    require(level in ("sgg", "administrative_dong"), "unsupported_level")
    require(month is None or month in MONTHS, "unsupported_month")
    require(offset >= 0 and 1 <= limit <= 500, "invalid_pagination")
    if region_code is not None:
        validate_region_code(region_code)
    require(dong_code is None or (level == "administrative_dong" and len(dong_code) == 10 and dong_code.isdigit()), "invalid_dong_filter")
    bundle, manifest = verified_bundle(bundle)
    name = "sgg_isolation_index.jsonl" if level == "sgg" else "dong_isolation_index.jsonl"
    national = national_profiles(bundle / "national_ten_group_profile.csv")
    validator = jsonschema.Draft202012Validator(read_json(SCHEMA))
    matched = []
    with (bundle / name).open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            if (month is not None and record["month"] != month) or (region_code is not None and record["region_code"] != region_code) or (dong_code is not None and record["dong_code"] != dong_code):
                continue
            validator.validate(record)
            matched.append(record)
    selected = [with_group_profiles(record, national) for record in matched[offset:offset + limit]]
    presence = availability(matched, region_code, month)
    if not matched and dong_code is not None:
        missing = read_json(bundle / "unscored_areas.json")["entries"]
        if any(r["dong_code"] == dong_code and (month is None or r["month"] == month) for r in missing):
            presence["reason"] = "zero_registered_adult_population"
    result = {"contract_version": CONTRACT,
              "run_id": manifest["run_id"],
              "index_kind": "population_composition_weighted_survey_reference",
              "quality_status": "hold", "geographic_scope": "nationwide",
              "source_population_coverage_verified": manifest.get("source_population_coverage_verified", False), "local_survey_measured": False,
              "operational_alert_allowed": False, "national_group_reference_available": True,
              "availability": presence, "total_matched": len(matched), "records": selected,
              "next_offset": offset + limit if offset + limit < len(matched) else None}
    require(not (set(result) & FORBIDDEN), "display_fields_forbidden")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_cmd = commands.add_parser("build")
    build_cmd.add_argument("--source-root", required=True)
    build_cmd.add_argument("--output-root", default=str(DATA))
    build_cmd.add_argument("--legacy-bundle", default=str(LEGACY))
    groups = commands.add_parser("groups")
    groups.add_argument("--bundle", default=str(DATA))
    groups.add_argument("--sex", choices=("남", "여"))
    groups.add_argument("--age-group", choices=tuple(dict.fromkeys(a for _, a in GROUPS)))
    reader = commands.add_parser("query")
    reader.add_argument("--bundle", default=str(DATA))
    reader.add_argument("--level", choices=("sgg", "administrative_dong"), default="sgg")
    reader.add_argument("--month")
    reader.add_argument("--region-code")
    reader.add_argument("--dong-code")
    reader.add_argument("--offset", type=int, default=0)
    reader.add_argument("--limit", type=int, default=100)
    args = vars(parser.parse_args())
    command = args.pop("command")
    if command == "build":
        print(json.dumps({"bundle": str(build(**args))}, ensure_ascii=False))
    elif command == "groups":
        print(json.dumps(query_national_groups(**args), ensure_ascii=False, allow_nan=False))
    else:
        print(json.dumps(query(**args), ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
