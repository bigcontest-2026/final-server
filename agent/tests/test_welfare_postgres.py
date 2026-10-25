"""Offline contract checks for real-schema normalization and bounded read queries."""
import json
import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, Mock, patch
from uuid import UUID

from donghaeng_agent.welfare_connection import WelfareDatabaseError
from donghaeng_agent.welfare_postgres import (
    PostgresWelfareDB, _filters, _normalize_program, _official_url,
)


VERSION = UUID('00000000-0000-4000-8000-000000000001')
PROGRAM = UUID('00000000-0000-4000-8000-000000000002')
PUBLICATION = UUID('00000000-0000-4000-8000-000000000003')
REGION = UUID('00000000-0000-4000-8000-000000000004')
EVIDENCE = UUID('00000000-0000-4000-8000-000000000005')


def program_row():
    return dict(program_version_id=VERSION, program_id=PROGRAM, title='공개 사업',
        summary='요약', institution='기관', support_content='지원 내용', target_text='대상 원문',
        selection_text='선정 원문', application_text='신청 원문', application_period_text=None,
        application_start=date(2026, 1, 1), application_end=date(2026, 12, 31),
        min_age=19, max_age=None, age_status='bounded', age_basis_text='19세 이상',
        life_stage_raw='성인', gender_scope='unknown', official_categories_raw={'서비스분야': ['생활안정']},
        verification_level='detail_structured', source_id='gov24', source_program_id='public-id',
        source_name='정부24', period_status='ongoing', storage_path='must-never-leak')


def publication_rows():
    return [dict(publication_id=PUBLICATION, source_id='gov24', publication_status='published',
                 source_name='정부24', published_at=datetime(2026, 10, 25, 6, 26, tzinfo=timezone.utc))]


def stats_row():
    return dict(total=16244, unclassified=212, ongoing=300, upcoming=30, ended=103,
                unknown=4811, always=6500, varies=900, announced=3400, not_required=200)


def cursor(*, rows=None, row=None):
    return Mock(fetchall=Mock(return_value=rows), fetchone=Mock(return_value=row))


class NormalizationTests(unittest.TestCase):
    def test_preserves_category_condition_quotes_and_collection_time_without_internal_metadata(self):
        tag_quote = '서비스분야: 생활안정\n지원 내용 원문'
        condition_quote = '기준중위소득 150% 이하\n세부 대상은 공고문 확인'
        retrieved = datetime(2026, 10, 24, 15, 13, 18, tzinfo=timezone.utc)
        tags = [dict(category_code='income_support', category_name='소득·생활 안정',
            description='소득과 생활비 안정을 돕는 사업', assignment_method='official_category_map',
            review_status='auto_assigned', rule_version='category-rules-v1.2', evidence_id=EVIDENCE,
            source_quote=tag_quote, locator={'private_path': 'must-never-leak'})]
        rules = [dict(condition_type='income', operator='<=', condition_value={'percent': 150},
            logic_group={'combine': 'unspecified'}, review_status='needs_review', evidence_id=EVIDENCE,
            source_quote=condition_quote, locator={'private_path': 'must-never-leak'})]
        documents = [dict(source_retrieved_at=retrieved, storage_path='must-never-leak',
                          endpoint='must-never-leak', raw_sha256='must-never-leak')]
        data = _normalize_program(program_row(), tags=tags, rules=rules, documents=documents)
        self.assertEqual(data['source_retrieved_at'], retrieved.isoformat())
        self.assertEqual(data['source_categories'][0]['description'], tags[0]['description'])
        self.assertEqual(data['source_categories'][0]['source_quote'], tag_quote)
        self.assertEqual(data['conditions'][0]['source_quote'], condition_quote)
        self.assertEqual(data['conditions'][0]['review_status'], 'needs_review')
        self.assertEqual(data['official_categories_raw'], {'서비스분야': ['생활안정']})
        encoded = json.dumps(data, ensure_ascii=False)
        for field in ('must-never-leak', 'storage_path', 'endpoint', 'raw_sha256', 'locator'):
            self.assertNotIn(field, encoded)
        self.assertIsNone(_normalize_program(program_row())['source_retrieved_at'])

    def test_preserves_explicit_period_even_without_date_columns(self):
        row = program_row()
        row.update(application_start=None, application_end=None, period_status='announced',
            application_period_text='2026. 3. 3. 09:00 ~ 2026. 3. 23. 18:00')
        data = _normalize_program(row)
        self.assertIsNone(data['start_date'])
        self.assertEqual(data['application_period_text'], row['application_period_text'])
        self.assertEqual(data['application_period_basis'], 'application_text')
        self.assertEqual(data['period_status'], 'announced')

    def test_business_period_never_becomes_an_application_period(self):
        row = program_row()
        row.update(application_start=None, application_end=None, period_status='unknown')
        data = _normalize_program(row, rules=[dict(condition_type='program_period',
            operator='between', condition_value=['2026-01-01', '2026-12-31'],
            logic_group={}, review_status='needs_review', evidence_id=None)])
        self.assertIsNone(data['application_period_text'])
        self.assertIsNone(data['application_period_basis'])
        self.assertIsNone(data['start_date'])

    def test_preserves_native_types_and_unreviewed_semantics(self):
        row = program_row()
        tags = [dict(category_code='income_support', category_name='소득·생활 안정',
                     assignment_method='keyword_rule', review_status='needs_review',
                     rule_version='category-rules-v1.2', evidence_id=EVIDENCE)]
        regions = [dict(scope_type='specific', region_id=REGION, region_code='1168000000',
                        region_name='서울특별시 강남구', code_system='MOIS_ADMIN',
                        scope_purpose='administering_jurisdiction', mapping_status='inferred',
                        original_region_text='강남구')]
        condition = {'expressions': [{'min': 19, 'max': 44}, {'text': '3개월 미만'}]}
        rules = [dict(condition_type='age', operator='text', condition_value=condition,
                      logic_group={'combine': 'unspecified'}, review_status='needs_review',
                      evidence_id=EVIDENCE)]
        data = _normalize_program(row, tags, regions, rules)
        self.assertEqual(data['version_id'], str(VERSION))
        self.assertEqual(data['start_date'], '2026-01-01')
        self.assertIsNone(data['max_age'])
        self.assertEqual(data['gender_scope'], 'unknown')
        self.assertFalse(data['source_verified'])
        self.assertEqual(data['content_review'], 'unknown')
        self.assertEqual(data['verification_level'], 'detail_structured')
        self.assertEqual(data['application_status'], 'unknown')
        self.assertEqual(data['i5_domains'], [])
        self.assertEqual(data['source_categories'][0]['source_id'], 'internal')
        self.assertEqual(data['source_categories'][0]['review_status'], 'needs_review')
        self.assertEqual(data['regions'][0]['scope_purpose'], 'administering_jurisdiction')
        self.assertEqual(data['regions'][0]['mapping_status'], 'inferred')
        self.assertEqual(data['conditions'][0]['value'], condition)
        self.assertEqual(data['conditions'][0]['logic_group'], {'combine': 'unspecified'})
        self.assertNotIn('storage_path', data)
        self.assertNotIn('must-never-leak', json.dumps(data, allow_nan=False))

    def test_native_jsonb_scalar_strings_keep_their_type_and_value(self):
        for value in ('대학생', '123', 'null', '{"codes":["JA0201"]}', '', None, 123):
            with self.subTest(value=value):
                data = _normalize_program(program_row(), rules=[dict(condition_type='target',
                    operator='text', condition_value=value, logic_group={},
                    review_status='needs_review', evidence_id=None)])
                self.assertEqual(data['conditions'][0]['value'], value)
                self.assertEqual(type(data['conditions'][0]['value']), type(value))

    def test_portal_link_is_not_labeled_an_application_form(self):
        link = dict(evidence_id=EVIDENCE, source_quote='https://www.gov.kr/')
        data = _normalize_program(program_row(), links=[link])
        self.assertEqual(data['source_url_type'], 'official_site')
        self.assertEqual(data['source_evidence_id'], str(EVIDENCE))
        link['source_quote'] = 'https://www.gov.kr/portal/rcvfvrSvc/dtlEx/123'
        self.assertEqual(_normalize_program(program_row(), links=[link])['source_url_type'], 'official_source')

    def test_unsafe_or_credential_links_are_rejected(self):
        for value in ('javascript:alert(1)', 'https://user:secret@gov.kr/',
                      'https://gov.kr/?serviceKey=secret', 'https://gov.kr/?API_KEY=secret',
                      'https://gov.kr/\nsecret', 'not a url', None):
            with self.subTest(value=value):
                self.assertIsNone(_official_url(value))
        self.assertEqual(_official_url('https://www.bokjiro.go.kr/?id=123'),
                         'https://www.bokjiro.go.kr/?id=123')


class QueryDecisionTests(unittest.TestCase):
    def test_user_search_and_category_are_bound_as_literal_values(self):
        query = "' OR 1=1 -- %_"
        category = "income_support'; DROP TABLE programs; --"
        sql, params = _filters(q=query, category_id=category, domain=None,
            allowed_regions=[], region_code=None, status=None)
        self.assertNotIn(query, sql)
        self.assertNotIn(category, sql)
        self.assertEqual(params, [query, query, category])
        self.assertIn('strpos', sql)
        self.assertNotIn('ILIKE', sql)

    def test_domain_filter_never_invents_internal_category_mapping(self):
        sql, params = _filters(q='', category_id=None, domain='E',
            allowed_regions=[], region_code=None, status=None)
        self.assertEqual(sql, ' WHERE FALSE')
        self.assertEqual(params, [])

    def test_region_filter_uses_only_explicit_ids_and_jurisdiction(self):
        sql, params = _filters(q='', category_id=None, domain=None,
            allowed_regions=[str(REGION)], region_code='11680', status='unknown')
        self.assertIn("scope_purpose='administering_jurisdiction'", sql)
        self.assertIn("scope_type='nationwide'", sql)
        self.assertEqual(params, [[str(REGION)], 'unknown'])
        self.assertNotIn('11680', sql)
        self.assertNotIn('LIKE', sql)

    def test_invalid_publication_error_does_not_echo_input(self):
        invalid = 'postgresql://user:secret@host/db'
        with self.assertRaises(WelfareDatabaseError) as caught:
            PostgresWelfareDB(invalid)
        self.assertEqual(caught.exception.code, 'invalid_publication')
        self.assertNotIn('secret', str(caught.exception))


class StoreContractTests(unittest.TestCase):
    def connection(self, db):
        context = MagicMock()
        context.__enter__.return_value = db
        return patch('donghaeng_agent.welfare_postgres.postgres_connection', return_value=context)

    def test_i5_search_holds_with_real_coverage_without_loading_programs(self):
        db = Mock()
        db.execute.side_effect = [cursor(rows=publication_rows()), cursor(row=stats_row())]
        with self.connection(db):
            data = PostgresWelfareDB().search(i5_domains=['E', 'U'])
        self.assertEqual(data['status'], 'mapping_required')
        self.assertEqual(data['programs'], [])
        self.assertEqual(data['coverage'], {'total': 16244, 'matched': 0, 'unclassified': 212})
        self.assertEqual(data['publication_ids'], [str(PUBLICATION)])
        self.assertEqual(data['publication_selection'], 'active_per_source')
        self.assertEqual(data['publications'][0]['published_at'], '2026-10-25T06:26:00+00:00')
        self.assertEqual(db.execute.call_count, 2)
        self.assertIn('active_publications', db.execute.call_args_list[0].args[0])

    def test_missing_publication_is_not_reported_as_empty_catalog(self):
        db = Mock()
        db.execute.return_value = cursor(rows=[])
        with self.connection(db), self.assertRaises(WelfareDatabaseError) as caught:
            PostgresWelfareDB().catalog(as_of=date(2026, 10, 25))
        self.assertEqual(caught.exception.code, 'publication_unavailable')

    def test_empty_page_keeps_total_and_facets_without_child_reads(self):
        pubs = publication_rows() + [dict(publication_id=UUID('00000000-0000-4000-8000-000000000006'),
            source_id='local', publication_status='published', source_name='지자체')]
        db = Mock()
        db.execute.side_effect = [cursor(rows=pubs), cursor(row={'rules':False}), cursor(row=stats_row()), cursor(rows=[]), cursor(row={'matched': 0}),
            cursor(rows=[]), cursor(rows=[{'category_id': 'income_support', 'label': '소득·생활 안정', 'count': 6130}]),
            cursor(rows=[])]
        with self.connection(db):
            data = PostgresWelfareDB().catalog(as_of=date(2026, 10, 25), domain='E', offset=100, limit=10)
        self.assertIsNone(data['publication_id'])
        self.assertEqual(len(data['publication_ids']), 2)
        self.assertEqual((data['total'], data['matched'], data['programs']), (16244, 0, []))
        self.assertEqual(data['categories'][0]['count'], 6130)
        self.assertIsNone(data['next_offset'])
        self.assertEqual(data['mapping_status'], 'unconfigured')
        self.assertEqual(db.execute.call_count, 8)
        self.assertEqual(db.execute.call_args_list[5].args[1][-2:], [10, 100])
        self.assertIn('WHERE FALSE', db.execute.call_args_list[5].args[0])
        self.assertNotIn('WHERE FALSE', db.execute.call_args_list[6].args[0])

    def test_page_children_are_batched_only_for_selected_version_ids(self):
        db = Mock()
        link = dict(program_version_id=VERSION, evidence_id=EVIDENCE,
                    source_quote='https://www.gov.kr/portal/rcvfvrSvc/dtlEx/123')
        document = dict(program_version_id=VERSION,
                        source_retrieved_at=datetime(2026, 10, 24, 15, 13, tzinfo=timezone.utc))
        db.execute.side_effect = [cursor(rows=publication_rows()), cursor(row={'rules':False}), cursor(row=stats_row()), cursor(rows=[]),
            cursor(row={'matched': 4}), cursor(rows=[program_row()]), cursor(rows=[]), cursor(rows=[]),
            cursor(rows=[]), cursor(rows=[]), cursor(rows=[]), cursor(rows=[link]), cursor(rows=[document])]
        with self.connection(db):
            data = PostgresWelfareDB(str(PUBLICATION)).catalog(as_of=date(2026, 10, 25), q='공개', limit=2)
        self.assertEqual(data['publication_selection'], 'explicit')
        self.assertEqual(data['next_offset'], 2)
        self.assertEqual(len(data['programs']), 1)
        self.assertEqual(data['programs'][0]['source_url'], link['source_quote'])
        self.assertEqual(data['programs'][0]['source_retrieved_at'], '2026-10-24T15:13:00+00:00')
        self.assertEqual(data['date_basis'], 'application')
        self.assertEqual(data['region_basis'], 'administering_jurisdiction')
        self.assertEqual(data['classification_basis'], 'internal_tags')
        self.assertEqual(db.execute.call_args_list[5].args[1][-2:], [2, 0])
        for call in db.execute.call_args_list[8:]:
            self.assertEqual(call.args[1], ([str(VERSION)],))
        self.assertEqual(db.execute.call_count, 13)
        json.dumps(data, allow_nan=False)

    def test_child_queries_bind_evidence_to_same_version_and_document_to_same_source(self):
        db = Mock()
        db.execute.return_value = cursor(rows=[])
        PostgresWelfareDB._children(db, [str(VERSION)])
        self.assertEqual(db.execute.call_count, 5)
        tag_sql = db.execute.call_args_list[0].args[0]
        rule_sql = db.execute.call_args_list[2].args[0]
        document_sql = db.execute.call_args_list[4].args[0]
        self.assertIn('e.evidence_id=t.evidence_id', tag_sql)
        self.assertIn('e.program_version_id=t.program_version_id', tag_sql)
        self.assertIn("e.evidence_kind='category_basis'", tag_sql)
        self.assertIn('e.evidence_id=r.evidence_id', rule_sql)
        self.assertIn('e.program_version_id=r.program_version_id', rule_sql)
        self.assertIn("e.evidence_kind='eligibility_basis'", rule_sql)
        self.assertIn('d.document_id=vd.document_id AND d.source_id=p.source_id', document_sql)
        self.assertIn('max(d.retrieved_at)', document_sql)
        for call in db.execute.call_args_list:
            self.assertEqual(call.args[1], ([str(VERSION)],))
            for private in ('storage_path', 'endpoint', 'raw_sha256', 'locator'):
                self.assertNotIn(private, call.args[0])


if __name__ == '__main__':
    unittest.main()
