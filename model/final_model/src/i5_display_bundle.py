"""Add the fixed display projection to a verified, published nationwide I5 bundle."""
import argparse
import json
import statistics
from pathlib import Path

from .common import dump, read_jsonl, sha, write_jsonl
from .i5_display import (
    REFERENCE_MEAN, REFERENCE_SCOPE, REFERENCE_STD, REFERENCE_VERSION,
    calculate_i5_display_score,
)

FILES = ("sgg_isolation_index.jsonl", "dong_isolation_index.jsonl")
SCHEMA = Path(__file__).resolve().parents[1] / "schemas/survey_area_index.schema.json"
TRANSFORM = Path(__file__).resolve().with_name("i5_display.py")


def distribution(rows):
    scores = [row["i5_display_score"] for row in rows]
    return {
        "rows": len(rows),
        "raw_min": min(row["isolation_index"] for row in rows),
        "raw_max": max(row["isolation_index"] for row in rows),
        "display_min": min(scores),
        "display_max": max(scores),
        "display_mean": statistics.fmean(scores),
        "clipped_rows": sum(row["i5_display_clipped"] for row in rows),
        "clipped_fraction": sum(row["i5_display_clipped"] for row in rows) / len(rows),
    }


def add_display(bundle):
    bundle = Path(bundle)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("geographic_scope") != "nationwide" or manifest.get("status") != "completed":
        raise ValueError("completed_nationwide_bundle_required")
    for name, digest in manifest["outputs"].items():
        if sha(bundle / name) != digest:
            raise ValueError("bundle_hash_mismatch:" + name)
    original = {name: read_jsonl(bundle / name) for name in FILES}
    if any("i5_display_score" in row for rows in original.values() for row in rows):
        raise ValueError("display_already_present")
    if tuple(map(len, original.values())) != (1584, 21698):
        raise ValueError("unexpected_national_coverage")
    december = [row for row in original[FILES[0]] if row["month"] == "202512"]
    # Published general districts have a parent city in their name; keep autonomous
    # districts, Sejong, Jeju/Seogwipo and the parent cities themselves.
    excluded = [row for row in december if "시 " in row["region_name"] and row["region_name"].endswith("구")]
    reference = [row["isolation_index"] for row in december if row not in excluded]
    if len(december) != 264 or len(excluded) != 35 or len(reference) != 229:
        raise ValueError("reference_membership_mismatch")
    mean, std = statistics.mean(reference), statistics.pstdev(reference)
    if abs(mean - REFERENCE_MEAN) > 1e-12 or abs(std - REFERENCE_STD) > 1e-12:
        raise ValueError("reference_statistics_mismatch")
    updated = {}
    for name, rows in original.items():
        updated[name] = []
        for row in rows:
            item = dict(row)
            item.update(calculate_i5_display_score(row["isolation_index"]))
            updated[name].append(item)
    report = {
        "reference_version": REFERENCE_VERSION,
        "reference_scope": REFERENCE_SCOPE,
        "reference_month": "202512",
        "reference_count": 229,
        "reference_mean_recalculated": mean,
        "reference_std_recalculated_population": std,
        "reference_mean_fixed": REFERENCE_MEAN,
        "reference_std_fixed": REFERENCE_STD,
        "excluded_general_district_codes": sorted(row["region_code"] for row in excluded),
        "source_file_sha256": {name: manifest["outputs"][name] for name in FILES},
        "source_records_and_existing_fields_preserved": True,
        "levels": {
            level: {
                "all_months": distribution(updated[name]),
                "202512": distribution([row for row in updated[name] if row["month"] == "202512"]),
            }
            for level, name in (("sgg", FILES[0]), ("administrative_dong", FILES[1]))
        },
        "interpretation": "행정동 값도 2025년 12월 시군구 기준 상대점수이며 행정동 집단 내 표준화 점수가 아니다.",
    }
    for name, rows in updated.items():
        write_jsonl(bundle / name, rows)
        manifest["outputs"][name] = sha(bundle / name)
    report_name = "i5_display_validation_report.json"
    dump(bundle / report_name, report)
    manifest["outputs"][report_name] = sha(bundle / report_name)
    manifest["i5_display_reference_version"] = REFERENCE_VERSION
    manifest["source_schema_sha256"] = manifest["schema_sha256"]
    manifest["schema_sha256"] = sha(SCHEMA)
    manifest["i5_display_code_sha256"] = sha(TRANSFORM)
    dump(bundle / "manifest.json", manifest)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle")
    args = parser.parse_args()
    print(json.dumps(add_display(args.bundle), ensure_ascii=False, indent=2))
