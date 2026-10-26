"""Actual nationwide values through HTTP, agent measurement and LangGraph rule execution."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings,DEFAULT_I5_BUNDLE
from app.main import create_app

ROOT=Path(__file__).resolve().parents[3]


@pytest.fixture
def application(tmp_path):
    pytest.importorskip("donghaeng_agent", reason="optional legacy agent tests: install requirements-legacy.txt")
    pytest.importorskip("donghaeng_model", reason="optional research snapshot tests: install requirements-legacy.txt")
    assert (DEFAULT_I5_BUNDLE/"validation_report.json").is_file()
    manifest=tmp_path/"support.json";manifest.write_text('{"sources":[]}',encoding="utf-8")
    return create_app(Settings(mode="research",support_manifest_path=manifest,
        investigation_worker_enabled=False,investigation_db_path=tmp_path/"cases.sqlite3",investigation_checkpoint_path=tmp_path/"checkpoints.sqlite3"))


@pytest.fixture(params=["final", "research"])
def i5_application(request):
    if request.param == "research":
        return request.getfixturevalue("application")
    return create_app(Settings(mode="final"))


def payload(code="2611051000",city="부산"):
    return dict(region_code=code,city=city,age_group="20대",purpose="전국 I5 참고지수 조회 검증",measurement_as_of="2025-12",
        support_as_of="2026-10-17",sk_mode="excluded",idempotency_key="national-i5-create")


def test_nationwide_api_exact_values_and_input_validation(i5_application):
    with TestClient(i5_application) as client:
        for code in ("26110","41110","36110","50130"):
            result=client.get('/api/research/i5',params=dict(region_code=code,month="202512"))
            assert result.status_code==200 and result.json()["total_matched"]==1
        assert client.get('/api/research/i5',params=dict(region_code="bad")).status_code==422
        assert client.get('/api/research/i5',params=dict(region_code="11999",month="202512")).json()["availability"]["status"]=="data_unavailable"


def test_actual_agent_graph_includes_national_i5_in_brief(application):
    # Real HTTP + InvestigationTools + LangGraph; rule_baseline invokes no external model.
    with TestClient(application) as client:
        result=client.post('/api/investigations',json=payload())
        assert result.status_code==201,result.text
        case=result.json();cid=case['case_id']
        response=client.post(f'/api/investigations/{cid}/runs',json=dict(strategy='rule_baseline',expected_case_version=case['case_version'],idempotency_key='national-i5-run'))
        assert response.status_code==202,response.text
        application.state.investigation_worker.drain_once()
        final=client.get(f'/api/investigations/{cid}').json()
        assert final['status']=='awaiting_review',final.get('executions')
        brief=final['briefs'][0]
        observation=next(o for o in brief['observations'] if o['kind']=='survey_i5_population_reference')
        expected=client.get('/api/research/i5',params=dict(dong_code='2611051000',month='202512')).json()['records'][0]
        assert observation['data']==expected and observation['status']=='available'
        assert brief['quality_status']=='hold' and brief['operational_alert_allowed'] is False
        assert next(c for c in brief['claims'] if c['claim_id']=='claim_survey_i5_population_reference')['text']=='인구구성 가중 I5 참고지수 확인'
        assert brief['model_called'] is False
        evidence=next(e for e in final['evidence'] if e['source_id']=='national_i5_population_reference')
        assert evidence['extracted_values']==expected


def test_mismatched_province_is_rejected(application):
    with TestClient(application) as client:
        result=client.post('/api/investigations',json=payload(city='서울'))
        assert result.status_code==422 and result.json()['detail']['code']=='region_city_mismatch'


def test_incomplete_i5_bundle_is_not_served(application,tmp_path):
    broken=tmp_path/'broken';broken.mkdir()
    (broken/'manifest.json').write_text('{"contract_version":"survey_area_isolation_index_v1","status":"failed","is_synthetic":false}',encoding='utf-8')
    application.state.investigation_tools.i5_bundle=broken
    with TestClient(application) as client:
        result=client.get('/api/research/i5',params=dict(region_code='26110',month='202512'))
        assert result.status_code==503


def test_queued_execution_rejects_changed_i5_version(application,monkeypatch):
    with TestClient(application) as client:
        created=client.post('/api/investigations',json=payload()).json();cid=created['case_id']
        assert client.post(f'/api/investigations/{cid}/runs',json=dict(strategy='rule_baseline',expected_case_version=created['case_version'],idempotency_key='queued-i5-version')).status_code==202
        tools=application.state.investigation_tools;original=tools.versions()
        monkeypatch.setattr(tools,'versions',lambda:{**original,'i5_manifest_sha256':'0'*64})
        application.state.investigation_worker.drain_once()
        final=client.get(f'/api/investigations/{cid}').json()
        assert final['briefs']==[]
        assert final['executions'][-1]['status']=='failed'
