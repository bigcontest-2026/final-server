"""Separate external context; never influences main activity candidates."""
import csv,json,hashlib,shutil
from pathlib import Path
from zipfile import ZipFile
from .common import dump,load_module,sha

def build_context(out,cfg):
    out=Path(out);base=Path(cfg["data_root"]).parent
    target=out/"external_context";target.mkdir(exist_ok=True)
    inventory=[]
    c1root=base/"C1_commercial_baseline"
    source=Path(cfg["data_root"])/"서울시 상권분석서비스(추정매출-자치구).json"
    data=json.loads(source.read_text(encoding="utf-8-sig"))
    labels=data["DESCRIPTION"]
    if len(set(labels.values()))!=len(labels): raise ValueError("duplicate_JSON_column_description")
    normalized=target/"c1_input.normalized.csv"
    # Use provider's explicit DESCRIPTION mapping, not invented metric aliases.
    with normalized.open("w",encoding="utf-8",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=list(labels.values()));writer.writeheader()
        for row in data["DATA"]:
            if set(k.upper() for k in row)-set(labels): raise ValueError("unknown_JSON_column")
            writer.writerow({labels[k.upper()]:v for k,v in row.items()})
    c1=load_module("final_c1",c1root/"c1_baseline.py")
    config=json.loads((c1root/"config_c1.json").read_text(encoding="utf-8"))
    # The integration delivers all Seoul districts; preserve upstream research settings.
    config["focus_district_codes"]=sorted(c1.DISTRICTS)
    c1.validate_config(config)
    frame,profile=c1.load_source(normalized,config)
    results,coverage=c1.analyze(frame,config)
    config_hash=hashlib.sha256(json.dumps(config,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    records=c1.combine_focus(results,profile["sha256"],config_hash,False)
    details=target/"c1";details.mkdir(exist_ok=True)
    c1.write_outputs(details,results,records,coverage,config)
    dump(details/"config_used.json",config)
    dump(details/"manifest.json",{"status":"completed","is_demo":False,"original_json_sha256":sha(source),"normalized_csv_sha256":sha(normalized),
        "normalization":"provider_DESCRIPTION_exact_column_mapping","code_sha256":sha(c1root/"c1_baseline.py"),"config_sha256":sha(c1root/"config_c1.json"),
        "effective_config_sha256":config_hash,
        "region_codes":sorted({r["district_code"] for r in records}),
        "input_rows":profile["input_rows"],"agent_records":len(records),"main_model_join":False})
    normalized.unlink()
    inventory.append({"experiment":"C1","status":"reexecuted_from_real_JSON","evidence":"external_context/c1/agent_evidence.jsonl",
                      "region_codes":sorted({r["district_code"] for r in records}),"time_granularity":"quarter","main_model_join":False,"limitations":["Seoul_only","no_monthly_replication"]})
    mroot=base/"Mobile_M1_M2_v3_code"
    run=mroot/"outputs/v3_20261014_101514_927324"
    status=json.loads((run/"run_status.json").read_text(encoding="utf-8"))
    if status["status"]!="completed": raise ValueError("M12_run_incomplete")
    for name in ("m1","m2"):
        audit=json.loads((run/name/"audit.json").read_text(encoding="utf-8"))
        mobile=Path(cfg["data_root"])/"모바일 인구이동 데이터셋.zip"
        with ZipFile(mobile) as archive:
            source_hashes={hashlib.sha256(archive.read(n)).hexdigest() for n in archive.namelist() if n.endswith(".xlsx")}
        if not {item[1] for item in audit["source_identity"]}<=source_hashes:
            raise ValueError("M12_source_identity_mismatch")
        checked=0
        for filename,digest in audit["artifact_hashes"].items():
            path=run/name/filename
            if not path.exists(): raise ValueError("M12_artifact_missing:"+filename)
            if sha(path)!=digest: raise ValueError("M12_artifact_hash_mismatch:"+filename)
            checked+=1
        folder=target/name;folder.mkdir(exist_ok=True)
        for filename in ("agent_evidence.jsonl","audit.json","config_used.json","11_past_only_scores.csv"):
            shutil.copy2(run/name/filename,folder/filename)
        inventory.append({"experiment":name.upper(),"status":"preserved_completed_run_hash_verified","artifact_hashes_verified":checked,
            "evidence":"external_context/"+name+"/agent_evidence.jsonl","source_identity":audit["source_identity"],
            "main_model_join":False,"reexecuted":False,"limitations":["week_dates_unverified_no_month_join","overlapping_group_margins"]})
    m3root=base/"M3_SK_v2_code/outputs/m3_20261013_201106_662229"
    manifest=json.loads((m3root/"input_manifest.json").read_text(encoding="utf-8"))
    raw=Path(cfg["data_root"])/"2026빅콘테스트_sk통신인구_데이터_레이아웃.zip"
    matches=0
    with ZipFile(raw) as archive:
        for item in manifest:
            member=item["source"].split("::")[-1]
            if hashlib.sha256(archive.read(member)).hexdigest()!=item["sha256"]: raise ValueError("M3_raw_hash_mismatch:"+member)
            matches+=1
    inventory.append({"experiment":"M3","status":"not_comparable","raw_files_hash_verified":matches,"raw_zip_sha256":sha(raw),
        "main_model_join":False,"reexecuted":False,"preserved_run_status":json.loads((m3root/"run_status.json").read_text(encoding="utf-8"))["status"],
        "reason":"official_BLOCK_to_region_mapping_unverified","full_results_sha256":sha(m3root/"full_results.zip")})
    dump(target/"index.json",inventory)
    return inventory
