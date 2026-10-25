"""The browser path reads complete page details without cached global aggregates."""
import json
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, Mock
from uuid import UUID

import pytest

from donghaeng_agent import welfare_postgres as store_module
from donghaeng_agent.welfare_postgres import PostgresWelfareDB, _normalize_program, _serializable


DAY = date(2026, 10, 25)
VERSION = UUID('00000000-0000-4000-8000-000000000001')
PUBLICATION = UUID('00000000-0000-4000-8000-000000000003')
EVIDENCE = UUID('00000000-0000-4000-8000-000000000005')


def browser_envelope():
    row = dict(program_version_id=VERSION,
        program_id=UUID('00000000-0000-4000-8000-000000000002'), title='공개 사업',
        summary='요약', institution='기관', support_content='지원 내용', target_text='대상 원문',
        selection_text='선정 원문', application_text='신청 원문', application_period_text='2026년 공고 기간',
        application_start=date(2026, 1, 1), application_end=date(2026, 12, 31),
        min_age=19, max_age=None, age_status='bounded', age_basis_text='19세 이상',
        life_stage_raw='성인', gender_scope='unknown', official_categories_raw={'서비스분야': ['생활안정']},
        verification_level='detail_structured', source_id='gov24', source_program_id='public-id',
        source_name='정부24', period_status='ongoing', storage_path='private-storage-path')
    page = dict(total=100, matched=30, rows=[row],
        tags=[dict(program_version_id=VERSION, category_code='income_support', category_name='소득 지원',
            description='생활비 지원 사업', assignment_method='official_category_map', review_status='auto_assigned',
            rule_version='category-rules-v1', evidence_id=EVIDENCE, source_quote='분류 근거 원문')],
        scopes=[dict(program_version_id=VERSION, scope_type='nationwide', region_id=None,
            region_code=None, region_name=None, code_system=None,
            scope_purpose='administering_jurisdiction', mapping_status='exact', original_region_text='전국')],
        rules=[dict(program_version_id=VERSION, condition_type='income', operator='<=',
            condition_value={'percent': 150}, logic_group={'combine': 'unspecified'},
            review_status='needs_review', evidence_id=EVIDENCE, source_quote='중위소득 150% 이하 원문')],
        links=[dict(program_version_id=VERSION, evidence_id=EVIDENCE,
            source_quote='https://www.gov.kr/portal/rcvfvrSvc/dtlEx/123')],
        documents=[dict(program_version_id=VERSION,
            source_retrieved_at=datetime(2026, 10, 24, 15, 13, tzinfo=timezone.utc))],
        categories=[{'category_id': 'income_support', 'label': '소득 지원'}])
    # PostgreSQL jsonb envelope children arrive as JSON strings/numbers, not
    # native UUID/date objects. Exercise that boundary rather than a row cursor.
    return json.loads(json.dumps(_serializable(page)))


@pytest.fixture
def browser_store(monkeypatch):
    page = browser_envelope()
    db = Mock()
    db.execute.return_value.fetchone.return_value = page
    connection = MagicMock()
    connection.__enter__.return_value = db
    monkeypatch.setattr(store_module, 'postgres_connection', lambda: connection)
    publications = _serializable([dict(publication_id=PUBLICATION, source_id='gov24',
        publication_status='published', source_name='정부24', published_at=datetime(2026, 10, 25, tzinfo=timezone.utc))])
    monkeypatch.setattr(PostgresWelfareDB, '_publications', Mock(return_value=publications))
    monkeypatch.setattr(PostgresWelfareDB, '_support_mapping_status', Mock(return_value={
        'mapping_status':'unconfigured','mapping_reason':store_module.MAPPING_REASON,'rule_version':None}))
    monkeypatch.setattr(PostgresWelfareDB, '_region_tree', Mock(return_value=[]))
    monkeypatch.setattr(store_module.CATALOG_CACHE, 'get_or_load', Mock(side_effect=AssertionError('browser must read without cache')))
    for name in ('_stats', '_region_facets', '_children'):
        monkeypatch.setattr(PostgresWelfareDB, name, Mock(side_effect=AssertionError(f'browser must not call {name}')))
    return PostgresWelfareDB(), db, page


def test_browser_reads_complete_source_details_without_cache_or_global_facet_counts(browser_store):
    store, db, page = browser_store
    result = store.catalog(as_of=DAY, view='browser', offset=12, limit=12)
    assert result['view'] == 'browser' and result['facet_counts_included'] is False
    assert (result['total'], result['matched'], result['offset'], result['limit'], result['next_offset']) == (100, 30, 12, 12, 24)
    assert result['publication_ids'] == [str(PUBLICATION)]
    assert result['publications'][0]['published_at'] == '2026-10-25T00:00:00+00:00'
    program = result['programs'][0]
    expected = _normalize_program(page['rows'][0], *(page[key] for key in ('tags', 'scopes', 'rules', 'links', 'documents')))
    assert program == expected
    assert (program['target_text'], program['selection_text'], program['application_text']) == ('대상 원문', '선정 원문', '신청 원문')
    assert program['official_categories_raw'] == {'서비스분야': ['생활안정']}
    assert program['source_categories'][0]['source_quote'] == '분류 근거 원문'
    assert program['conditions'][0]['source_quote'] == '중위소득 150% 이하 원문'
    assert program['source_retrieved_at'] == '2026-10-24T15:13:00+00:00'
    assert program['source_url'] == 'https://www.gov.kr/portal/rcvfvrSvc/dtlEx/123'
    assert 'private-storage-path' not in json.dumps(result)
    assert result['categories'] == page['categories']
    for options in (result['categories'], result['domains'], result['statuses'], result['regions']):
        assert all('count' not in option for option in options)
    assert db.execute.call_count == 1  # Publications/tree mocked; one page envelope remains.


def test_browser_binds_filters_before_page_limit_and_uses_stable_order(browser_store):
    store, db, _ = browser_store
    result = store.catalog(as_of=DAY, view='browser', q="공개 %_'", category_id='income_support',
        status='ongoing', offset=12, limit=12)
    query, args = db.execute.call_args.args
    assert args == [DAY, [str(PUBLICATION)], "공개 %_'", "공개 %_'", 'income_support', 'ongoing', 12, 12]
    assert "공개 %_'" not in query
    assert query.index('c.period_status=%s') < query.index('page_rows AS MATERIALIZED') < query.index('LIMIT %s OFFSET %s')
    assert 'ORDER BY c.title,c.program_version_id LIMIT %s OFFSET %s' in query
    assert result['next_offset'] == 24


def test_browser_national_scope_and_last_page_keep_ranking_and_empty_pages_valid(browser_store):
    store, db, page = browser_store
    page['rows'][0]['region_match_rank'] = 2
    result = store.catalog(as_of=DAY, view='browser', region_level='national', offset=24, limit=12)
    assert result['programs'][0]['region_match_label'] == '전국'
    assert result['next_offset'] is None
    query, args = db.execute.call_args.args
    assert args == [DAY, [str(PUBLICATION)], [], [], 12, 24]
    assert query.index('c.region_match_rank<3') < query.index('page_rows AS MATERIALIZED')
    assert 'ORDER BY c.region_match_rank,c.title,c.program_version_id LIMIT %s OFFSET %s' in query
    page.update(rows=[], matched=0)
    result = store.catalog(as_of=DAY, view='browser', region_level='national', offset=0, limit=12)
    assert result['programs'] == [] and result['matched'] == 0 and result['next_offset'] is None
