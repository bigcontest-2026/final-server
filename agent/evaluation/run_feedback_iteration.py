"""Run a bounded real-data iteration and write Phoenix-linked evaluation evidence.

Explicit --strategy selects baseline vs real subscription use. No API fallback.
Only public aggregate records and archived official support documents are read.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault('PHOENIX_WORKING_DIR', str(ROOT / 'agent/runtime/phoenix'))
os.environ.setdefault('PHOENIX_ALLOW_EXTERNAL_RESOURCES', 'false')
sys.path.insert(0, str(ROOT / 'service/backend'))
sys.path.insert(0, str(ROOT / 'model'))
from app.config import Settings
from donghaeng_agent.provider import create_provider, model_configuration
from donghaeng_agent.repository import InvestigationRepository
from donghaeng_agent.worker import InvestigationWorker
from donghaeng_agent.tools import InvestigationTools
from donghaeng_agent.observability import TraceConfig, init_observability, flush_observability, observability_status
from phoenix_feedback import evaluate_case, publish_feedback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strategy', required=True, choices=['rule_baseline', 'single_search', 'agent'])
    parser.add_argument('--iteration', required=True)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    if not args.iteration.replace('-', '').replace('_', '').isalnum():
        raise SystemExit('Use a simple iteration label')
    directory = ROOT / 'agent/runtime/enterprise-feedback' / args.iteration
    directory.mkdir(parents=True, exist_ok=False)
    init_observability(TraceConfig(enabled=True, project_name='hermes-enterprise-development'))
    settings = Settings()
    tools = InvestigationTools(settings.research_path, settings.support_manifest_path,
        resource_root=settings.agent_resource_root, capture_root=settings.support_capture_root,
        i5_bundle=settings.i5_bundle_path)
    repo = InvestigationRepository(directory / 'investigations.sqlite3')
    worker = InvestigationWorker(repo, tools, directory / 'checkpoints.sqlite3')
    request = {'region_code':'1168066000','city':'서울','age_group':'20대',
        'purpose':'공개 집계 근거와 공식 지원 조건을 검토하고 미확인 사항을 정리',
        'measurement_as_of':'2025-12','support_as_of':'2026-10-21','sk_mode':'provided',
        'idempotency_key': args.iteration + '-create'}
    case = repo.create(request)
    provider = create_provider()
    versions = {**tools.versions(), 'model':provider.model, 'model_config':model_configuration(provider)}
    execution = repo.enqueue(case['case_id'], {'strategy':args.strategy,'expected_case_version':1,
        'idempotency_key':args.iteration + '-run'}, versions)
    try:
        worker.drain_once()
        saved = repo.get(case['case_id'])
    finally:
        worker.close()
    execution_id = execution['execution_id']
    result = next(e for e in saved['executions'] if e['execution_id'] == execution_id)
    (directory / 'case.json').write_text(json.dumps(saved, ensure_ascii=False, indent=2)+'\n')
    report = evaluate_case(saved, execution_origin='rule_baseline' if args.strategy=='rule_baseline' else 'actual_model',
        execution_id=execution_id, span_id=result['metrics'].get('span_id'))
    flush_observability()
    report['observability'] = observability_status()
    if args.publish and report.get('span_id'):
        try:
            report['submission'] = publish_feedback(report)
        except Exception as exc:
            report['submission'] = {'status':'failed','error_type':type(exc).__name__}
    (directory / 'feedback.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'iteration':args.iteration,'status':result['status'],'error':result.get('error'),
        'trace_id':result['metrics'].get('trace_id'),'span_id':report['span_id'],
        'checks_passed':report['checks_passed'],'checks_total':report['checks_total'],
        'actual_model_calls':result['metrics'].get('actual_model_calls'), 'observability':report['observability']}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
