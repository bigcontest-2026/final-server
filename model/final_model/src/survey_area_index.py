"""Additive I5 area-reference export; existing behavior bundles are never edited."""
from __future__ import annotations

from .i5_display import calculate_i5_display_score, REFERENCE_SCOPE, REFERENCE_VERSION

import argparse
import csv
import hashlib
import io
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import jsonschema

from .common import dump, read_jsonl, sha, write_jsonl
from .geography import region_registry, validate_region_code, availability

FINAL = Path(__file__).resolve().parents[1]
CONTRACT = "survey_area_isolation_index_v1"
GROUPS = tuple((sex, age) for sex in ("남", "여") for age in ("20-39", "40-59", "60+"))
DOMAINS = ("E", "P", "O", "C", "U")
FIELDS = ("i5_reference",) + tuple(name + "_reference" for name in DOMAINS)
REGIONS = {"11680": "강남구", "51110": "춘천시"}
LIMITATIONS = [
    "national_survey_group_means_weighted_by_local_population_composition",
    "not_direct_local_survey_measurement_or_isolation_rate",
    "monthly_change_reflects_population_composition_not_repeated_survey",
    "behavior_SKT_card_signals_not_combined_into_this_index",
]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_number:" + label) from exc
    require(math.isfinite(result), "nonfinite:" + label)
    return result


def canonical_bytes(path):
    # Git's Windows CRLF checkout differs only in line endings from frozen SHA.
    return Path(path).read_bytes().replace(b"\r\n", b"\n")


def read_frozen(source):
    source = Path(source)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    require(manifest["index_version"] == "I5_2025_final_v1", "unsupported_I5_version")
    tables, fingerprints = {}, {}
    for name in ("national_six_group_profile.csv", "dong_month_six_group_population.csv",
                 "region_month_summary.csv", "dong_month_reference.csv"):
        content = canonical_bytes(source / name)
        digest = hashlib.sha256(content).hexdigest()
        require(digest == manifest["files"][name]["sha256"], "frozen_hash_mismatch:" + name)
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline="")))
        require(len(rows) == manifest["files"][name]["rows"], "frozen_row_count_mismatch:" + name)
        tables[name] = rows
        fingerprints[name] = {"canonical_sha256": digest, "local_file_sha256": sha(source / name)}
    return tables, fingerprints, manifest


def profiles(rows):
    indexed = {}
    for row in rows:
        key = row["sex"], row["age_group"]
        require(key not in indexed, "duplicate_national_group")
        values = {domain: number(row[domain + "_mean"], domain) for domain in DOMAINS}
        i5 = number(row["i5_mean"], "i5_mean")
        require(all(0 <= value <= 100 for value in [i5, *values.values()]), "score_out_of_range")
        require(math.isclose(i5, sum(values.values()) / 5, abs_tol=1e-9), "national_I5_formula_mismatch")
        indexed[key] = {"i5": i5, "domains": values, "n_valid": int(row["n_valid"])}
    require(set(indexed) == set(GROUPS), "six_national_groups_required")
    return indexed


def make_record(row, level, national, source_version, regions=None):
    regions = regions or REGIONS
    require(level in ("sgg", "administrative_dong"), "unsupported_level")
    month, region = row["month"], row["region_code"]
    require(month in {f"2025{m:02d}" for m in range(7, 13)}, "unsupported_month")
    require(region in regions and row["region"] == regions[region], "region_code_name_mismatch")
    population = {key: int(row["pop_" + "_".join(key)]) for key in GROUPS}
    require(all(value >= 0 for value in population.values()), "negative_population")
    total = sum(population.values())
    require(total > 0 and total == int(row["population_20plus"]), "population_20plus_denominator_mismatch")
    require(int(row["population_total"]) >= total, "total_population_less_than_adults")
    weights = {key: population[key] / total for key in GROUPS}
    for key, weight in weights.items():
        require(math.isclose(number(row["share_" + "_".join(key)], "share"), weight, abs_tol=1e-12),
                "source_share_mismatch")
    expected = {"i5_reference": sum(weights[key] * national[key]["i5"] for key in GROUPS)}
    expected.update({name + "_reference": sum(weights[key] * national[key]["domains"][name] for key in GROUPS)
                     for name in DOMAINS})
    for name, value in expected.items():
        require(math.isclose(number(row[name], name), value, abs_tol=1e-9), "area_I5_recalculation_mismatch:" + name)
    dong = row["dong_code"] if level == "administrative_dong" else None
    require(dong is None or (len(dong) == 10 and dong.isdigit() and dong[:5] == region), "invalid_dong_code")
    area = dong or region
    result = {
        "schema_version": CONTRACT,
        "record_id": f"survey_i5:2025:{level}:{area}:{month}",
        "month": month,
        "survey_year": 2025,
        "spatial_unit": level,
        "region_code": region,
        "region_name": regions[region],
        "area_code": area,
        "area_name": row["dong"] if dong else regions[region],
        "dong_code": dong,
        "index_name": "사회조사 기반 인구구성 고립취약성 참고지수(I5)",
        "index_kind": "population_composition_weighted_survey_reference",
        # Preserve the frozen published-reference value without rounding.
        "isolation_index": number(row["i5_reference"], "i5_reference"),
        "i5_reference": number(row["i5_reference"], "i5_reference"),
        "scale": {"min": 0, "max": 100, "higher_means": "higher_reference_vulnerability"},
        "domain_scores": {name: number(row[name + "_reference"], name) for name in DOMAINS},
        "population_20plus": total,
        "population_total": int(row["population_total"]),
        "population_basis": "MOIS_month_end_registered_population_20plus",
        "group_weights": {"_".join(key): weights[key] for key in GROUPS},
        "national_survey_n_valid": sum(value["n_valid"] for value in national.values()),
        "local_survey_n_valid": None,
        "local_survey_measured": False,
        "is_isolation_rate": False,
        "behavior_signals_included": False,
        "quality": {"status": "reference_only", "formula_verified": True},
        "operational_alert_allowed": False,
        "source_contract": "survey_mois_reference_v1",
        "source_index_version": "I5_2025_final_v1",
        "source_data_version": source_version,
        "limitations": LIMITATIONS,
    }

    result.update(calculate_i5_display_score(result["isolation_index"]))
    return result


def build_records(tables, source_version):
    national = profiles(tables["national_six_group_profile.csv"])
    regions = {}
    for row in tables["region_month_summary.csv"]:
        require(row["region_code"] not in regions or regions[row["region_code"]] == row["region"], "region_code_name_mismatch")
        regions[row["region_code"]] = row["region"]
    regions = region_registry({"regions": regions})
    sgg = [make_record(row, "sgg", national, source_version, regions) for row in tables["region_month_summary.csv"]]
    dong = [make_record(row, "administrative_dong", national, source_version, regions)
            for row in tables["dong_month_reference.csv"]]
    require(len({r["record_id"] for r in sgg + dong}) == len(sgg) + len(dong), "duplicate_area_month_key")
    require(bool(sgg) and bool(dong), "empty_area_panel")
    require({(r["month"],r["region_code"]) for r in dong} == {(r["month"],r["region_code"]) for r in sgg}, "area_month_coverage_mismatch")
    # Cross-check the separately exported MOIS six-group population table.
    detail = {}
    for row in tables["dong_month_six_group_population.csv"]:
        key = row["month"], row["dong_code"], row["sex"], row["age_group"]
        require(key not in detail, "duplicate_dong_population_group")
        detail[key] = int(row["population"])
    expected_detail = {(r["month"],r["dong_code"],sex,age) for r in dong for sex,age in GROUPS}
    require(set(detail) == expected_detail, "unexpected_dong_population_rows")
    dong_sources = tables["dong_month_reference.csv"]
    for row in dong_sources:
        for sex, age in GROUPS:
            require(detail[row["month"], row["dong_code"], sex, age] == int(row[f"pop_{sex}_{age}"]),
                    "MOIS_population_table_mismatch")
    for row in tables["region_month_summary.csv"]:
        selected = [d for d in dong_sources if (d["month"], d["region_code"]) == (row["month"], row["region_code"])]
        for sex, age in GROUPS:
            require(sum(int(d[f"pop_{sex}_{age}"]) for d in selected) == int(row[f"pop_{sex}_{age}"]),
                    "sgg_dong_population_mismatch")
    return sgg, dong


def default_source():
    config = FINAL / "configs/local.json"
    if not config.exists():
        config = FINAL / "configs/example.json"
    value = json.loads(config.read_text(encoding="utf-8-sig"))["index_root"]
    root = Path(value)
    if not root.is_absolute():
        root = (FINAL.parent / root).resolve()
    return root / "v3/exports/2025_final_v1"


def build(source=None, output_root=None, *, publish_latest=True):
    source = Path(source) if source else default_source()
    tables, inputs, upstream = read_frozen(source)
    sgg, dong = build_records(tables, upstream["data_version"])
    schema = json.loads((FINAL / "schemas/survey_area_index.schema.json").read_text(encoding="utf-8-sig"))
    validator = jsonschema.Draft202012Validator(schema)
    for record in sgg + dong:
        validator.validate(record)
    # An independent output family: never write into existing behavior runs or latest.json.
    run_id = datetime.now(timezone.utc).strftime("survey_area_index_%Y%m%d_%H%M%S_%f")
    output_root = Path(output_root) if output_root else FINAL / "outputs"
    out = output_root / run_id
    out.mkdir(parents=True, exist_ok=False)
    write_jsonl(out / "sgg_isolation_index.jsonl", sorted(sgg, key=lambda r: (r["month"], r["area_code"])))
    write_jsonl(out / "dong_isolation_index.jsonl", sorted(dong, key=lambda r: (r["month"], r["area_code"])))
    validation = {
        "status": "passed", "sgg_month_rows": len(sgg), "dong_month_rows": len(dong),
        "sgg_count": len({r["area_code"] for r in sgg}), "dong_count": len({r["area_code"] for r in dong}),
        "months": sorted({r["month"] for r in sgg}),
        "frozen_I5_scores_preserved": True, "weighted_scores_recalculated": len(sgg) + len(dong),
        "MOIS_six_group_rows_verified": len(tables["dong_month_six_group_population.csv"]), "schema_records_validated": len(sgg) + len(dong),
        "behavior_outputs_written": False,
    }
    dump(out / "validation_report.json", validation)
    dump(out / "manifest.json", {
        "contract_version": CONTRACT, "run_id": run_id, "status": "completed", "is_synthetic": False,
        "source_directory": str(source.resolve()), "source_manifest_sha256": sha(source / "manifest.json"),
        "inputs": inputs, "code_sha256": sha(Path(__file__)),
        "schema_sha256": sha(FINAL / "schemas/survey_area_index.schema.json"),
        "validation": validation,
        "outputs": {p.name: sha(p) for p in sorted(out.iterdir()) if p.is_file()},
    })
    if publish_latest:
        dump(output_root / "survey_area_index_latest.json", {"run_id": run_id})
    return out


def query(bundle, level="sgg", month=None, region_code=None, dong_code=None, offset=0, limit=100):
    require(level in ("sgg", "administrative_dong"), "unsupported_level")
    require(offset >= 0 and 1 <= limit <= 500, "invalid_pagination")
    require(month is None or month in {f"2025{m:02d}" for m in range(7, 13)}, "unsupported_month")
    if region_code is not None: validate_region_code(region_code)
    require(dong_code is None or (level == "administrative_dong" and len(dong_code) == 10
                                  and dong_code.isdigit()), "invalid_dong_filter")
    bundle = Path(bundle)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8-sig"))
    if manifest.get("contract_version") == "survey_area_isolation_index_10group_v1":
        from .survey_area_index_ten import query as query_ten
        return query_ten(bundle, level=level, month=month, region_code=region_code,
                         dong_code=dong_code, offset=offset, limit=limit)
    require(manifest.get("contract_version") == CONTRACT and manifest.get("status") == "completed"
            and manifest.get("is_synthetic") is False, "invalid_survey_area_bundle")
    for name, digest in manifest["outputs"].items():
        path = (bundle / name).resolve()
        require(path.is_relative_to(bundle.resolve()) and sha(path) == digest, "survey_area_hash_mismatch")
    name = "sgg_isolation_index.jsonl" if level == "sgg" else "dong_isolation_index.jsonl"
    filters = {"month": month, "region_code": region_code, "dong_code": dong_code}
    rows = [r for r in read_jsonl(bundle / name)
            if all(value is None or r[key] == value for key, value in filters.items())]
    presence=availability(rows,region_code,month)
    if not rows and dong_code and (bundle/"unscored_areas.json").exists():
        missing=json.loads((bundle/"unscored_areas.json").read_text(encoding="utf-8"))
        if any(r["dong_code"]==dong_code and (month is None or r["month"]==month) for r in missing):
            presence["reason"]="zero_registered_adult_population"
    return {
        "contract_version": CONTRACT, "index_kind": "population_composition_weighted_survey_reference",
        "i5_display_reference_version": REFERENCE_VERSION, "i5_display_reference_scope": REFERENCE_SCOPE,
        "quality_status":"hold",
        "geographic_scope":manifest.get("geographic_scope","published_bundle_only"),
        "source_population_coverage_verified":manifest.get("national_coverage",{}).get("nationwide_published_population_coverage_verified",False),
        "local_survey_measured": False, "operational_alert_allowed": False,
        "availability": presence,
        "total_matched": len(rows), "records": rows[offset:offset + limit],
        "next_offset": offset + limit if offset + limit < len(rows) else None,
    }


def main():
    parser = argparse.ArgumentParser(description="시군구·행정동별 V3 I5 인구구성 참고지수")
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("--source")
    builder.add_argument("--output-root")
    reader = commands.add_parser("query")
    reader.add_argument("--bundle", required=True)
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
    else:
        print(json.dumps(query(**args), ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
