"""Read-only query interface. No legacy snapshot replacement."""
import argparse,csv,json
from pathlib import Path
from .common import read_jsonl,verify_bundle
from .geography import validate_region_code, availability
from .pipeline import historical_key

def query(root,region_code=None,month=None,sex_code=None,age_code=None,candidate_only=False,offset=0,limit=20):
    if offset<0 or not 1<=limit<=100: raise ValueError("invalid_pagination")
    if month and (len(month)!=6 or not month.isdigit() or not 1<=int(month[4:])<=12): raise ValueError("invalid_month")
    if region_code is not None: validate_region_code(region_code)
    if sex_code and sex_code not in ("MAN","WMAN"): raise ValueError("unsupported_sex")
    if age_code and age_code not in ("20G","30G","40G","50G","60GU"): raise ValueError("unsupported_age")
    root=Path(root)
    manifest=verify_bundle(root)
    return _query_verified(root,manifest,region_code,month,sex_code,age_code,candidate_only,offset,limit)


def _query_verified(root,manifest,region_code=None,month=None,sex_code=None,age_code=None,candidate_only=False,offset=0,limit=20):
    """Internal projection for checked artifacts; public queries always verify completion."""
    history={r["record_id"]:r for r in read_jsonl(root/"historical_reference.jsonl")}
    filters=dict(region_code=region_code,month=month,sex_code=sex_code,age_code=age_code)
    observed=[r for r in read_jsonl(root/"behavior_evidence.jsonl") if all(v is None or r["key"][k]==v for k,v in filters.items())]
    rows=[r for r in observed if not candidate_only or r["rule"]["candidate"] is True or r["robust"]["candidate"] is True]
    for row in rows:
        reference_id = row["historical_reference_id"]
        if reference_id is None:
            continue
        reference = history.get(reference_id)
        regions = {row["key"]["region_code"]: row["region_name"]}
        key = tuple(row["key"][name] for name in ("month", "region_code", "sex_code", "age_code"))
        if reference is None or historical_key(reference, regions) != key:
            raise ValueError("invalid_behavior_historical_reference")
    return {"contract_version":"final_activity_evidence_v1","run_id":manifest["run_id"],"quality_status":"hold","operational_alert_allowed":False,
            "availability":availability(observed,region_code,month),
            "total_matched":len(rows),"offset":offset,"next_offset":offset+limit if offset+limit<len(rows) else None,
            "records":[{"behavior":r,"historical_reference":history.get(r["historical_reference_id"])} for r in rows[offset:offset+limit]],
            "survey_reference":{"contract_version":"survey_mois_reference_v1","path":"survey_mois_reference/","automatic_10_to_6_group_join":False}}

def survey_query(root,table="national_six_group_profile",**filters):
    root=Path(root);verify_bundle(root)
    allowed={"national_six_group_profile","national_overall_reference","national_item_profile","dong_month_reference","dong_month_six_group_population","region_month_summary"}
    if table not in allowed: raise ValueError("unsupported_survey_table")
    with (root/"survey_mois_reference"/(table+".csv")).open(encoding="utf-8-sig",newline="") as f:
        reader=csv.DictReader(f)
        if set(filters)-set(reader.fieldnames): raise ValueError("unknown_filter")
        return [r for r in reader if all(r[k]==str(v) for k,v in filters.items())]

def main():
    p=argparse.ArgumentParser();p.add_argument("--bundle",required=True)
    for key in ("region-code","month","sex-code","age-code"): p.add_argument("--"+key)
    p.add_argument("--candidate-only",action="store_true");p.add_argument("--offset",type=int,default=0);p.add_argument("--limit",type=int,default=20)
    args=vars(p.parse_args());root=args.pop("bundle")
    print(json.dumps(query(root,**args),ensure_ascii=False,allow_nan=False))
if __name__=="__main__": main()


def external_query(root, experiment, offset=0, limit=20, **filters):
    """Use original external dimensions; never fabricate monthly/sex-age joins."""
    root=Path(root);verify_bundle(root)
    if offset<0 or not 1<=limit<=100:raise ValueError("invalid_pagination")
    experiment=experiment.lower()
    if experiment=="legacy_seoul":
        from .legacy_seoul import query as legacy_query
        return legacy_query(root/"external_context/legacy_seoul",offset=offset,limit=limit,**filters)
    if experiment not in ("c1","m1","m2"):raise ValueError("external_not_comparable_or_unsupported")
    rows=read_jsonl(root/"external_context"/experiment/"agent_evidence.jsonl")
    allowed=set.intersection(*(set(r) for r in rows)) if rows else set()
    if set(filters)-allowed:raise ValueError("unsupported_original_dimension")
    selected=[r for r in rows if all(r[k]==v for k,v in filters.items())]
    return {"experiment":experiment,"role":"separate_background_reference","main_model_join":False,
            "availability":availability(selected,filters.get("district_code"),filters.get("quarter")),
            "quality_status":"hold","operational_alert_allowed":False,"total_matched":len(selected),"records":selected[offset:offset+limit],
            "next_offset":offset+limit if offset+limit<len(selected) else None}
