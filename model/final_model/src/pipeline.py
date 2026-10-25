"""Actual-data V3/C3 integration. Upstream models and inputs are read-only."""
import argparse, calendar, csv, io, json, math, platform, shutil, statistics, subprocess, sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile
import jsonschema
from .common import dump, load_module, sha, write_jsonl
from .geography import region_registry, region_aliases, resolve_region

FINAL = Path(__file__).resolve().parents[1]
MONTHS = tuple(f"2025{m:02d}" for m in range(7,13))
REGIONS = {"11680":"강남구","51110":"춘천시"}
SEXES = ("MAN","WMAN")
AGES = ("20G","30G","40G","50G","60GU")
HOLD = ["SKT_ORIGINAL_BOUNDARY_CRS_UNVERIFIED","SKT_MONTHLY_COVERAGE_UNVERIFIED","SKT_PRODUCTION_RULES_UNVERIFIED","CARD_MISSINGNESS_RULE_UNVERIFIED","CARD_INDUSTRY_SUBTYPE_UNVERIFIED"]
LIMITS = ["place_flow_not_resident_mobility","merchant_transactions_not_people","no_isolation_ground_truth_or_probability","six_month_window_no_annual_seasonality","same_source_evidence_not_independent","source_release_date_backtest_unverified"]

def stage(n, message):
    print(f"Step {n}: {message}", flush=True)
    with (FINAL/"README.md").open("a",encoding="utf-8") as f:
        f.write(f"\n- Step {n} [{datetime.now(timezone.utc).isoformat()}]: {message}\n")

def prev(month):
    return f"{int(month[:4]):04d}{int(month[4:])-1:02d}"

def historical_key(row, regions=None, aliases=None):
    regions = regions or REGIONS
    if row["dataset"]!="data2" or row["view"]!="age_sex" or row["calendar_type"]!="all":
        return None
    return row["month"].replace("-",""), resolve_region(row["region"],regions,aliases), {"남성":"MAN","여성":"WMAN"}[row["sex"]], row["age"]+"G"

def aggregate_card(path, regions=None, aliases=None):
    regions = regions or REGIONS
    sums, bands, daily = defaultdict(int), defaultdict(set), defaultdict(int)
    rows = 0
    with ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.endswith("데이터2.txt")]
        if len(names)!=1: raise ValueError("exactly_one_data2_required")
        with archive.open(names[0]) as binary, io.TextIOWrapper(binary,encoding="cp949",newline="") as stream:
            reader = csv.DictReader(stream,delimiter="\t")
            if not {"TA_YMD","MCT_SGG_CD","SEX_CCD","AGE_CCD","USE_CNT"} <= set(reader.fieldnames):
                raise ValueError("card_columns_missing")
            for row in reader:
                rows+=1
                dt = datetime.strptime(row["TA_YMD"],"%Y%m%d")
                month = dt.strftime("%Y%m")
                if month not in MONTHS: continue
                region = resolve_region(row["MCT_SGG_CD"], regions, aliases)
                sex = {"남성":"MAN","여성":"WMAN"}[row["SEX_CCD"]]
                band = row["AGE_CCD"].replace(" ","")
                if band not in {f"{i}대" for i in range(10,100,10)}: raise ValueError("unknown_age")
                count = int(row["USE_CNT"])
                if count<0: raise ValueError("negative_count")
                if band=="10대": continue
                age = band[:2]+"G" if int(band[:2])<60 else "60GU"
                key = month,region,sex,age
                sums[key]+=count
                bands[key].add(band)
                daily[key+(row["TA_YMD"],)]+=count
    counts={}
    for m in MONTHS:
        for r in regions:
            for s in SEXES:
                for a in AGES:
                    key=m,r,s,a
                    required={a[:2]+"대"} if a!="60GU" else {"60대","70대","80대","90대"}
                    counts[key]=sums[key] if required<=bands[key] else None
    return counts,daily,{"rows":rows,"member":names[0]}

def source_robust_stats(index, scorer, config):
    """Reuse upstream for complete pairs; retain valid source groups independently."""
    minimum = config["robust"]["min_groups"]
    complete = {k:e for k,e in index.items() if all(v["T"] is not None for v in e["sources"].values())}
    sizes = Counter(k[:2] for k in complete)
    complete = {k:e for k,e in complete.items() if sizes[k[:2]] == minimum}
    stats = scorer.robust_group_stats(complete, config)
    groups = defaultdict(list)
    for key, entry in index.items():
        for source, value in entry["sources"].items():
            if value["T"] is not None:
                groups[(key[:2],source)].append(float(value["T"]))
    for key, values in groups.items():
        if key in stats or len(values) != minimum:
            continue
        # Same median/MAD formula as upstream, without requiring the other source.
        center = statistics.median(values)
        mad = statistics.median(abs(value-center) for value in values)
        stats[key] = {"center":center,"mad":mad,"status":"computed" if mad > 0 else "unavailable_zero_mad"}
    return stats


def compare_c3_cell(record, history, comparable_dates, raw_daily):
    """Audit matching raw dates while preserving unavailable baseline/decision values."""
    key = tuple(record["key"][k] for k in ("month","region_code","sex_code","age_code"))
    dates = [date.strftime("%Y%m%d") for date in comparable_dates]
    expected = history["count"]["comparable_sum"]
    if expected is None:
        if dates:
            raise ValueError("C3_null_sum_with_comparable_dates")
        validation = "unavailable_no_comparable_dates"
    else:
        if not dates:
            raise ValueError("C3_sum_without_comparable_dates")
        if len(dates) != len(set(dates)):
            raise ValueError("C3_duplicate_comparable_date")
        if any(key+(date,) not in raw_daily for date in dates):
            raise ValueError("C3_comparable_raw_date_missing")
        observed = sum(raw_daily[key+(date,)] for date in dates)
        if not math.isclose(observed,expected,rel_tol=0,abs_tol=1e-8):
            raise ValueError("C3_comparable_sum_mismatch")
        validation = "verified"
    card = record["sources"]["card_a"]
    decline = None if card["Q"] is None else card["Q"] > 0
    c3_decline = history["count"]["decline_flag"]
    return {"key":record["key"],"v3_card_decline":decline,"c3_count_decline":c3_decline,
            "v3_change_pct":card["change_percent"],"c3_change_pct":history["count"]["change_pct"],
            "comparison_role":"different_baselines_same_source",
            "comparison_status":"computed" if decline is not None and c3_decline is not None else "unavailable",
            "comparable_sum_validation":validation,"c3_withheld_reasons":history["count"].get("withheld_reasons",[])}


def build_behavior(panel, baseline, scorer, robust_config, provenance, coverage, regions=None):
    regions = regions or REGIONS
    index={}
    for key,current in sorted(panel.items()):
        m,r,s,a=key
        if m==MONTHS[0]: continue
        old=panel.get((prev(m),r,s,a),{})
        sources={}
        for name in ("skt","card_a"):
            now,prior=current[name],old.get(name)
            q,decline,reason=baseline.previous_change(None if now is None else str(now),None if prior is None else str(prior))
            sources[name]={"activity_daily":now,"previous_activity_daily":prior,"change_percent":None if decline is None else -decline,"Q":q,"H":None,"T":None,
                           "direction":None if q is None else "decrease" if q>0 else "increase" if q<0 else "flat",
                           "calculation_status":"unavailable" if reason else "computed","reason":reason or None,
                           "unit":"daily_place_flow_sum" if name=="skt" else "payment_count_per_day"}
        index[key]={"sources":sources}
    for m in MONTHS[1:]:
        for r in regions:
            group=[entry for key,entry in index.items() if key[:2]==(m,r)]
            for name in ("skt","card_a"):
                valid=[e["sources"][name]["Q"] for e in group if e["sources"][name]["Q"] is not None]
                h=statistics.median(valid) if len(valid)==10 else None
                for e in group:
                    value=e["sources"][name]
                    value["H"]=h
                    value["T"]=value["Q"]-h if h is not None and value["Q"] is not None else None
    stats=source_robust_stats(index,scorer,robust_config)
    records=[]
    for key,entry in sorted(index.items()):
        m,r,s,a=key
        for name,value in entry["sources"].items():
            summary=stats.get(((m,r),name))
            mad=summary["mad"] if summary else None
            z=(value["T"]-summary["center"])/(robust_config["robust"]["mad_scale"]*mad) if mad else None
            value["robust"]={"z":z,"mad":mad,"status":summary["status"] if summary else "unavailable_insufficient_groups",
                             "candidate":value["Q"]>0 and z>=3.5 if z is not None else None}
        skt,card=entry["sources"]["skt"],entry["sources"]["card_a"]
        available=skt["Q"] is not None and card["Q"] is not None
        rule=skt["Q"]>0 and card["Q"]>0 if available else None
        old=index.get((prev(m),r,s,a))
        streak=bool(rule and all(x["Q"]>0 for x in old["sources"].values())) if old and rule is not None and all(x["Q"] is not None for x in old["sources"].values()) else None
        flags=[v["robust"]["candidate"] for v in entry["sources"].values()]
        robust=any(v is True for v in flags) if any(v is not None for v in flags) else None
        reasons=HOLD+(["SKT_SEP_OCT_BOUNDARY_VERSION_CHANGED"] if m=="202510" else [])
        records.append({"contract_version":"final_activity_evidence_v1","record_id":"behavior:"+":".join(key),
            "key":dict(zip(("month","region_code","sex_code","age_code"),key)),"region_name":regions[r],
            "spatial_unit":"sgg","geography_basis":{"skt":"observed_place","card_a":"merchant_location"},
            "population_basis":"source_aggregate_sex_age_not_identified_residents","sources":entry["sources"],
            "observation":{"card_count":panel[key]["card_count"],"card_unit":"transactions"},
            "rule":{"candidate":rule,"two_month_streak":streak},
            "robust":{"candidate":robust,"status":"computed" if all(v is not None for v in flags) else "partial" if robust is not None else "unavailable","cutoff":3.5},
            "comparison_status":"computed" if available else "unavailable",
            "coverage":{"skt_observed_blocks":coverage.get((m,r)),"skt_coverage_ratio":None,"reason":"expected_block_universe_unverified"},
            "historical_reference_id":None,"spatial_context":{"status":"not_comparable","reason":"M3_official_mapping_unverified"},
            "calendar_context":{"skt_daily_adjustment":None,"card_previous_month_basis":"calendar_days"},
            "reason_codes":["BOTH_SOURCES_DECREASE" if rule else "JOINT_DECLINE_NOT_MET" if rule is False else "COMPARISON_UNAVAILABLE"],
            "explanation":f"{m} {regions[r]} {s} {a}: 전월 관측 변화. 고립 여부·원인 미확정.",
            "possible_explanations":["seasonality","calendar_composition","observation_footprint_change"],"unverified_factors":reasons,
            "quality":{"status":"hold","reasons":reasons},"operational_alert_allowed":False,"provenance":provenance,"limitations":LIMITS})
    return records

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--config",default=str(FINAL/"configs/local.json"))
    args=parser.parse_args()
    cfg_path=Path(args.config)
    cfg=json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    regions=region_registry(cfg)
    aliases=cfg.get("region_aliases",{})
    root=Path(cfg["index_root"])
    data=Path(cfg["data_root"])
    c3root=Path(cfg["c3_root"])
    run_id=datetime.now(timezone.utc).strftime("final_%Y%m%d_%H%M%S_%f")
    out=FINAL/"outputs"/run_id
    out.mkdir(parents=True,exist_ok=False)
    dump(out/"manifest.json",{"status":"running","run_id":run_id,"is_synthetic":False})
    try:
        merge=data/cfg["skt_merge"]
        card=data/cfg["card_zip"]
        sys.path.insert(0,str(root/"v1/src"))
        skmod=load_module("final_skt",root/"v1/src/calculate_regional_signals.py")
        baseline=load_module("final_baseline",root/"v2/src/validate_baselines.py")
        scorer=load_module("final_scorer",root/"v3/behavior/src/compare_step7_anomalies.py")
        skmod.SKT=merge
        skmod.legacy_relative=lambda p:Path(p).name
        signal_cfg=json.loads((root/"v1/configs/regional_signals.json").read_text(encoding="utf-8"))
        robust_cfg=json.loads((root/"v3/behavior/configs/step7_3_experiment_v1.json").read_text(encoding="utf-8"))
        signal_cfg["regions"]=regions
        skt,skt_profile=skmod.read_skt(signal_cfg)
        counts,raw_daily,card_profile=aggregate_card(card,regions,aliases)
        stage(4,"V1 SKT 선택·집계, V2 전월 산식, V3 강건 통계 재사용. 원자료 집계 완료.")
        c3=load_module("c3_baseline",c3root/"c3_baseline.py")
        c3.REGIONS={name: REGIONS[code] if code in REGIONS else code for name,code in region_aliases(regions,aliases).items()}
        c3cfg=json.loads((c3root/"config_c3.json").read_text(encoding="utf-8"))
        c3.validate_config(c3cfg)
        holidays,holiday_meta=c3.load_calendar(c3root,c3cfg)
        daily,profile=c3.aggregate_sources(c3.discover_sources(card,"data2"),"data2",c3cfg)
        result,refs=c3.make_daily_results(daily,c3cfg,holidays)
        result,models=c3.fit_isolation_forest(result,c3cfg)
        observed,monthly,sensitivity=c3.monthly_summaries(result,c3cfg)
        matched=0
        for row in observed:
            if row["view"]!="age_sex": continue
            key=row["month"].replace("-",""),resolve_region(row["region"],regions,aliases),{"남성":"MAN","여성":"WMAN"}[row["sex"]],row["age"]+"G"
            if counts[key]!=row["count_reported_sum"]: raise ValueError("C3_month_count_mismatch")
            matched+=1
        if matched != len({(r["month"],r["region"],r["sex"],r["age"]) for r in observed if r["view"]=="age_sex"}):
            raise ValueError("C3_monthly_cells_mismatch")
        details=out/"c3_details"
        details.mkdir()
        result.to_csv(details/"daily_results.csv",index=False)
        refs.to_csv(details/"reference_points.csv",index=False)
        for name,obj in [("model_metadata",models),("data_profile",profile),("config_used",c3cfg),("calendar_metadata",holiday_meta)]:
            dump(details/(name+".json"),obj)
        write_jsonl(details/"monthly_results.jsonl",monthly)
        write_jsonl(details/"threshold_sensitivity.jsonl",sensitivity)
        history=c3.make_agent_records(monthly,"data2",profile,c3cfg,False,run_id)
        write_jsonl(out/"historical_reference.jsonl",history)
        stage(5,f"C3 실제 data2 재실행 완료. 원 월건수 {matched}셀 독립 대사 통과. 실행 {run_id}")
        coverage=Counter()
        dong=defaultdict(Decimal)
        with merge.open(encoding="utf-8-sig",newline="") as f:
            for row in csv.DictReader(f):
                if not skmod.selected_skt(row,set(MONTHS),set(regions)): continue
                m,r,d=row["STD_YM"],row["sgg_code"],row["administrative_dong_code"]
                coverage[m,r]+=1
                for s in SEXES:
                    for a in AGES: dong[m,r,d,s,a]+=Decimal(row[f"{s}_FLOW_POP_CNT_{a}"])
        panel={}
        for key,count in counts.items():
            m,r,s,a=key
            panel[key]={"skt":float(skt[r,m,s,a]) if (r,m,s,a) in skt else None,
                        "card_a":count/calendar.monthrange(int(m[:4]),int(m[4:]))[1] if count is not None else None,"card_count":count}
        module_paths=[root/"v1/src/calculate_regional_signals.py",root/"v1/src/project_paths.py",root/"v2/src/validate_baselines.py",root/"v3/behavior/src/compare_step7_anomalies.py",c3root/"c3_baseline.py",c3root/"config_c3.json",c3root/"calendar_2025_H2.csv"]
        inputs={str(p):sha(p) for p in (merge,card)}
        provenance={"model_version":"final_activity_evidence_v1","run_id":run_id,"config_sha256":sha(cfg_path),"inputs":inputs,
                    "source_code_sha256":{str(p):sha(p) for p in module_paths},"analysis_period":["202507","202512"],"source_release_dates_verified":False}
        records=build_behavior(panel,baseline,scorer,robust_cfg,provenance,coverage,regions)
        hindex={}
        for row in history:
            key=historical_key(row,regions,aliases)
            if key:
                if key in hindex: raise ValueError("duplicate_historical_key")
                hindex[key]=row
        joins=[]
        for record in records:
            key=tuple(record["key"][k] for k in ("month","region_code","sex_code","age_code"))
            h=hindex.get(key)
            if h is None: continue
            record["historical_reference_id"]=h["record_id"]
            dates=result[(result.group_id==h["group_id"]) & (result.date.dt.strftime("%Y-%m")==h["month"]) & result.count_change_pct.notna()]
            joins.append(compare_c3_cell(record,h,dates.date,raw_daily))
        write_jsonl(out/"behavior_evidence.jsonl",records)
        write_jsonl(out/"monthly_observations.jsonl",[{"key":dict(zip(("month","region_code","sex_code","age_code"),k)),**v,"quality_status":"hold"} for k,v in sorted(panel.items())])
        write_jsonl(out/"dong_skt_evidence.jsonl",[{"key":{"month":m,"region_code":r,"dong_code":d,"sex_code":s,"age_code":a},
                   "activity_daily":float(value),"previous_activity_daily":float(dong[prev(m),r,d,s,a]) if (prev(m),r,d,s,a) in dong else None,
                   "spatial_unit":"administrative_dong","geography_basis":"observed_place","card_observation":None,
                   "quality_status":"hold","operational_alert_allowed":False,"mapping_status":"provided_merge_not_independently_verified"} for (m,r,d,s,a),value in sorted(dong.items())])
        source=root/"v3/exports/2025_final_v1"
        dest=out/"survey_mois_reference"
        dest.mkdir()
        sm=json.loads((source/"manifest.json").read_text(encoding="utf-8"))
        for name,meta in sm["files"].items():
            raw=(source/name).read_bytes()
            canonical=raw.replace(b"\r\n",b"\n")
            import hashlib
            if hashlib.sha256(canonical).hexdigest()!=meta["sha256"]:
                raise ValueError("survey_frozen_hash_mismatch:"+name)
            (dest/name).write_bytes(canonical)
        for name in ("manifest.json","README.md","DATA_DICTIONARY.md"): shutil.copy2(source/name,dest/name)
        validator=jsonschema.Draft202012Validator(json.loads((FINAL/"schemas/behavior.schema.json").read_text(encoding="utf-8-sig")))
        for row in records:
            validator.validate(row)
            for value in row["sources"].values():
                if value["Q"] is not None:
                    assert math.isclose(value["Q"],-math.log(value["activity_daily"]/value["previous_activity_daily"]),abs_tol=1e-12)
                    assert value["H"] is None or math.isclose(value["Q"],value["H"]+value["T"],abs_tol=1e-12)
        expected={(m,r,s,a) for m in MONTHS[1:] for r in regions for s in SEXES for a in AGES}
        actual={tuple(row["key"][k] for k in ("month","region_code","sex_code","age_code")) for row in records}
        if actual!=expected or len(records)!=len(expected): raise ValueError("panel_size_mismatch")
        expected_joins={key for key in expected if key in hindex}
        if len(joins)!=len(expected_joins): raise ValueError("historical_join_size_mismatch")
        dump(out/"comparison.json",joins)
        summaries=[]
        for m in MONTHS[1:]:
            for r in regions:
                group=[x for x in records if x["key"]["month"]==m and x["key"]["region_code"]==r]
                summaries.append({"month":m,"region_code":r,"groups":len(group),"joint_decline_candidates":sum(x["rule"]["candidate"] is True for x in group),"robust_candidates":sum(x["robust"]["candidate"] is True for x in group),"quality_status":"hold","operational_alert_allowed":False})
        dump(out/"region_summary.json",summaries)
        report={"status":"passed","behavior_rows":len(records),"monthly_rows":len(panel),"historical_rows":len(history),"historical_join_rows":len(joins),
                "raw_C3_month_count_matches":matched,"survey_frozen_outputs_verified":6,"survey_newline_policy":"LF_canonical_matches_frozen_SHA256","dong_rows":len(dong),
                "rule_candidates":sum(x["rule"]["candidate"] is True for x in records),"robust_candidates":sum(x["robust"]["candidate"] is True for x in records),
                "comparison":dict(Counter(f"{r['v3_card_decline']}/{r['c3_count_decline']}" for r in joins)),
                "spatial_mapping_verified":False,"original_team_outputs_reproduced":False,"source_release_dates_verified":False,
                "skt_profile":skt_profile,"card_profile":card_profile,"no_future_values_in_C3_reference":c3cfg["reference_end"]<c3cfg["evaluation_start"],
                "quality_status":"hold","operational_alert_allowed":False}
        dump(out/"validation_report.json",report)
        shutil.copy2(FINAL/"reports/DATA_DICTIONARY.md",out/"data_dictionary.md")
        outputs={p.relative_to(out).as_posix():sha(p) for p in sorted(out.rglob("*")) if p.is_file() and p!=out/"manifest.json"}
        dump(out/"manifest.json",{"status":"core_ready","is_synthetic":False,"run_id":run_id,"inputs":inputs,"provenance":provenance,"outputs":outputs,"regions":regions,"region_aliases":aliases,
             "validation":report,"source_commits":cfg["source_commits"],"python":platform.python_version(),
             "environment":subprocess.check_output([sys.executable,"-m","pip","freeze"],text=True).splitlines(),
             "final_code_sha256":{p.relative_to(FINAL).as_posix():sha(p) for directory in ("src","schemas","configs") for p in sorted((FINAL/directory).rglob("*")) if p.is_file() and "__pycache__" not in str(p)},
             "agent_service_live_integration":False})
        stage(6,f"스키마·Q/H/T 검증 통과. 행동 {len(records)}행, C3 연결 {len(joins)}행, 동 SKT {len(dong)}행 생성.")
        stage(7,f"실측 집계 {run_id} core_ready. 후처리 검증 전에는 조회·최신 번들 게시 불가.")
        print(json.dumps(report,ensure_ascii=False),flush=True)
        return out
    except Exception as error:
        dump(out/"manifest.json",{"status":"failed","run_id":run_id,"is_synthetic":False,"error":type(error).__name__+":"+str(error)})
        stage(5,f"실패 실행은 인계 불가: {type(error).__name__}: {error}")
        raise

if __name__=="__main__":
    main()
