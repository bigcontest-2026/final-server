"""Development/replay integration and paired SK comparison; never a locked/model score."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

AGENT_ROOT = Path(__file__).resolve().parents[1]
ROOT = AGENT_ROOT.parent
OUT = AGENT_ROOT / 'evaluation/replays'
sys.path.insert(0, str(ROOT / 'service/backend'))
# Import-time global app also writes only to this task's local records.
os.environ.setdefault('INVESTIGATION_DB_PATH', str(OUT / 'development_import.sqlite3'))
os.environ.setdefault('INVESTIGATION_CHECKPOINT_PATH', str(OUT / 'development_import_checkpoints.sqlite3'))
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def key():
    return 'eval-' + uuid.uuid4().hex


def must(response, status=200):
    assert response.status_code == status, (response.status_code, response.text[:700])
    return response.json()


def create(client, city, code, mode='provided', age='20대'):
    return must(client.post('/api/investigations', json=dict(city=city, region_code=code,
        age_group=age, purpose='개발 재현 검증: 과거 활동 근거와 현재 공식 지원 조건 확인',
        measurement_as_of='2025-12', support_as_of='2026-10-05', sk_mode=mode, idempotency_key=key())),201)


def execute(client, app, case):
    started = time.perf_counter()
    cmd=dict(strategy='rule_baseline',expected_case_version=case['case_version'],idempotency_key=key())
    job=must(client.post(f"/api/investigations/{case['case_id']}/runs",json=cmd),202)
    for _ in range(20):
        if not app.state.investigation_worker.drain_once(): break
    result=must(client.get(f"/api/investigations/{case['case_id']}"))
    assert result['status']=='awaiting_review', result['executions']
    assert len(result['briefs'])==1
    return result,(time.perf_counter()-started)*1000,job


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    run_id=uuid.uuid4().hex[:10]
    run_dir=OUT/'results'/run_id
    settings=Settings.from_env()
    from dataclasses import replace
    settings=replace(settings, mode='research', investigation_worker_enabled=False,
        investigation_db_path=run_dir/'cases.sqlite3', investigation_checkpoint_path=run_dir/'checkpoints.sqlite3')
    app=create_app(settings)
    checks=[]
    def check(name, passed, **evidence):
        checks.append(dict(check=name,passed=bool(passed),**evidence))
    paired=[]
    with TestClient(app) as client:
        for family,city,code in [('gaepo','서울','1168066000'),('sabuk','춘천','5111036000'),('shinsa','서울','1168051000'),('gyodong','춘천','5111052000')]:
            results={}
            for mode in ['excluded','provided','quality_mutation']:
                result,ms,job=execute(client,app,create(client,city,code,mode))
                brief=result['briefs'][-1]
                observations=brief['observations']
                sk=next((o for o in observations if o['kind']=='sk'),None)
                conditions=[{'id':c['id'],'conditions':c['conditions']} for c in brief['support_candidates']]
                tool_calls=result['executions'][-1].get('tool_calls',[])
                actions=brief['next_actions']
                row=dict(case_family=family,city=city,region_code=code,sk_mode=mode,
                    execution_mode='official_capture_replay',measurement_origin='mutated_fixture' if mode=='quality_mutation' else 'real_snapshot',
                    model_called=brief['model_called'],case_status=result['status'],measurement_status=brief['measurement_status'],
                    support_status=brief['support_status'],sk_observation_present=sk is not None,sk_status=sk['status'] if sk else 'excluded',
                    next_action_types=[a['type'] for a in actions],candidate_ids=[c['id'] for c in brief['support_candidates']],
                    excluded_ids=[c['id'] for c in brief['excluded_candidates']],tool_calls=len(tool_calls),server_replay_ms=ms,
                    model_input_tokens=0,model_output_tokens=0,model_cost=0,model_cost_scope='no_model_call')
                paired.append(row);results[mode]=dict(case=result,brief=brief,conditions=conditions,row=row)
                save(run_dir/f'{family}_{mode}.json',result)
                check(f'{family}:{mode}:no_personal_eligibility',all(c['eligibility_status']!='eligible' for c in brief['support_candidates']))
                check(f'{family}:{mode}:closed_candidate_excluded','seoul-family-training-ended' not in row['candidate_ids'])
                check(f'{family}:{mode}:historical_current_split',brief['scope']['measurement_as_of']=='2025-12' and brief['scope']['support_as_of']=='2026-10-05')
                check(f'{family}:{mode}:evidence_resolves',set(brief['evidence_ids'])<=set(e['evidence_id'] for e in result['evidence']))
                check(f'{family}:{mode}:rule_not_model',not brief['model_called'])
            m0,m1,m2=[results[m] for m in ['excluded','provided','quality_mutation']]
            check(f'{family}:M0_no_sk_observation',not m0['row']['sk_observation_present'])
            check(f'{family}:M0_no_sk_evidence',all(e.get('kind')!='sk' for e in m0['case']['evidence']))
            check(f'{family}:M1_actual_selected_month',next(o for o in m1['brief']['observations'] if o['kind']=='sk')['data']['selected']['period']=='2025-12')
            check(f'{family}:M2_quality_hold',m2['row']['sk_status']!='available' and 'request_measurement_evidence' in m2['row']['next_action_types'])
            check(f'{family}:support_conditions_unchanged',m0['conditions']==m1['conditions']==m2['conditions'])
            if family in ['sabuk','gyodong']:
                check(f'{family}:M1_context_action', 'check_context' in m1['row']['next_action_types'])
                check(f'{family}:M0_no_context_action', 'check_context' not in m0['row']['next_action_types'])
        # Repeat command has exactly one saved case, brief and interrupted execution.
        req=dict(city='서울',region_code='1168066000',age_group='30대',purpose='멱등·재시작 개발검증',measurement_as_of='2025-12',support_as_of='2026-10-05',sk_mode='provided',idempotency_key=key())
        first=must(client.post('/api/investigations',json=req),201);same=must(client.post('/api/investigations',json=req),201)
        check('same_create_key_same_case',first['case_id']==same['case_id'])
        bad={**req,'purpose':'같은키 다른내용'}
        check('same_create_key_changed_input_conflict',client.post('/api/investigations',json=bad).status_code==409)
        stored,ms,job=execute(client,app,first);stored_id=stored['case_id'];stored_brief=stored['briefs'][-1]
        check('city_code_mismatch_rejected',client.post('/api/investigations',json={**req,'city':'춘천','idempotency_key':key()}).status_code in [400,422,503])
    # New app/worker/checkpointer: recovery is from disk, not previous objects.
    fresh=create_app(settings)
    with TestClient(fresh) as client:
        restored=must(client.get(f'/api/investigations/{stored_id}'))
        check('new_app_restores_interrupted_case',restored['status']=='awaiting_review' and restored['briefs'][-1]['brief_id']==stored_brief['brief_id'])
        cmd=dict(brief_id=stored_brief['brief_id'],brief_version=stored_brief['brief_version'],expected_case_version=restored['case_version'],decision='request_info',reason='개발 검증용: 개인 출생일과 거주·접수 수용은 사용자 검토 대기',reviewer_type='developer_verification',idempotency_key=key())
        review=must(client.post(f'/api/investigations/{stored_id}/reviews',json=cmd),202)
        for _ in range(20):
            if not fresh.state.investigation_worker.drain_once():break
        final=must(client.get(f'/api/investigations/{stored_id}'))
        check('review_resume_same_graph_execution',final['executions'][-1]['execution_id']==job['execution_id'] and len(final['briefs'])==1 and len(final['reviews'])==1)
        check('review_resume_complete',final['executions'][-1]['status']=='succeeded')
        retry=must(client.post(f'/api/investigations/{stored_id}/reviews',json=cmd),202)
        check('review_command_idempotent',retry==review)
        recheck=must(client.post(f'/api/investigations/{stored_id}/recheck',json=dict(expected_case_version=final['case_version'],idempotency_key=key())))
        check('unchanged_source_no_new_brief',not recheck['requires_new_run'] and len(must(client.get(f'/api/investigations/{stored_id}'))['briefs'])==1)
        check('developer_review_not_user_review',all(r.get('reviewer_type')=='developer_verification' for r in final['reviews']))
        save(run_dir/'restarted_reviewed_case.json',final);save(run_dir/'recheck.json',recheck)
    save(run_dir/'checks.json',checks)
    with (run_dir/'sk_paired_results.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(paired[0]));writer.writeheader();writer.writerows(paired)
    summary=dict(run_id=run_id,execution_scope='development_auto_checks_official_capture_replay',passed=all(c['passed'] for c in checks),checks_passed=sum(c['passed'] for c in checks),checks_total=len(checks),region_cases=4,independent_isolation_labels=0,executions=12,sk_excluded_cases=4,quality_mutation_cases=4,locked_evaluation=False,user_review=False,model_called=False,comparisons={'R':'executed','S':'not_executed_missing_model_credentials_and_configuration','A':'not_executed_missing_model_credentials_and_configuration'},result_path=str(run_dir))
    summary['limitations']=['규칙 분기의 차이를 실제 모델 적응 성능으로 해석하지 않음','날씨·휴일 맥락 커넥터 미연결로 활동 변화 원인 미확정','독립 고립 라벨·외적 타당도·미래 예측·예방 효과 미검증']
    save(OUT/'integration_summary.json',summary)
    save(OUT/'latest_result.json',dict(path=str(run_dir)))
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    if not summary['passed']:raise SystemExit(1)

if __name__=='__main__':main()
