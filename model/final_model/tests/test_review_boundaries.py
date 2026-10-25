import json
from datetime import datetime
import pytest
from final_model.src import run
from final_model.src.common import dump, sha, verify_bundle, write_jsonl
from final_model.src.pipeline import compare_c3_cell, build_behavior, MONTHS, REGIONS, SEXES, AGES
from final_model.src.query import query
from final_model.tests.test_integration import upstream


def test_one_missing_source_preserves_other_source_robust(upstream):
    panel={(m,r,s,a):dict(skt=100.0,card_a=100.0,card_count=3100) for m in MONTHS for r in REGIONS for s in SEXES for a in AGES}
    for i,(s,a) in enumerate((s,a) for s in SEXES for a in AGES):panel['202512','11680',s,a]['card_a']=40.0+i*5
    before={r['record_id']:r for r in build_behavior(panel,*upstream,{},{})}
    panel['202512','11680','MAN','20G']['skt']=None
    after=build_behavior(panel,*upstream,{},{})
    rows=[r for r in after if r['key']['month']=='202512' and r['key']['region_code']=='11680']
    assert len(rows)==10
    for row in rows:
        assert row['sources']['card_a']==before[row['record_id']]['sources']['card_a']
        assert row['sources']['card_a']['robust']['z'] is not None
        assert row['sources']['skt']['robust']['z'] is None
        assert row['robust']['status']=='partial' and row['operational_alert_allowed'] is False


def cell(q=0.1,total=10,flag=False,change=2):
    record=dict(key=dict(month='202510',region_code='11680',sex_code='MAN',age_code='20G'),sources={'card_a':dict(Q=q,change_percent=None if q is None else -1)})
    history={'count':dict(comparable_sum=total,decline_flag=flag,change_pct=change,withheld_reasons=[])}
    raw={('202510','11680','MAN','20G','20251001'):10}
    return record,history,raw


def test_no_c3_baseline_preserves_null():
    record,history,raw=cell(total=None,flag=None,change=None)
    row=compare_c3_cell(record,history,[],raw)
    assert row['c3_count_decline'] is None and row['c3_change_pct'] is None
    assert row['comparison_status']=='unavailable' and row['comparable_sum_validation']=='unavailable_no_comparable_dates'


def test_missing_v3_q_preserves_null_without_losing_c3_audit():
    record,history,raw=cell(q=None)
    row=compare_c3_cell(record,history,[datetime(2025,10,1)],raw)
    assert row['v3_card_decline'] is None and row['v3_change_pct'] is None
    assert row['c3_count_decline'] is False and row['comparable_sum_validation']=='verified'
    assert row['comparison_status']=='unavailable'


def test_withheld_c3_flag_keeps_observed_change():
    record,history,raw=cell(flag=None,change=-15)
    row=compare_c3_cell(record,history,[datetime(2025,10,1)],raw)
    assert row['c3_count_decline'] is None and row['c3_change_pct']==-15 and row['comparison_status']=='unavailable'


@pytest.mark.parametrize('case,message',[('wrong_sum','sum_mismatch'),('missing_raw','raw_date_missing'),('null_with_dates','null_sum_with_comparable_dates'),('sum_without_dates','sum_without_comparable_dates'),('duplicate_date','duplicate_comparable_date')])
def test_c3_audit_rejects_inconsistent_data(case,message):
    record,history,raw=cell();dates=[datetime(2025,10,1)]
    if case=='wrong_sum':history['count']['comparable_sum']=11
    if case=='missing_raw':raw={}
    if case=='null_with_dates':history['count']['comparable_sum']=None
    if case=='sum_without_dates':dates=[]
    if case=='duplicate_date':dates=dates*2
    with pytest.raises(ValueError,match=message):compare_c3_cell(record,history,dates,raw)


def core_bundle(tmp_path):
    bundle=tmp_path/'new';bundle.mkdir()
    write_jsonl(bundle/'behavior_evidence.jsonl',[dict(key=dict(month='202510',region_code='11680',sex_code='MAN',age_code='20G'),historical_reference_id=None)])
    write_jsonl(bundle/'historical_reference.jsonl',[])
    directory=bundle/'survey_mois_reference';directory.mkdir()
    (directory/'national_six_group_profile.csv').write_text('group\n1\n2\n3\n4\n5\n6\n',encoding='utf-8')
    outputs={p.relative_to(bundle).as_posix():sha(p) for p in bundle.rglob('*') if p.is_file()}
    dump(bundle/'manifest.json',dict(status='core_ready',is_synthetic=False,run_id='new',outputs=outputs,final_code_sha256={}))
    latest=tmp_path/'outputs/latest.json';dump(latest,dict(run_id='old'))
    return bundle,latest


def test_finalization_failure_rejects_query_and_keeps_latest(tmp_path,monkeypatch):
    bundle,latest=core_bundle(tmp_path);monkeypatch.setattr(run,'FINAL',tmp_path)
    def fail(*args):
        with pytest.raises(ValueError):query(bundle)
        raise FileNotFoundError('required external context')
    monkeypatch.setattr(run,'_finish_checks',fail)
    with pytest.raises(FileNotFoundError):run.finish(bundle,{})
    assert json.loads((bundle/'manifest.json').read_text())['status']=='failed'
    assert json.loads(latest.read_text())==dict(run_id='old')
    with pytest.raises(ValueError):verify_bundle(bundle)
    with pytest.raises(ValueError):query(bundle)


def test_finalization_success_publishes_only_checked_bundle(tmp_path,monkeypatch):
    bundle,latest=core_bundle(tmp_path)
    baseline=tmp_path/'C1_commercial_baseline/c1_baseline.py';baseline.parent.mkdir();baseline.write_text('# fixture',encoding='utf-8')
    monkeypatch.setattr(run,'FINAL',tmp_path)
    monkeypatch.setattr('final_model.src.pipeline.stage',lambda *args:None)
    def checked(*args):
        assert json.loads(latest.read_text())==dict(run_id='old')
        with pytest.raises(ValueError):query(bundle)
        return dict(status='passed')
    monkeypatch.setattr(run,'_finish_checks',checked)
    run.finish(bundle,dict(data_root=str(tmp_path/'data-set')))
    assert verify_bundle(bundle)['status']=='completed'
    assert json.loads(latest.read_text())==dict(run_id='new') and query(bundle)['total_matched']==1


@pytest.mark.parametrize('status',['core_ready','validating','failed'])
def test_public_query_rejects_every_incomplete_state(tmp_path,status):
    dump(tmp_path/'manifest.json',dict(status=status,is_synthetic=False,outputs={}))
    with pytest.raises(ValueError):query(tmp_path)
