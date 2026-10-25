import unittest
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.main import create_app
from app.config import Settings


class WelfareAPITests(unittest.TestCase):
    def setUp(self):
        self.client=TestClient(create_app(Settings()))
        self.params=dict(region_code='11680',month='202508',sex_code='MAN',age_code='20G',as_of='2026-10-25')

    def test_rejects_unknown_filters(self):
        self.assertEqual(self.client.get('/api/model/welfare',params={**self.params,'db_path':'other'}).status_code,422)

    def test_rejects_invalid_region(self):
        self.assertEqual(self.client.get('/api/model/welfare',params={**self.params,'region_code':'1168010100'}).status_code,422)

    def test_only_explicit_supported_need_categories_reach_the_graph(self):
        from donghaeng_agent.welfare_graph import build_welfare_graph
        received=[]
        graph=build_welfare_graph(lambda **kw: received.append(kw) or {'operational_alert_allowed':False})
        with patch('donghaeng_agent.welfare_graph.build_welfare_graph',return_value=graph), \
                patch('app.welfare_api.FinalModel.bundle_path',return_value=Path('/unused')):
            response=self.client.get('/api/model/welfare',params={**self.params,'confirmed_needs':'housing'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(received[0]['confirmed_needs'],['housing'])
        self.assertEqual(response.json()['request'],{
            **{key:self.params[key] for key in ('region_code','month','sex_code','age_code')},
            'support_as_of':self.params['as_of'],'confirmed_needs':['housing']})
        self.assertEqual(self.client.get('/api/model/welfare',params={**self.params,
            'confirmed_needs':'unknown_category'}).status_code,422)

    def test_all_nine_explicit_need_fields_are_allowed_and_duplicate_fields_are_collapsed(self):
        from donghaeng_agent.welfare_graph import build_welfare_graph
        categories=['social_connection','isolation_outreach','income_support','daily_living',
                    'employment','mental_health','health_care','housing','family_support']
        received=[]
        graph=build_welfare_graph(lambda **kw: received.append(kw) or {'operational_alert_allowed':False})
        with patch('donghaeng_agent.welfare_graph.build_welfare_graph',return_value=graph), \
                patch('app.welfare_api.FinalModel.bundle_path',return_value=Path('/unused')):
            response=self.client.get('/api/model/welfare',params={**self.params,'confirmed_needs':categories})
            duplicate=self.client.get('/api/model/welfare',params={**self.params,
                'confirmed_needs':['housing','housing','daily_living']})
        self.assertEqual(response.status_code,200)
        self.assertEqual(received[0]['confirmed_needs'],categories)
        self.assertEqual(response.json()['request']['confirmed_needs'],categories)
        self.assertEqual(duplicate.status_code,200)
        self.assertEqual(received[1]['confirmed_needs'],['housing','daily_living'])
        self.assertEqual(self.client.get('/api/model/welfare',params={**self.params,
            'confirmed_needs':categories+['housing']}).status_code,422)

    def test_graph_response_and_private_paths(self):
        from donghaeng_agent.welfare_graph import build_welfare_graph
        graph=build_welfare_graph(lambda **kw: {'operational_alert_allowed':False,
            'provenance':{'path':'private'},'activity':{'provenance':{'inputs':'private'},'record_id':'real'},'status':'completed'})
        with patch('donghaeng_agent.welfare_graph.build_welfare_graph',return_value=graph), \
                patch('app.welfare_api.FinalModel.bundle_path', return_value=Path('/unused/test-bundle')):
            response=self.client.get('/api/model/welfare',params=self.params)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['execution_graph'],'welfare-research-v1')
        self.assertNotIn('provenance',response.json())
        self.assertNotIn('provenance',response.json()['activity'])

    def test_missing_db_is_not_empty_success(self):
        with patch.dict('os.environ',{'WELFARE_DB_PATH':'missing-welfare-file.sqlite'}):
            response=self.client.get('/api/model/welfare',params=self.params)
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.json()['detail']['code'],'welfare_unavailable')


class WelfareCatalogAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'catalog.sqlite'
        self.client = TestClient(create_app(Settings()))
        from donghaeng_agent.welfare_erd import create_demo
        create_demo(self.path)
        self.env = patch.dict('os.environ', {
            'WELFARE_DB_PATH': str(self.path), 'WELFARE_PUBLICATION_ID': 'erd-demo-v2',
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def get(self, **params):
        return self.client.get('/api/model/welfare/catalog', params={'as_of': '2026-10-25', **params})

    def test_lists_all_published_members_without_recommendation_gate(self):
        before = self.path.read_bytes()
        response = self.get()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['schema_version'], 'welfare_catalog_v1')
        self.assertTrue(body['is_synthetic'])
        self.assertEqual((body['total'], body['matched']), (5, 5))
        self.assertEqual({p['program_id'] for p in body['programs']},
                         {'social', 'work', 'expired', 'unknown', 'unclassified'})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn(str(self.path), response.text)
        self.assertNotIn('recommendation_reasons', response.text)

    def test_returns_stored_conditions_categories_and_named_regions(self):
        body = self.get(q='social').json()
        self.assertEqual(body['matched'], 1)
        program = body['programs'][0]
        self.assertEqual(program['source']['source_id'], 'demo-api')
        self.assertEqual(program['source_categories'][0]['category_id'], 'demo:life')
        self.assertEqual(program['regions'], [{'scope_type': 'specific', 'region_id': 'demo-gangnam',
                                               'code': '11680', 'name': '강남구'}])
        self.assertEqual(next(rule['value'] for rule in program['conditions'] if rule['kind'] == 'age'), [19, 39])
        self.assertIsNone(next(rule['value'] for rule in program['conditions'] if rule['kind'] == 'personal_eligibility'))
        self.assertEqual({item['code'] for item in program['i5_domains']}, {'P', 'C', 'U'})

    def test_facets_cover_entire_publication_while_results_are_filtered(self):
        body = self.get(q='social').json()
        self.assertEqual(body['total'], 5)
        self.assertEqual({c['category_id']: c['count'] for c in body['categories']},
                         {'demo:job': 1, 'demo:life': 3, 'demo:participation': 1})
        self.assertEqual({item['code']: item['count'] for item in body['statuses']},
                         {'ongoing': 2, 'ended': 1, 'unknown': 2, 'upcoming': 0})

    def test_combines_search_category_domain_and_date_status(self):
        body = self.get(q='집단', category_id='demo:life', domain='P', status='ongoing').json()
        self.assertEqual([p['program_id'] for p in body['programs']], ['social'])
        self.assertEqual(self.get(q='생활지원').json()['matched'], 3)
        self.assertEqual(self.get(q="' OR 1=1 --").json()['matched'], 0)

    def test_region_filter_includes_nationwide_and_excludes_unknown_scope(self):
        self.assertEqual({p['program_id'] for p in self.get(region_code='11680').json()['programs']},
                         {'social', 'work', 'expired'})
        self.assertEqual({p['program_id'] for p in self.get(region_code='51110').json()['programs']},
                         {'work', 'expired'})

    def test_region_filter_follows_stored_parent_links(self):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO administrative_regions VALUES ('seoul','KR_SIDO','11','서울특별시',NULL)")
            db.execute("UPDATE administrative_regions SET parent_id='seoul' WHERE region_id='demo-gangnam'")
            db.execute("UPDATE program_regions SET region_id='seoul' WHERE version_id='social:v1'")
        body = self.get(region_code='11680').json()
        self.assertIn('social', {p['program_id'] for p in body['programs']})
        self.assertNotIn('social', {p['program_id'] for p in self.get(region_code='11710').json()['programs']})

    def add_dong_jurisdictions(self):
        with sqlite3.connect(self.path) as db:
            db.executemany('INSERT INTO administrative_regions VALUES (?,?,?,?,?)', [
                ('seoul', 'KR_SIDO', '11', '서울특별시', None),
                ('songpa', 'KR_SGG', '11710', '송파구', 'seoul'),
                ('sinsa', 'KR_DONG', '1168051000', '신사동', 'demo-gangnam'),
                ('nonhyeon', 'KR_DONG', '1168052100', '논현1동', 'demo-gangnam'),
                ('other', 'KR_DONG', '1171051000', '풍납1동', 'songpa'),
            ])
            db.execute("UPDATE administrative_regions SET parent_id='seoul' WHERE region_id='demo-gangnam'")
            db.execute("UPDATE program_regions SET scope_type='specific',region_id='sinsa' WHERE version_id='unknown:v1'")
            db.execute("UPDATE program_regions SET scope_type='specific',region_id='nonhyeon' WHERE version_id='unclassified:v1'")

    def test_explicit_district_scope_orders_district_dongs_then_national_before_paging(self):
        self.add_dong_jurisdictions()
        params = dict(region_level='sgg', region_code='11680', limit=1)
        pages, offset = [], 0
        while True:
            body = self.get(**params, offset=offset).json()
            pages.extend(body['programs'])
            if body['next_offset'] is None:
                break
            offset = body['next_offset']
        self.assertEqual([row['region_match_rank'] for row in pages], [0, 1, 1, 2, 2])
        self.assertEqual(pages[0]['program_id'], 'social')
        self.assertEqual(len({row['version_id'] for row in pages}), 5)
        self.assertEqual({row['program_id'] for row in pages[1:3]}, {'unknown', 'unclassified'})

    def test_explicit_dong_scope_excludes_sibling_and_unrelated_dongs(self):
        self.add_dong_jurisdictions()
        body = self.get(region_level='administrative_dong', region_code='1168051000').json()
        self.assertEqual([row['region_match_rank'] for row in body['programs']], [0, 1, 2, 2])
        self.assertEqual([row['program_id'] for row in body['programs'][:2]], ['unknown', 'social'])
        self.assertNotIn('unclassified', {row['program_id'] for row in body['programs']})
        other = self.get(region_level='administrative_dong', region_code='1171051000').json()
        self.assertEqual({row['program_id'] for row in other['programs']}, {'work', 'expired'})

    def test_explicit_national_means_nationwide_only_and_deduplicates_multiscope(self):
        self.add_dong_jurisdictions()
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO program_regions VALUES ('social:v1','nationwide',NULL)")
            db.execute("INSERT INTO program_regions VALUES ('social:v1','specific','sinsa')")
        body = self.get(region_level='national').json()
        self.assertEqual({row['program_id'] for row in body['programs']}, {'social', 'work', 'expired'})
        self.assertTrue(all(row['region_match_rank'] == 2 for row in body['programs']))
        district = self.get(region_level='sgg', region_code='11680').json()
        self.assertEqual(len([row for row in district['programs'] if row['program_id'] == 'social']), 1)
        self.assertEqual(district['programs'][0]['region_match_rank'], 0)

    def test_full_region_facets_are_available_before_filtering(self):
        self.add_dong_jurisdictions()
        body = self.get().json()
        dong = next(row for row in body['available_regions'] if row['code'] == '1168051000')
        self.assertEqual((dong['level'], dong['parent_code'], dong['province_code'], dong['province_name']),
                         ('administrative_dong', '11680', '11', '서울특별시'))

    def test_explicit_scope_rejects_inconsistent_codes(self):
        for params in ({'region_level': 'national', 'region_code': '11680'},
                       {'region_level': 'sgg'}, {'region_level': 'administrative_dong'},
                       {'region_level': 'sgg', 'region_code': '1168051000'},
                       {'region_level': 'administrative_dong', 'region_code': '11680'},
                       {'region_level': 'province', 'region_code': '11'}):
            with self.subTest(params=params):
                self.assertEqual(self.get(**params).status_code, 422)

    def test_date_status_does_not_rewrite_stored_recruitment_status(self):
        program = self.get(status='ended').json()['programs'][0]
        self.assertEqual(program['program_id'], 'expired')
        self.assertEqual(program['period_status'], 'ended')
        self.assertEqual(program['application_status'], 'open')
        self.assertEqual(self.get(as_of='2025-12-01', status='upcoming').json()['matched'], 5)

    def test_pagination_is_stable_and_empty_page_is_success(self):
        first = self.get(limit=2).json()
        second = self.get(limit=2, offset=first['next_offset']).json()
        third = self.get(limit=2, offset=second['next_offset']).json()
        self.assertIsNone(third['next_offset'])
        rows = first['programs'] + second['programs'] + third['programs']
        self.assertEqual(len({p['version_id'] for p in rows}), 5)
        body = self.get(offset=100).json()
        self.assertEqual(body['programs'], [])
        self.assertEqual(body['matched'], 5)
        self.assertEqual(self.get(q='존재하지 않는 사업').json()['programs'], [])

    def test_only_selected_published_corpus_is_read(self):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO program_versions SELECT 'unpublished:v1',program_id,'UNPUBLISHED',support_content,source_verified,content_review,start_date,end_date,application_status,source_url FROM program_versions LIMIT 1")
        self.assertEqual(self.get().json()['total'], 5)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE publications SET status='draft'")
        self.assertEqual(self.get().status_code, 503)

    def test_unknown_or_missing_publication_and_missing_file_are_errors(self):
        with patch.dict('os.environ', {'WELFARE_PUBLICATION_ID': 'missing'}):
            self.assertEqual(self.get().status_code, 503)
        missing = self.path.with_name('missing.sqlite')
        with patch.dict('os.environ', {'WELFARE_DB_PATH': str(missing)}):
            response = self.get()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail']['code'], 'welfare_catalog_unavailable')
        self.assertNotIn(str(missing), response.text)
        self.assertFalse(missing.exists())

    def test_empty_publication_is_distinct_from_unavailable(self):
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM publication_members')
        body = self.get().json()
        self.assertEqual((body['total'], body['matched']), (0, 0))
        self.assertEqual(body['programs'], [])

    def test_corrupt_corpus_does_not_return_partial_success(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE eligibility_rules SET value_json='bad json' WHERE version_id='social:v1'")
        response = self.get()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail']['code'], 'welfare_catalog_unavailable')

    def test_validates_inputs_and_keeps_local_access_boundary(self):
        for params in ({'limit': 0}, {'limit': 501}, {'offset': -1}, {'domain': 'X'},
                       {'status': 'approved'}, {'region_code': '1168010100'}, {'db_path': 'other'},
                       {'q': 'a' * 201}):
            with self.subTest(params=params):
                self.assertEqual(self.get(**params).status_code, 422)
        response = self.client.get('/api/model/welfare/catalog', headers={'Host': 'external.example'})
        self.assertEqual(response.status_code, 403)

    def test_synthetic_corpus_requires_explicit_opt_in_for_reader(self):
        from app.welfare_api import _read_catalog
        from datetime import date
        with self.assertRaisesRegex(ValueError, 'synthetic'):
            _read_catalog(self.path, 'erd-demo-v2', False, date(2026, 10, 25))
