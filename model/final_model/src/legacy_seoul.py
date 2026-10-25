"""Preserve the 417 Seoul reference observations without changing final-model scores."""
import argparse
import csv
import hashlib
import json
import math
import re
import shutil
from datetime import datetime,timezone
from pathlib import Path

from .common import dump,load_module,read_jsonl,sha,write_jsonl
from .geography import validate_region_code,availability

CONTRACT="legacy_seoul_behavior_reference_v1"
DIMENSIONS=("economic_instability","social_activity","isolated_lifestyle","social_relations")
SOURCE_FILES=("service_payload.json","code_mapping.csv","index_scores.csv","validation.json",
              "source_manifest.json","service_verification.json","protocol.json","normalization.json")
FINAL=Path(__file__).resolve().parents[1]


def require(condition,message):
    if not condition:raise ValueError(message)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def unique(rows,field):
    result={row[field]:row for row in rows}
    require(len(result)==len(rows),"duplicate_reference_key:"+field)
    return result


def check_source(source,snapshot):
    source=Path(source);snapshot=Path(snapshot)
    payload=load(source/"service_payload.json");records=payload["records"]
    # Read the original model's contract rather than reinterpreting the source scores.
    contract=load_module("legacy_snapshot_contract",source.parents[1]/"src/donghaeng_model/snapshot.py")
    service=contract.load_research_snapshot(snapshot)
    raw_snapshot=load(snapshot)
    require(payload==raw_snapshot.get("paper_behavior_index"),"service_snapshot_reference_mismatch")
    require(len(service.paper_behavior_index.records)==len(records),"service_reference_count_mismatch")
    mapping=list(csv.DictReader((source/"code_mapping.csv").open(encoding="utf-8-sig",newline="")))
    scores=list(csv.DictReader((source/"index_scores.csv").open(encoding="utf-8-sig",newline="")))
    mapped=[r for r in mapping if r["mapping_status"]=="matched_code_and_labels"]
    held=[r for r in mapping if r["mapping_status"]=="held"]
    require(len(mapping)==len(mapped)+len(held),"unknown_mapping_status")
    by_code=unique(mapped,"dong_code");by_source=unique(mapping,"source_dong_code")
    score_index=unique(scores,"source_dong_code");unique(records,"dong_code")
    require(set(score_index)==set(by_source),"calculated_mapping_keys_mismatch")
    require({r["dong_code"] for r in records}==set(by_code),"published_mapping_keys_mismatch")
    require(payload["mapping_counts"]==dict(calculated=len(scores),mapped=len(mapped),held=len(held)),"mapping_count_mismatch")
    require(len(records)==417 and len(scores)==424 and len(held)==7,"legacy_reference_scope_changed")
    for row in records:
        code=row["dong_code"];mapped_row=by_code[code];score=score_index[row["source_dong_code"]]
        require(re.fullmatch(r"11[0-9]{8}",code) is not None,"invalid_Seoul_dong_code")
        require(row["period"]==payload["period"]=="2025-12" and row["age_group"]=="20~39세","reference_population_or_period_changed")
        for field in ("source_dong_code","district","dong_name"):
            require(row[field]==mapped_row[field],"mapping_label_mismatch:"+field)
        require(score["dong_code"]==code,"score_mapping_mismatch")
        require(row["score"]==float(score["score"]),"score_changed")
        require(math.isfinite(row["score"]) and 0<=row["score"]<=100,"invalid_reference_score")
        for field in DIMENSIONS:
            require(row["dimensions"][field]==float(score[field]),"domain_score_changed:"+field)
        require(bool(row["notes"]) and bool(row["quality"]),"missing_source_quality")
    require(all(not row["dong_code"] for row in held),"held_mapping_published")
    return payload,held,dict(status="passed",published_records=len(records),calculated_records=len(scores),held_records=len(held),
        full_payload_matches_service_snapshot=True,score_and_four_domains_exact=True,spatial_mapping_exact=True,
        original_snapshot_schema_validated=True,source_record_fields_preserved=True,
        service_snapshot_sha256=sha(snapshot),
        service_snapshot_git_blob=hashlib.sha1(b"blob "+str(snapshot.stat().st_size).encode()+b"\0"+snapshot.read_bytes()).hexdigest(),
        live_API_or_agent_execution_verified=False,
        quality_status="hold",operational_alert_allowed=False)


def build(source,snapshot,output_root=None,target=None):
    source=Path(source);snapshot=Path(snapshot)
    payload,held,validation=check_source(source,snapshot)
    inputs={name:sha(source/name) for name in SOURCE_FILES}
    inputs["service_snapshot.json"]=sha(snapshot)
    run_id=datetime.now(timezone.utc).strftime("legacy_seoul_reference_%Y%m%d_%H%M%S_%f")
    out=Path(target) if target else Path(output_root or FINAL/"outputs")/run_id
    if out.exists():
        manifest=verify(out)
        require(manifest["inputs"]==inputs,"existing_reference_inputs_changed")
        return out
    out.mkdir(parents=True,exist_ok=False)
    for name in SOURCE_FILES:shutil.copy2(source/name,out/name)
    shutil.copy2(snapshot,out/"service_snapshot.json")
    write_jsonl(out/"agent_evidence.jsonl",payload["records"])
    dump(out/"held_mapping_records.json",held)
    dump(out/"validation_report.json",validation)
    dump(out/"manifest.json",dict(contract_version=CONTRACT,run_id=run_id,status="completed",is_synthetic=False,
        quality_status="hold",operational_alert_allowed=False,reference_role="separate_historical_background",
        inputs=inputs,validation=validation,source_directory=str(source.resolve()),code_sha256=sha(Path(__file__)),
        outputs={p.name:sha(p) for p in out.iterdir() if p.is_file()}))
    verify(out)
    return out


def verify(bundle):
    bundle=Path(bundle).resolve();manifest=load(bundle/"manifest.json")
    require(manifest.get("contract_version")==CONTRACT and manifest.get("status")=="completed"
        and manifest.get("is_synthetic") is False,"invalid_legacy_reference_bundle")
    require(manifest.get("quality_status")=="hold" and manifest.get("operational_alert_allowed") is False,"legacy_reference_quality_changed")
    require(set(manifest["outputs"])==set(SOURCE_FILES)|{"agent_evidence.jsonl","service_snapshot.json","held_mapping_records.json","validation_report.json"},"incomplete_legacy_reference_manifest")
    for name,digest in manifest["outputs"].items():
        path=(bundle/name).resolve()
        require(path.is_relative_to(bundle) and sha(path)==digest,"legacy_reference_hash_mismatch:"+name)
    return manifest


def query(bundle,region_code=None,dong_code=None,period=None,age_group=None,offset=0,limit=100):
    if region_code is not None:validate_region_code(region_code)
    require(dong_code is None or re.fullmatch(r"[0-9]{10}",dong_code) is not None,"invalid_dong_code")
    require(period is None or re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])",period) is not None,"invalid_period")
    require(age_group is None or age_group=="20~39세","unsupported_original_age_group")
    require(offset>=0 and 1<=limit<=500,"invalid_pagination")
    bundle=Path(bundle);manifest=verify(bundle)
    payload=load(bundle/"service_payload.json")
    rows=[row for row in read_jsonl(bundle/"agent_evidence.jsonl")
        if (region_code is None or row["dong_code"][:5]==region_code)
        and (dong_code is None or row["dong_code"]==dong_code)
        and (period is None or row["period"]==period)
        and (age_group is None or row["age_group"]==age_group)]
    return dict(contract_version=CONTRACT,run_id=manifest["run_id"],reference_role="separate_historical_background",
        quality_status="hold",operational_alert_allowed=False,main_model_join=False,
        availability=availability(rows,region_code,period),total_matched=len(rows),offset=offset,
        next_offset=offset+limit if offset+limit<len(rows) else None,records=rows[offset:offset+limit],
        reference_metadata={key:value for key,value in payload.items() if key!="records"},
        service_snapshot_comparison=manifest["validation"])


def main():
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest="command",required=True)
    builder=commands.add_parser("build");builder.add_argument("--source",required=True);builder.add_argument("--snapshot",required=True);builder.add_argument("--output-root")
    reader=commands.add_parser("query");reader.add_argument("--bundle",required=True)
    for key in ("region-code","dong-code","period","age-group"):reader.add_argument("--"+key)
    reader.add_argument("--offset",type=int,default=0);reader.add_argument("--limit",type=int,default=100)
    args=vars(parser.parse_args());command=args.pop("command")
    print(json.dumps({"bundle":str(build(**args))} if command=="build" else query(**args),ensure_ascii=False,allow_nan=False))


if __name__=="__main__":main()
