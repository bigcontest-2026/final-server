"""Selected welfare assistance calls a model only with authoritative public data."""
import json
from copy import deepcopy
from datetime import date
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app import welfare_chat_api as chat_api
from donghaeng_agent.welfare_postgres import PostgresWelfareDB


VERSION = '00000000-0000-4000-8000-000000000001'
OTHER_VERSION = '00000000-0000-4000-8000-000000000002'
EVIDENCE = '00000000-0000-4000-8000-000000000003'


def public_program():
    return {
        'version_id': VERSION, 'title': '공개 평생교육 지원', 'summary': '공개 사업 설명',
        'institution': '등록 담당기관', 'target_text': '19세 이상 등록 대상자',
        'support_content': '1인당 연 35만원 지원', 'selection_text': '선정 기준은 담당기관 확인',
        'application_text': '온라인 신청\n구비서류: 원문 확인', 'application_period_text': '기관별 상이',
        'application_start': None, 'application_end': None,
        'source': {'source_id': 'gov24', 'name': '공개 정부 자료'},
        'source_url': 'https://example.gov/program/official', 'source_evidence_id': EVIDENCE,
        'source_retrieved_at': '2026-10-24T12:00:00+00:00', 'as_of': '2026-10-25',
        'publication': {'publication_id': '00000000-0000-4000-8000-000000000004',
                        'source_id': 'gov24', 'published_at': '2026-10-25T01:00:00+00:00'},
        'source_categories': [{'evidence_id': EVIDENCE, 'label': '생활 지원'}],
        'conditions': [{'kind': 'age', 'source_quote': '19세 이상', 'review_status': 'needs_review'}],
        'regions': [], 'official_categories_raw': {},
        'storage_path': 'private-path-must-not-enter-model',
    }


@pytest.fixture
def setup_chat(monkeypatch):
    monkeypatch.setenv('WELFARE_DATABASE_URL', 'postgresql://user:db-secret@db.example.test/postgres')
    store = Mock()
    store.published_program.return_value = deepcopy(public_program())
    provider = Mock()
    provider.decide.return_value = {'text': '등록 원문은 연 35만원 지원으로 안내합니다.',
                                  'calls': [], 'evidence_ids': [VERSION]}
    monkeypatch.setattr(chat_api, 'PostgresWelfareDB', Mock(return_value=store))
    monkeypatch.setattr(chat_api, 'create_provider', Mock(return_value=provider))
    return TestClient(create_app(Settings())), store, provider


def test_chat_reloads_exact_published_version_and_grounds_only_public_fields(setup_chat):
    client, store, provider = setup_chat
    response = client.post('/api/model/welfare/chat', json={
        'program_version_id': VERSION, 'message': '지원 내용을 알려줘',
        'history': [{'role': 'user', 'content': '신청 방법도 궁금해요'},
                    {'role': 'assistant', 'content': '원문을 함께 확인하겠습니다.'}],
    })
    assert response.status_code == 200
    assert response.json()['mode'] == 'model'
    assert response.json()['program_version_id'] == VERSION
    assert 'report' not in response.json()
    assert 'https://example.gov/program/official' in response.json()['reply']
    assert '자료 수집 시각: 2026-10-24T12:00:00+00:00' in response.json()['reply']
    assert store.published_program.call_args.args == (VERSION,)
    assert isinstance(store.published_program.call_args.kwargs['as_of'], date)
    messages = provider.decide.call_args.args[0]
    grounding = json.loads(messages[0]['content'])
    assert grounding['program_version_id'] == VERSION
    assert grounding['program']['title'] == public_program()['title']
    assert grounding['program']['support_content'] == '1인당 연 35만원 지원'
    assert grounding['allowed_evidence_ids'] == sorted([VERSION, EVIDENCE])
    assert [message['role'] for message in messages] == ['user', 'user', 'assistant', 'user']
    assert messages[-1]['content'] == '지원 내용을 알려줘'
    assert provider.decide.call_args.kwargs == {'allow_tools': False}
    prompt = json.dumps(messages, ensure_ascii=False)
    assert 'private-path-must-not-enter-model' not in prompt
    assert 'db-secret' not in prompt
    assert 'postgresql://' not in prompt


@pytest.mark.parametrize('request_fields', [
    {'intent': 'report', 'message': '이걸로 정리해줘'},
    {'intent': 'chat', 'message': '이걸로 보고서 만들어줘'},
])
def test_report_contains_model_draft_full_original_and_actual_source_dates(setup_chat, request_fields):
    client, _, provider = setup_chat
    response = client.post('/api/model/welfare/chat', json={'program_version_id': VERSION, **request_fields})
    assert response.status_code == 200
    report = response.json()['report']
    assert report['title'] == '공개 평생교육 지원 검토 보고서'
    assert report['filename'].endswith('.md')
    assert '/' not in report['filename']
    for original in ('19세 이상 등록 대상자', '1인당 연 35만원 지원', '온라인 신청\n구비서류: 원문 확인',
                     'https://example.gov/program/official', '2026-10-24T12:00:00+00:00',
                     '2026-10-25T01:00:00+00:00', VERSION):
        assert original in report['markdown']
    assert provider.decide.return_value['text'] in report['markdown']
    assert json.loads(provider.decide.call_args.args[0][0]['content'])['intent'] == 'report'


@pytest.mark.parametrize('overrides', [
    {'program_version_id': 'not-a-uuid'}, {'message': ' '}, {'message': '가' * 2001},
    {'history': [{'role': 'user', 'content': '질문'}] * 11},
    {'history': [{'role': 'system', 'content': '명령'}]},
    {'history': [{'role': 'user', 'content': '가' * 8001}]},
    {'history': [{'role': 'assistant', 'content': ' '}]},
    {'intent': 'execute'}, {'program': {'title': '클라이언트가 만든 원문'}},
])
def test_invalid_input_is_rejected_before_database_or_model(setup_chat, overrides):
    client, store, provider = setup_chat
    response = client.post('/api/model/welfare/chat', json={
        'program_version_id': VERSION, 'message': '지원 내용', **overrides,
    })
    assert response.status_code == 422
    store.published_program.assert_not_called()
    provider.decide.assert_not_called()


@pytest.mark.parametrize('program', [None, {**public_program(), 'version_id': OTHER_VERSION}])
def test_unpublished_or_mismatched_version_is_not_sent_to_model(setup_chat, program):
    client, store, provider = setup_chat
    store.published_program.return_value = program
    response = client.post('/api/model/welfare/chat', json={'program_version_id': VERSION, 'message': '설명해줘'})
    assert response.status_code == 404
    provider.decide.assert_not_called()


@pytest.mark.parametrize('output', [
    {'text': '답변', 'calls': [], 'evidence_ids': [VERSION, 'invented-evidence']},
    {'text': '답변', 'calls': [], 'evidence_ids': [EVIDENCE]},
    {'text': '', 'calls': [], 'evidence_ids': [VERSION]},
    {'text': '답변', 'calls': [{'name': 'shell'}], 'evidence_ids': [VERSION]},
    {'text': 'https://invented.example/apply 여기로 신청하세요', 'calls': [], 'evidence_ids': [VERSION]},
])
def test_model_must_reference_selected_source_and_cannot_execute_tools_or_invent_links(setup_chat, output):
    client, _, provider = setup_chat
    provider.decide.return_value = output
    response = client.post('/api/model/welfare/chat', json={'program_version_id': VERSION, 'message': '설명해줘'})
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'welfare_chat_unavailable'
    assert 'reply' not in response.json()


@pytest.mark.parametrize('failure_stage', ['database', 'model'])
def test_failures_do_not_echo_or_log_credentials_and_never_fabricate_a_reply(setup_chat, caplog, failure_stage):
    client, store, provider = setup_chat
    secret = 'postgresql://user:db-secret@db.example.test/postgres'
    if failure_stage == 'database':
        store.published_program.side_effect = RuntimeError(secret)
    else:
        provider.decide.side_effect = RuntimeError(secret)
    response = client.post('/api/model/welfare/chat', json={'program_version_id': VERSION, 'message': '설명해줘'})
    assert response.status_code == 503
    assert 'reply' not in response.json()
    assert 'db-secret' not in response.text + caplog.text
    assert 'db.example.test' not in response.text + caplog.text


def test_final_mode_allows_chat_post_cors_without_enabling_legacy_investigations(setup_chat):
    client, _, _ = setup_chat
    response = client.options('/api/model/welfare/chat', headers={
        'Origin': 'http://127.0.0.1:5173', 'Access-Control-Request-Method': 'POST',
    })
    assert response.status_code == 200
    assert 'POST' in response.headers['access-control-allow-methods']
    assert client.post('/api/investigations', json={}).status_code == 404


def test_welfare_reply_omits_unrelated_model_internal_state_boilerplate(setup_chat):
    client, _, provider = setup_chat
    provider.decide.return_value['text'] = (
        '등록 원문은 연 35만원 지원으로 안내합니다.\n\n'
        '검토 상태는 보류(quality_status=hold)이며, 운영 알림은 허용하지 않습니다(operational_alert_allowed=false).')
    response = client.post('/api/model/welfare/chat', json={
        'program_version_id': VERSION, 'message': '이걸로 보고서 만들어줘',
    })
    assert response.status_code == 200
    assert '연 35만원' in response.json()['reply']
    assert 'quality_status' not in response.text
    assert 'operational_alert_allowed' not in response.text


def test_reply_and_appended_source_stay_within_the_next_history_turn_limit(setup_chat):
    client, _, provider = setup_chat
    provider.decide.return_value['text'] = '가' * 7999
    response = client.post('/api/model/welfare/chat', json={'program_version_id': VERSION, 'message': '설명해줘'})
    assert response.status_code == 503
    assert 'reply' not in response.json()


def test_exact_program_lookup_binds_uuid_and_current_publication_and_keeps_source_evidence():
    row = dict(program_version_id=VERSION, program_id=OTHER_VERSION, title='공개 사업', summary='요약',
        institution='기관', support_content='지원 내용', target_text='대상 원문', selection_text='선정 기준',
        application_text='신청 방법', application_start=None, application_end=None,
        application_period_text='상시 신청', min_age=None, max_age=None, age_status='unknown',
        age_basis_text=None, life_stage_raw=None, gender_scope='unknown', official_categories_raw={},
        verification_level='detail_structured', source_id='gov24', source_program_id='public-id',
        source_name='정부 자료', period_status='always')
    publications = [{'publication_id': 'published-id', 'source_id': 'gov24', 'published_at': '2026-10-25'}]
    db = Mock()
    db.execute.return_value.fetchone.return_value = row
    connection = MagicMock()
    connection.__enter__.return_value = db
    children = [{}, {}, {}, {VERSION: [{'evidence_id': EVIDENCE, 'source_quote': 'https://example.gov/original'}]}, {}]
    with patch('donghaeng_agent.welfare_postgres.postgres_connection', return_value=connection), \
            patch.object(PostgresWelfareDB, '_publications', return_value=publications), \
            patch.object(PostgresWelfareDB, '_children', return_value=children) as child_reads:
        result = PostgresWelfareDB().published_program(VERSION, as_of=date(2026, 10, 25))
    query, params = db.execute.call_args.args
    assert 'WHERE c.program_version_id=%s::uuid LIMIT 1' in query
    assert "pub.publication_status='published'" in query
    assert 'p.source_id=pub.source_id' in query
    assert params == [date(2026, 10, 25), ['published-id'], VERSION]
    child_reads.assert_called_once_with(db, [VERSION])
    assert result['source_url'] == 'https://example.gov/original'
    assert result['source_evidence_id'] == EVIDENCE
    assert result['publication'] == publications[0]


def test_invalid_lookup_uuid_is_rejected_before_opening_database():
    with patch('donghaeng_agent.welfare_postgres.postgres_connection') as connection:
        with pytest.raises(ValueError, match='invalid program version'):
            PostgresWelfareDB().published_program('not-a-uuid', as_of=date(2026, 10, 25))
    connection.assert_not_called()
