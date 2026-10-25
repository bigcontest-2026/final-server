"""Remote selection must not expose credentials or silently serve demo records."""
import json
import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


REMOTE = {
    'WELFARE_DATABASE_URL': 'postgresql://test-user:test-secret@db.example.test/postgres',
    'WELFARE_DATABASE_PASSWORD': 'separate-test-secret',
    'WELFARE_PUBLICATION_ID': '',
}


def test_remote_catalog_uses_remote_filters_without_sqlite():
    client = TestClient(create_app(Settings()))
    result = {'schema_version': 'welfare_catalog_v1', 'data_source': 'supabase',
              'is_synthetic': False, 'programs': [], 'total': 0, 'matched': 0}
    with patch.dict('os.environ', REMOTE), \
            patch('donghaeng_agent.welfare_postgres.PostgresWelfareDB') as store, \
            patch('app.welfare_api._read_catalog') as local:
        store.return_value.catalog.return_value = result
        response = client.get('/api/model/welfare/catalog', params={
            'q': '돌봄', 'region_code': '11680', 'category_id': 'daily_living',
            'as_of': '2026-10-25', 'offset': 12, 'limit': 12,
        })
    assert response.status_code == 200
    assert response.json() == result
    store.assert_called_once_with(None)
    args = store.return_value.catalog.call_args.kwargs
    assert (args['q'], args['region_code'], args['offset'], args['limit']) == ('돌봄', '11680', 12, 12)
    assert 'view' not in args
    local.assert_not_called()


def test_remote_catalog_forwards_browser_view_with_the_same_filters_and_page():
    client = TestClient(create_app(Settings()))
    result = {'schema_version': 'welfare_catalog_v1', 'view': 'browser', 'data_source': 'supabase',
              'facet_counts_included': False, 'is_synthetic': False, 'programs': [], 'total': 0, 'matched': 0}
    with patch.dict('os.environ', REMOTE), \
            patch('donghaeng_agent.welfare_postgres.PostgresWelfareDB') as store, \
            patch('app.welfare_api._read_catalog') as local:
        store.return_value.catalog.return_value = result
        response = client.get('/api/model/welfare/catalog', params={
            'view': 'browser', 'q': '돌봄', 'region_level': 'sgg', 'region_code': '11680',
            'category_id': 'daily_living', 'status': 'always', 'offset': 12, 'limit': 12,
        })
    assert response.status_code == 200
    assert response.json() == result
    assert store.return_value.catalog.call_args.kwargs.items() >= {
        'view': 'browser', 'q': '돌봄', 'region_level': 'sgg', 'region_code': '11680',
        'category_id': 'daily_living', 'status': 'always', 'offset': 12, 'limit': 12,
    }.items()
    local.assert_not_called()


def test_remote_catalog_rejects_an_unknown_view_before_querying_either_store():
    client = TestClient(create_app(Settings()))
    with patch.dict('os.environ', REMOTE), \
            patch('donghaeng_agent.welfare_postgres.PostgresWelfareDB') as store, \
            patch('app.welfare_api._read_catalog') as local:
        response = client.get('/api/model/welfare/catalog', params={'view': 'summary'})
    assert response.status_code == 422
    store.assert_not_called()
    local.assert_not_called()


def test_remote_failure_is_503_without_demo_fallback_or_secret():
    from donghaeng_agent.welfare_connection import WelfareDatabaseError
    client = TestClient(create_app(Settings()))
    with patch.dict('os.environ', REMOTE), \
            patch('donghaeng_agent.welfare_postgres.PostgresWelfareDB.catalog',
                  side_effect=WelfareDatabaseError('database_unavailable', 'DB 조회 실패')), \
            patch('app.welfare_api._read_catalog') as local:
        response = client.get('/api/model/welfare/catalog')
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'welfare_catalog_unavailable'
    assert 'test-secret' not in response.text
    assert 'db.example.test' not in response.text
    local.assert_not_called()


def test_graph_request_never_contains_database_credentials():
    from donghaeng_agent.welfare_graph import build_welfare_graph
    seen = {}

    def runner(**request):
        seen.update(request)
        return {'status': 'held', 'operational_alert_allowed': False}

    client = TestClient(create_app(Settings()))
    graph = build_welfare_graph(runner)
    with patch.dict('os.environ', REMOTE), \
            patch('donghaeng_agent.welfare_graph.build_welfare_graph', return_value=graph), \
            patch('app.welfare_api.FinalModel.bundle_path', return_value=Path('/test-bundle')):
        response = client.get('/api/model/welfare', params={
            'region_code': '11680', 'month': '202508', 'sex_code': 'MAN', 'age_code': '20G',
        })
    assert response.status_code == 200
    assert seen['publication_id'] is None
    encoded = json.dumps(seen, default=str)
    assert 'postgresql://' not in encoded
    assert 'test-secret' not in encoded
    assert 'DATABASE_PASSWORD' not in encoded


def test_remote_mapping_hold_preserves_model_evidence_without_demo_search():
    from donghaeng_agent.welfare_pipeline import run
    i5 = {'region_code': '11680', 'month': '202508', 'spatial_unit': 'sgg',
          'record_id': 'i5:fixture', 'domain_scores': dict(E=20, P=30, C=40, U=50, O=10)}
    behavior = {'key': {'region_code': '11680', 'month': '202508', 'sex_code': 'MAN', 'age_code': '20G'}, 'spatial_unit': 'sgg',
                'record_id': 'activity:fixture', 'comparison_status': 'computed',
                'rule': {'candidate': True}, 'robust': {'candidate': False},
                'sources': {'card_a': {'calculation_status': 'computed', 'change_percent': -6}}}
    readers = {
        'final_model.src.survey_area_index': SimpleNamespace(query=Mock(return_value={
            'total_matched': 1, 'records': [i5], 'availability': 'available'})),
        'final_model.src.query': SimpleNamespace(query=Mock(return_value={
            'total_matched': 1, 'records': [{'behavior': behavior}],
            'availability': 'available', 'run_id': 'fixture-run'})),
    }
    with patch.dict('os.environ', REMOTE), patch.dict('sys.modules', readers), \
            patch('donghaeng_agent.welfare_support_context.load_support_context',return_value={}), \
            patch('donghaeng_agent.welfare_postgres.PostgresWelfareDB') as remote, \
            patch('donghaeng_agent.welfare_pipeline.ERDWelfareDB') as local:
        remote.return_value.search_support.return_value = {
            'status': 'mapping_required', 'programs': [], 'publication_id': None,
            'publication_ids': ['published-fixture'],
            'coverage': {'total': 42, 'matched': 0, 'unclassified': 3},
        }
        result = run(i5_bundle='/unused/i5', activity_bundle='/unused/activity',
                     db_path='/unused/demo', publication_id=None, region_code='11680',
                     month='202508', sex_code='MAN', age_code='20G',
                     as_of='2026-10-25', research_context=True)
    assert result['status'] == 'held'
    assert result['data_source'] == 'supabase'
    assert result['mapping_status'] == 'unconfigured'
    assert result['welfare_data_synthetic'] is False
    assert result['i5'] == i5 and result['activity'] == behavior
    assert result['coverage']['total'] == 42
    assert result['recommendations'] == []
    assert '복지 목록' in result['mapping']['reasons'][-1]
    local.assert_not_called()


@pytest.mark.parametrize('status', ['always', 'varies', 'announced', 'not_required'])
def test_remote_catalog_accepts_application_period_text_statuses(status):
    client = TestClient(create_app(Settings()))
    result = {'schema_version': 'welfare_catalog_v1', 'data_source': 'supabase',
              'is_synthetic': False, 'programs': [], 'total': 0, 'matched': 0,
              'statuses': [{'code': status, 'label': '기간 표시', 'count': 0}]}
    with patch.dict('os.environ', REMOTE), \
            patch('donghaeng_agent.welfare_postgres.PostgresWelfareDB') as store, \
            patch('app.welfare_api._read_catalog') as local:
        store.return_value.catalog.return_value = result
        response = client.get('/api/model/welfare/catalog', params={'status': status})
    assert response.status_code == 200
    assert response.json() == result
    assert store.return_value.catalog.call_args.kwargs['status'] == status
    local.assert_not_called()


def test_remote_catalog_rejects_unrecognized_application_period_status():
    client = TestClient(create_app(Settings()))
    with patch.dict('os.environ', REMOTE), \
            patch('donghaeng_agent.welfare_postgres.PostgresWelfareDB') as store:
        response = client.get('/api/model/welfare/catalog', params={'status': 'currently_eligible'})
    assert response.status_code == 422
    store.assert_not_called()
