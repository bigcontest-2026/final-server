"""Regression and negative cases for the preserved real Seoul reference."""
import json
import shutil
from pathlib import Path

import pytest

from final_model.src.common import sha
from final_model.src.legacy_seoul import build,check_source,query,verify
from final_model.src.common import dump
from final_model.src.query import external_query

ROOT=Path(__file__).resolve().parents[3]
SOURCE=ROOT/"model_mori/outputs/paper_isolation_index_20261003"
SNAPSHOT=Path(__file__).resolve().parents[1]/"outputs/service_snapshot_reference/isolation_snapshot.json"


@pytest.fixture
def reference(tmp_path):
    if not SOURCE.exists() or not SNAPSHOT.exists():pytest.skip("actual legacy source/snapshot not available")
    return build(SOURCE,SNAPSHOT,output_root=tmp_path)


def test_all_417_records_exact_roundtrip_and_held_seven_not_published(reference):
    original=json.loads((SOURCE/"service_payload.json").read_text(encoding="utf-8"))
    rows=[];offset=0
    while True:
        result=query(reference,offset=offset,limit=100);rows.extend(result["records"])
        assert result["quality_status"]=="hold" and result["operational_alert_allowed"] is False
        if result["next_offset"] is None:break
        offset=result["next_offset"]
    assert rows==original["records"] and len(rows)==417
    assert len(json.loads((reference/"held_mapping_records.json").read_text(encoding="utf-8")))==7
    assert verify(reference)["validation"]["full_payload_matches_service_snapshot"] is True
    assert verify(reference)["validation"]["service_snapshot_git_blob"]=="12ca3d1039a4ef3d7f9c1eac9c85da2c9e59047f"


def test_exact_spatial_and_time_filters_do_not_replicate(reference):
    row=query(reference,limit=1)["records"][0]
    assert query(reference,dong_code=row["dong_code"])["records"]==[row]
    assert all(r["dong_code"].startswith("11110") for r in query(reference,region_code="11110")["records"])
    for filters in [dict(region_code="51110"),dict(period="2025-11"),dict(dong_code="1111051000")]:
        result=query(reference,**filters)
        if not result["records"]:assert result["availability"]["status"]=="data_unavailable"
    assert query(reference,region_code="51110")["total_matched"]==0
    with pytest.raises(ValueError,match="unsupported_original_age_group"):query(reference,age_group="20G")


def test_snapshot_drift_is_rejected_before_export(tmp_path):
    if not SNAPSHOT.exists():pytest.skip("actual snapshot unavailable")
    value=json.loads(SNAPSHOT.read_text(encoding="utf-8"));value["paper_behavior_index"]["records"][0]["score"]+=1
    altered=tmp_path/"snapshot.json";altered.write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")
    with pytest.raises(ValueError,match="service_snapshot_reference_mismatch"):check_source(SOURCE,altered)


def test_tampered_reference_and_removed_hash_are_rejected(reference):
    manifest=reference/"manifest.json";value=json.loads(manifest.read_text(encoding="utf-8"))
    del value["outputs"]["agent_evidence.jsonl"];manifest.write_text(json.dumps(value),encoding="utf-8")
    with pytest.raises(ValueError,match="incomplete_legacy_reference_manifest"):query(reference)


def test_source_score_drift_is_rejected(tmp_path):
    if not SOURCE.exists() or not SNAPSHOT.exists():pytest.skip("actual reference unavailable")
    # Keep the original contract location while supplying a modified score table.
    copied=tmp_path/"outputs/reference";shutil.copytree(SOURCE,copied)
    contract=tmp_path/"src/donghaeng_model";contract.mkdir(parents=True)
    shutil.copy2(SOURCE.parents[1]/"src/donghaeng_model/snapshot.py",contract/"snapshot.py")
    path=copied/"index_scores.csv";text=path.read_text(encoding="utf-8-sig")
    score=json.loads((copied/"service_payload.json").read_text(encoding="utf-8"))["records"][0]["score"]
    path.write_text(text.replace(str(score),str(score+1),1),encoding="utf-8")
    with pytest.raises(ValueError,match="score_changed"):check_source(copied,SNAPSHOT)


def test_final_model_external_query_routes_to_separate_reference(reference,tmp_path):
    outer=tmp_path/"final";target=outer/"external_context/legacy_seoul";shutil.copytree(reference,target)
    dump(outer/"manifest.json",dict(status="completed",is_synthetic=False,
        outputs={p.relative_to(outer).as_posix():sha(p) for p in outer.rglob("*") if p.is_file()}))
    result=external_query(outer,"legacy_seoul",dong_code="1111053000")
    assert result["total_matched"]==1 and result["main_model_join"] is False
    assert result["records"][0]["age_group"]=="20~39세"
