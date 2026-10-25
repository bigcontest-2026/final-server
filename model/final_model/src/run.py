import argparse,json,hashlib,csv,shutil
from datetime import datetime,timezone
import jsonschema
from pathlib import Path
import pandas as pd
from .common import dump,sha,read_jsonl,verify_bundle,load_module
from .context import build_context
from .pipeline import FINAL,build_behavior
from .query import _query_verified
from .geography import region_registry, region_aliases

def _finish_checks(bundle,cfg,manifest):
    # Keep pipeline execution hashes before adding separate reference contexts.
    manifest.setdefault("pipeline_execution_code_sha256",manifest["final_code_sha256"])
    context=build_context(bundle,cfg)
    if cfg.get("legacy_reference_source"):
        from .legacy_seoul import build as build_legacy, verify as verify_legacy
        reference=build_legacy(cfg["legacy_reference_source"],cfg["legacy_service_snapshot"],target=bundle/"external_context/legacy_seoul")
        checked=verify_legacy(reference)
        context.append(dict(experiment="LEGACY_SEOUL",status="preserved_snapshot_exact_match",
            evidence="external_context/legacy_seoul/agent_evidence.jsonl",records=417,
            period="2025-12",age_group="20~39세",main_model_join=False,
            service_snapshot_sha256=checked["validation"]["service_snapshot_sha256"]))
        dump(bundle/"external_context/index.json",context)
    history=read_jsonl(bundle/"historical_reference.jsonl")
    validator=jsonschema.Draft202012Validator(json.loads((FINAL/"schemas/historical.schema.json").read_text(encoding="utf-8-sig")))
    for row in history:validator.validate(row)
    # Canonical documentation bytes must match frozen export hashes too.
    source=Path(cfg["index_root"])/"v3/exports/2025_final_v1"
    frozen=json.loads((source/"manifest.json").read_text(encoding="utf-8"))
    for name,meta in frozen["documentation"].items():
        raw=(source/name).read_bytes().replace(b"\r\n",b"\n")
        if hashlib.sha256(raw).hexdigest()!=meta["sha256"]:raise ValueError("survey_doc_hash_mismatch")
        (bundle/"survey_mois_reference"/name).write_bytes(raw)
    from .pipeline import historical_key
    regions=manifest.get("regions",region_registry(cfg))
    aliases=manifest.get("region_aliases",cfg.get("region_aliases",{}))
    groups={row["group_id"] for row in history if historical_key(row,regions,aliases)}
    sensitivity=read_jsonl(bundle/"c3_details/threshold_sensitivity.jsonl")
    threshold_counts={}
    for threshold in (5.0,10.0,20.0):
        rows=[r for r in sensitivity if r["group_id"] in groups and r["calendar_type"]=="all" and r["metric"]=="count" and r["decline_threshold_pct"]==threshold]
        threshold_counts[str(threshold)]={"rows":len(rows),"candidates":sum(r["review_flag"] is True for r in rows),"withheld":sum(r["review_flag"] is None for r in rows)}
    assert threshold_counts["5.0"]["candidates"]>=threshold_counts["10.0"]["candidates"]>=threshold_counts["20.0"]["candidates"]
    dump(bundle/"threshold_sensitivity_summary.json",threshold_counts)
    original=read_jsonl(bundle/"behavior_evidence.jsonl")
    panel={tuple(r["key"][k] for k in ("month","region_code","sex_code","age_code")):{k:r[k] for k in ("skt","card_a","card_count")} for r in read_jsonl(bundle/"monthly_observations.jsonl")}
    root=Path(cfg["index_root"])
    base=load_module("recheck_base",root/"v2/src/validate_baselines.py")
    scorer=load_module("recheck_scorer",root/"v3/behavior/src/compare_step7_anomalies.py")
    robust=json.loads((root/"v3/behavior/configs/step7_3_experiment_v1.json").read_text(encoding="utf-8"))
    coverage={(r["key"]["month"],r["key"]["region_code"]):r["coverage"]["skt_observed_blocks"] for r in original}
    replay=build_behavior(panel,base,scorer,robust,original[0]["provenance"],coverage,regions)
    if len(original)!=len(replay):raise ValueError("behavior_recalculation_row_count_mismatch")
    for expected,actual in zip(original,replay):
        actual["historical_reference_id"]=expected["historical_reference_id"]
        if expected!=actual:raise ValueError("behavior_recalculation_mismatch")
    c3=load_module("recheck_c3",Path(cfg["c3_root"])/"c3_baseline.py")
    from .pipeline import REGIONS
    c3.REGIONS={name: REGIONS[code] if code in REGIONS else code for name,code in region_aliases(regions,aliases).items()}
    c3cfg=json.loads((bundle/"c3_details/config_used.json").read_text(encoding="utf-8"))
    daily=pd.read_csv(bundle/"c3_details/daily_results.csv",parse_dates=["date","first_observed_date"],dtype={"age":str})
    daily["withheld_reason"]=daily["withheld_reason"].fillna("")
    _,monthly,_=c3.monthly_summaries(daily,c3cfg)
    stored=read_jsonl(bundle/"c3_details/monthly_results.jsonl")
    fields=("count_comparable_sum","count_baseline_sum","count_change_pct","count_decline_flag","count_comparable_days","count_coverage")
    idx=lambda r:(r["group_id"],r["month"],r["calendar_type"])
    recalculated={idx(r):r for r in monthly}
    if len(recalculated)!=len(stored):raise ValueError("C3_recalculation_row_count_mismatch")
    for r in stored:
        for field in fields:
            if r[field]!=recalculated[idx(r)][field]:raise ValueError("C3_monthly_recalculation_mismatch:"+field)
    report=json.loads((bundle/"validation_report.json").read_text(encoding="utf-8"))
    report.update(validation_run_id=manifest["run_id"],behavior_recalculation_rows=len(replay),C3_monthly_recalculation_rows=len(stored),
                  external_context=context,historical_schema_records=len(history),survey_frozen_documentation_verified=2,C3_common_24_threshold_sensitivity=threshold_counts,core_reproducibility="same_saved_observations_exact_output",
                  full_raw_input_repeat=False)
    dump(bundle/"validation_report.json",report)
    manifest["validation"]=report
    manifest["source_commits"]["local_index2_checkout"]="89687761a242a4aa5072df0730c4aca535c07e5d"
    manifest["source_commits"]["index2_main_verified"]="19b0ec61e88b0d3dc513a935c49dc2de50b7b3fd"
    manifest["source_commits_note"]="Main snapshots are inventory; standalone experiment execution is identified by actual code hashes."
    return report


def finish(bundle,cfg,*,publish_latest=True):
    """Only publish a real bundle after every finalization check succeeds."""
    from .pipeline import stage
    bundle=Path(bundle)
    manifest=verify_bundle(bundle,expected_status="core_ready")
    manifest["status"]="validating"
    dump(bundle/"manifest.json",manifest)
    try:
        report=_finish_checks(bundle,cfg,manifest)
        sample=_query_verified(bundle,manifest,region_code="11680",month="202510",sex_code="MAN",age_code="20G")
        dump(bundle/"agent_query_example.json",sample)
        with (bundle/"survey_mois_reference/national_six_group_profile.csv").open(encoding="utf-8-sig",newline="") as stream:
            if len(list(csv.DictReader(stream)))!=6:raise ValueError("survey_six_groups_required")
        manifest["outputs"]={p.relative_to(bundle).as_posix():sha(p) for p in sorted(bundle.rglob("*")) if p.is_file() and p!=bundle/"manifest.json"}
        manifest["handoff_code_sha256"]={p.relative_to(FINAL).as_posix():sha(p) for folder in ("src","schemas") for p in sorted((FINAL/folder).rglob("*")) if p.is_file() and "__pycache__" not in str(p)}
        manifest["external_context_code_sha256"]={str(Path(cfg["data_root"]).parent/"C1_commercial_baseline/c1_baseline.py"):sha(Path(cfg["data_root"]).parent/"C1_commercial_baseline/c1_baseline.py")}
        stage(7,f"후처리 검증 통과: {manifest['run_id']}. hold 유지. 공동 확정·실제 서비스 연동은 대기.")
        manifest["status"]="completed"
        dump(bundle/"manifest.json",manifest)
        verify_bundle(bundle)
        if publish_latest:
            dump(FINAL/"outputs/latest.json",{"run_id":manifest["run_id"]})
        return report
    except Exception as error:
        manifest["status"]="failed"
        manifest["finalization_error"]=type(error).__name__+":"+str(error)
        dump(bundle/"manifest.json",manifest)
        raise


def prepare_revalidation(source):
    """Finish a pending core or copy a completed snapshot before revalidation."""
    source=Path(source).resolve()
    state=json.loads((source/"manifest.json").read_text(encoding="utf-8"))["status"]
    if state=="core_ready":
        verify_bundle(source,expected_status="core_ready")
        return source
    manifest=verify_bundle(source)
    run_id=datetime.now(timezone.utc).strftime("revalidated_%Y%m%d_%H%M%S_%f")
    target=FINAL/"outputs"/run_id
    shutil.copytree(source,target,ignore=lambda directory,names: ["manifest.json"] if Path(directory)==source else [])
    manifest["revalidation_source"]={"run_id":manifest["run_id"],"manifest_sha256":sha(source/"manifest.json")}
    manifest.update(run_id=run_id,status="core_ready",validation_scope="saved_observations_and_preserved_context_not_full_raw_repeat")
    dump(target/"manifest.json",manifest)
    return target


def main():
    p=argparse.ArgumentParser();p.add_argument("--bundle");p.add_argument("--no-publish",action="store_true");p.add_argument("--config",default=str(FINAL/"configs/local.json"));args=p.parse_args()
    cfg=json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    if args.bundle is None:
        from .pipeline import main as pipeline
        import sys
        sys.argv=[sys.argv[0],"--config",args.config]
        bundle=pipeline()
    else:bundle=prepare_revalidation(args.bundle)
    report=finish(bundle,cfg,publish_latest=not args.no_publish)
    print(json.dumps(report,ensure_ascii=False))
if __name__=="__main__":main()
