"""Region options are independent of the browser's paged program query."""
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.welfare_api import welfare_router
from donghaeng_agent.welfare_connection import WelfareDatabaseError


REMOTE = {'WELFARE_DATABASE_URL': 'postgresql://test-user:test-secret@db.example.test/postgres'}


def test_remote_regions_forwards_date_without_loading_the_program_catalog():
    client = TestClient(create_app(Settings()))
    regions = [{'code': '11680', 'name': '강남구', 'level': 'sgg'},
               {'code': '1168051000', 'name': '신사동', 'level': 'administrative_dong', 'parent_code': '11680'}]
    with patch.dict('os.environ', REMOTE), \
            patch('donghaeng_agent.welfare_postgres.PostgresWelfareDB') as store, \
            patch('app.welfare_api._read_catalog') as local:
        store.return_value.catalog_regions.return_value = regions
        response = client.get('/api/model/welfare/regions', params={'as_of': '2026-10-25'})
    assert response.status_code == 200
    assert response.json() == {'as_of': '2026-10-25', 'regions': regions}
    store.assert_called_once_with()
    store.return_value.catalog_regions.assert_called_once_with(as_of=date(2026, 10, 25))
    store.return_value.catalog.assert_not_called()
    local.assert_not_called()


def test_regions_rejects_unsupported_query_before_database_access():
    client = TestClient(create_app(Settings()))
    with patch.dict('os.environ', REMOTE), \
            patch('donghaeng_agent.welfare_postgres.PostgresWelfareDB') as store, \
            patch('app.welfare_api._read_catalog') as local:
        response = client.get('/api/model/welfare/regions', params={'offset': 12})
    assert response.status_code == 422
    store.assert_not_called()
    local.assert_not_called()


def test_regions_obeys_institution_feature_gate_before_reading_database():
    # Exercise the route's feature gate directly; authentication has its own suite.
    app = FastAPI()
    app.include_router(welfare_router(SimpleNamespace(**{**vars(Settings()), 'access_mode': 'institution'})))
    with patch.dict('os.environ', {**REMOTE, 'WELFARE_RESEARCH_ENABLED': 'false'}), \
            patch('donghaeng_agent.welfare_postgres.PostgresWelfareDB') as store, \
            patch('app.welfare_api._read_catalog') as local:
        response = TestClient(app).get('/api/model/welfare/regions')
    assert response.status_code == 503
    assert response.json()['detail'] == '복지 자료 조회가 활성화되지 않았습니다.'
    store.assert_not_called()
    local.assert_not_called()


def test_remote_region_failure_returns_safe_error_without_sqlite_fallback():
    client = TestClient(create_app(Settings()))
    with patch.dict('os.environ', REMOTE), \
            patch('donghaeng_agent.welfare_postgres.PostgresWelfareDB') as store, \
            patch('app.welfare_api._read_catalog') as local:
        store.return_value.catalog_regions.side_effect = WelfareDatabaseError(
            'database_unavailable', 'postgresql://test-user:test-secret@db.example.test/postgres')
        response = client.get('/api/model/welfare/regions')
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'welfare_regions_unavailable'
    assert 'test-secret' not in response.text
    assert 'db.example.test' not in response.text
    local.assert_not_called()


def test_local_region_options_match_available_tree_and_leave_database_unchanged(tmp_path, monkeypatch):
    from donghaeng_agent.welfare_erd import create_demo

    database = tmp_path / 'catalog.sqlite'
    create_demo(database)
    before = database.read_bytes()
    monkeypatch.setenv('WELFARE_DATABASE_URL', '')
    monkeypatch.setenv('WELFARE_DB_PATH', str(database))
    monkeypatch.setenv('WELFARE_PUBLICATION_ID', 'erd-demo-v2')
    client = TestClient(create_app(Settings()))
    params = {'as_of': '2026-10-25'}
    expected = client.get('/api/model/welfare/catalog', params=params)
    response = client.get('/api/model/welfare/regions', params=params)
    assert expected.status_code == response.status_code == 200
    assert response.json() == {'as_of': '2026-10-25', 'regions': expected.json()['available_regions']}
    assert any(region['code'] == '11680' and region['level'] == 'sgg'
               for region in response.json()['regions'])
    assert database.read_bytes() == before
