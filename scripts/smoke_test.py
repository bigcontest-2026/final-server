"""Check included bundles and the configured welfare source without an HTTP server."""
import argparse
from collections import Counter
from pathlib import Path
import tempfile

import run_api
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--local-only', action='store_true',
                    help='Verify model rules and report storage without contacting the remote welfare DB.')
args = parser.parse_args()
database = run_api.configure()
from app.config import Settings
from app.final_model import FinalModel
from app.analysis_api import _activity_input, _survey_input
from donghaeng_agent.analysis_reports import build_analysis_report
from donghaeng_agent.analysis_report_store import AnalysisReportStore
from donghaeng_agent.signal_rules import classify_activity, previous_month
from donghaeng_agent.welfare_graph import build_welfare_graph

settings=Settings.from_env()
model=FinalModel(settings)
status=model.status()
assert all(value['ready'] for value in status['sources'].values()),status
rows = model.behavior(limit=100)['records']
assert len(rows) == 100, 'The included activity bundle should contain 100 observations.'
index = {tuple(item['behavior']['key'][key] for key in ('region_code', 'month', 'sex_code', 'age_code')):
         item['behavior'] for item in rows}
categories = Counter()
for item in rows:
    behavior = item['behavior']
    key = behavior['key']
    previous = index.get((key['region_code'], previous_month(key['month']), key['sex_code'], key['age_code']))
    assessment = classify_activity(behavior, previous)
    categories[assessment['category']] += 1
    assert assessment['quality_status'] == 'hold' and assessment['operational_alert_allowed'] is False
assert categories == {'no_threshold_decline': 70, 'single_source_decline': 19,
                      'joint_decline': 10, 'priority_review': 1}, categories

scope = dict(region_code='11680', month='202508', sex_code='MAN', age_code='20G')
activity = _activity_input(model, scope)
survey = _survey_input(model, scope)
report = build_analysis_report(scope=scope, current_rows=activity['current'], previous_rows=activity['previous'],
    i5=survey['i5'], mois=activity['mois'],
    provenance={'activity': activity['provenance'], 'survey': survey['provenance']})
assert report['summary']['no_threshold_decline'] == 1 and report['operational_alert_allowed'] is False
with tempfile.TemporaryDirectory(prefix='hermes-smoke-') as directory:
    store = AnalysisReportStore(Path(directory) / 'analysis_reports.sqlite3')
    saved = store.save(report)
    reused = store.save(report)
    assert reused['reused'] and saved['report_id'] == reused['report_id']
    assert AnalysisReportStore(store.path).get(saved['report_id'])['markdown'] == saved['markdown']
print('Local bundles verified; 5% rules: 70 untriggered, 19 single, 10 joint, 1 priority candidate; durable report save/reload verified.')

if args.local_only:
    print('Local-only verification complete; remote welfare catalog and recommendation queries were not run.')
elif database is None:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from donghaeng_agent.welfare_postgres import PostgresWelfareDB
    catalog = PostgresWelfareDB().catalog(as_of=datetime.now(ZoneInfo('Asia/Seoul')).date(), limit=1)
    assert catalog['data_source'] == 'supabase' and catalog['is_synthetic'] is False
    assert len(catalog['programs']) <= 1
    mapping=PostgresWelfareDB().support_mapping_status()
    assert mapping['mapping_status']=='configured_research',mapping
    result=build_welfare_graph().invoke({'request':dict(
        i5_bundle=model.bundle_path('survey'),activity_bundle=model.bundle_path('activity'),
        db_path='/unused/remote',publication_id=None,region_code='11680',month='202510',
        sex_code='MAN',age_code='60GU',as_of='2026-10-25',research_context=True)})['result']
    assert result['mapping_status']=='configured_research' and result['recommendations'],result['status']
    assert result['support_context']['risk_score_combined'] is False
    assert result['operational_alert_allowed'] is False
    assert all(p['recommendation_reasons'] for p in result['recommendations'])
    print(f"Supabase catalog: {catalog['total']} published records; service mapping verified; "
          f"{len(result['recommendations'])} review candidates with original service evidence.")
else:
    result=build_welfare_graph().invoke({'request':dict(
        i5_bundle=model.bundle_path('survey'),activity_bundle=model.bundle_path('activity'),
        db_path=database,publication_id='erd-demo-v2',region_code='11680',month='202508',
        sex_code='MAN',age_code='20G',as_of='2026-10-25',research_context=True,allow_synthetic=True)})['result']
    assert result['status']=='no_signal',result['status']
    assert result['mapping']['activity_assessment']['category']=='no_threshold_decline'
    assert not result['recommendations'] and not result['excluded'] and not result['held']
    assert result['operational_alert_allowed'] is False
    print('Recommendation graph verified; Gangnam August men in their 20s are below the 5% threshold, so no automatic welfare search runs.')
