"""Agent-facing coverage discovery from verified artifacts, never from code allowlists."""
import argparse
import json
from collections import defaultdict
from pathlib import Path

from .common import verify_bundle,read_jsonl
from .geography import validate_region_code


def get_region_availability(bundle,region_code=None):
    if region_code is not None:validate_region_code(region_code)
    bundle=Path(bundle);manifest=verify_bundle(bundle)
    entries=defaultdict(lambda: {"months":set(),"quarters":set(),"sources":set(),"rows":0})
    for row in read_jsonl(bundle/"behavior_evidence.jsonl"):
        code=row["key"]["region_code"]
        entry=entries[code,"behavior"]
        entry["months"].add(row["key"]["month"]);entry["rows"]+=1
        for name,value in row.get("sources",{}).items():
            if value.get("activity_daily") is not None:entry["sources"].add(name)
    c1=bundle/"external_context/c1/agent_evidence.jsonl"
    if c1.exists():
        for row in read_jsonl(c1):
            entry=entries[row["district_code"],"c1"]
            entry["quarters"].add(row["quarter"]);entry["rows"]+=1
    legacy=bundle/"external_context/legacy_seoul/agent_evidence.jsonl"
    if legacy.exists():
        for row in read_jsonl(legacy):
            entry=entries[row["dong_code"][:5],"legacy_seoul"]
            entry["months"].add(row["period"].replace("-",""));entry["rows"]+=1
    records=[]
    for (code,family),entry in sorted(entries.items()):
        if region_code is not None and code!=region_code:continue
        records.append(dict(region_code=code,evidence_family=family,**{
            k:sorted(v) if isinstance(v,set) else v for k,v in entry.items()},
            geographic_completeness_verified=False,operational_alert_allowed=False))
    return dict(contract_version="regional_coverage_v1",run_id=manifest["run_id"],
                status="available" if records else "data_unavailable",region_code=region_code,
                region_existence_verified=bool(records),records=records,
                scope="observations_in_this_bundle_not_national_coverage",
                survey_area_lookup="final_model.src.survey_area_index.query",operational_alert_allowed=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle",required=True);parser.add_argument("--region-code")
    print(json.dumps(get_region_availability(**vars(parser.parse_args())),ensure_ascii=False))


if __name__=="__main__":main()
