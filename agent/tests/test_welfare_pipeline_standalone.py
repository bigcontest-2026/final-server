import unittest
import copy
import tempfile
from pathlib import Path
from unittest.mock import patch
from donghaeng_agent.welfare_pipeline import build_intent, run
from donghaeng_agent.welfare_erd import create_demo
from donghaeng_agent.welfare_support_mapping import CATEGORY_LABELS


class IntentTests(unittest.TestCase):
    def setUp(self):
        self.i5={'region_code':'11680','month':'202508','spatial_unit':'sgg',
                 'record_id':'i5:fixture','domain_scores':dict(E=20,P=30,C=40,U=50,O=10)}
        self.activity={'key':{'region_code':'11680','month':'202508','sex_code':'MAN','age_code':'20G'},'spatial_unit':'sgg',
            'record_id':'activity:fixture','comparison_status':'computed',
            'rule':{'candidate':True,'two_month_streak':None},'robust':{'candidate':False},
            'sources':{'skt':{'calculation_status':'computed','change_percent':-5},
                       'card_a':{'calculation_status':'computed','change_percent':-6}}}

    def intent(self):
        return build_intent(self.i5,self.activity,research_context=True)

    def run_pipeline(self, **changes):
        args=dict(i5_bundle='/tmp/i5',activity_bundle='/tmp/activity',db_path='/tmp/unused',
                  publication_id='fixture',region_code='11680',month='202508',
                  sex_code='MAN',age_code='20G',as_of='2026-10-25',research_context=True)
        return run(**{**args, **changes})

    def test_explicit_research_mode_required(self):
        self.assertEqual(build_intent(self.i5,self.activity)['status'],'held')

    def test_joint_signal_has_real_score_references(self):
        result=self.intent()
        self.assertEqual(result['route'],'common_factor_review')
        self.assertEqual(result['i5_domains'],['P','C','U','E'])
        self.assertEqual(result['evidence'][0]['score'],30)

    def test_missing_evidence_holds(self):
        self.assertEqual(build_intent(None,self.activity,research_context=True)['status'],'held')

    def test_mismatched_month_rejected(self):
        self.i5['month']='202507'
        with self.assertRaises(ValueError): self.intent()

    def test_invalid_scores_not_selected(self):
        self.i5['domain_scores']={'P':float('nan'),'C':None,'U':101,'E':True}
        self.assertEqual(self.intent()['status'],'held')

    def test_no_signal_does_not_search(self):
        self.activity['rule']['candidate']=False
        for source in self.activity['sources'].values(): source['change_percent']=1
        self.assertEqual(self.intent()['status'],'no_signal')

    def test_robust_route_preserved(self):
        self.activity['robust']['candidate']=True
        self.assertEqual(self.intent()['route'],'focused_investigation')

    def test_uncomputed_comparison_holds(self):
        self.activity['comparison_status']='not_comparable'
        self.assertEqual(self.intent()['status'],'held')

    def test_small_declines_do_not_search_despite_model_flags(self):
        self.activity['sources']['skt']['change_percent']=-4
        self.activity['sources']['card_a']['change_percent']=-1
        self.activity['robust']['candidate']=True
        result=self.intent()
        self.assertEqual(result['status'],'no_signal')
        self.assertEqual(result['activity_assessment']['category'],'no_threshold_decline')

    def test_one_source_robust_candidate_does_not_become_joint_route(self):
        self.activity['sources']['card_a']['change_percent']=1
        self.activity['robust']['candidate']=True
        result=self.intent()
        self.assertEqual(result['route'],'single_source_review')
        self.assertEqual(result['i5_domains'],['O','P'])

    def test_run_queries_exact_previous_month_and_preserves_remote_mapping_hold(self):
        previous=copy.deepcopy(self.activity)
        previous['key']['month']='202507'
        previous['record_id']='activity:previous'
        activity_calls=[]
        def activity_query(bundle,**kwargs):
            activity_calls.append(kwargs)
            row=self.activity if kwargs['month']=='202508' else previous
            return {'total_matched':1,'records':[{'behavior':row}],
                    'availability':{'status':'available'},'run_id':'fixture'}
        with patch('final_model.src.survey_area_index.query',return_value={
                'total_matched':1,'records':[self.i5],'availability':{'status':'available'}}), \
             patch('final_model.src.query.query',side_effect=activity_query), \
             patch('donghaeng_agent.welfare_pipeline.remote_configured',return_value=True), \
             patch('donghaeng_agent.welfare_support_context.load_support_context',return_value={}), \
             patch('donghaeng_agent.welfare_postgres.PostgresWelfareDB') as database:
            database.return_value.search_support.return_value={
                'status':'mapping_required','coverage':{},'publication_id':'fixture'}
            result=run(i5_bundle='/tmp/i5',activity_bundle='/tmp/activity',db_path='/tmp/unused',
                       publication_id='fixture',region_code='11680',month='202508',
                       sex_code='MAN',age_code='20G',as_of='2026-10-25',research_context=True)
        self.assertEqual([call['month'] for call in activity_calls],['202508','202507'])
        self.assertTrue(all(call['region_code']=='11680' and call['sex_code']=='MAN'
                            and call['age_code']=='20G' for call in activity_calls))
        self.assertEqual(result['mapping']['activity_assessment']['category'],'priority_review')
        self.assertEqual(result['status'],'held')
        self.assertEqual(result['mapping_status'],'unconfigured')
        self.assertIs(result['operational_alert_allowed'],False)
        self.assertEqual(result['recommendations'],[])

    def test_run_below_threshold_returns_without_searching_welfare(self):
        for source in self.activity['sources'].values(): source['change_percent']=-1
        response={'total_matched':1,'records':[{'behavior':self.activity}],
                  'availability':{'status':'available'},'run_id':'fixture'}
        with patch('final_model.src.survey_area_index.query',return_value={
                'total_matched':1,'records':[self.i5],'availability':{'status':'available'}}), \
             patch('final_model.src.query.query',return_value=response), \
             patch('donghaeng_agent.welfare_pipeline.remote_configured') as remote:
            result=run(i5_bundle='/tmp/i5',activity_bundle='/tmp/activity',db_path='/tmp/unused',
                       publication_id='fixture',region_code='11680',month='202508',
                       sex_code='MAN',age_code='20G',as_of='2026-10-25',research_context=True)
        self.assertEqual(result['status'],'no_signal')
        self.assertEqual(result['recommendations'],[])
        self.assertEqual(result['request']['confirmed_needs'],[])
        remote.assert_not_called()

    def test_invalid_confirmed_needs_fail_before_model_or_database_queries(self):
        for needs in (['unknown'], ['housing', None], [['housing']], 'housing', {'housing': True}, None):
            with self.subTest(needs=needs), \
                 patch('final_model.src.survey_area_index.query') as survey, \
                 patch('donghaeng_agent.welfare_pipeline.remote_configured') as remote:
                with self.assertRaisesRegex(ValueError, 'confirmed_needs'):
                    self.run_pipeline(confirmed_needs=needs)
                survey.assert_not_called()
                remote.assert_not_called()

    def test_local_confirmed_needs_hold_instead_of_falling_back_to_i5_search(self):
        for decline in (-6, -1):
            activity=copy.deepcopy(self.activity)
            for source in activity['sources'].values(): source['change_percent']=decline
            response={'total_matched':1,'records':[{'behavior':activity}],
                      'availability':{'status':'available'},'run_id':'fixture'}
            needs=[*CATEGORY_LABELS, 'housing']
            with self.subTest(decline=decline), \
                 patch('final_model.src.survey_area_index.query',return_value={
                    'total_matched':1,'records':[self.i5],'availability':{'status':'available'}}), \
                 patch('final_model.src.query.query',return_value=response), \
                 patch('donghaeng_agent.welfare_pipeline.remote_configured',return_value=False), \
                 patch('donghaeng_agent.welfare_pipeline.ERDWelfareDB') as database:
                result=self.run_pipeline(confirmed_needs=needs)
            database.assert_not_called()
            self.assertEqual(result['request']['confirmed_needs'],sorted(CATEGORY_LABELS))
            self.assertEqual(result['status'],'held')
            self.assertEqual(result['mapping_status'],'confirmed_needs_unsupported')
            self.assertEqual(result['mapping']['status'],'held')
            self.assertIn('로컬 복지 자료',result['mapping']['reasons'][-1])
            self.assertEqual(result['recommendations'],[])
            self.assertEqual(result['quality_status'],'hold')
            self.assertFalse(result['operational_alert_allowed'])
            self.assertIn('사업 부재나 지원 공백을 확정하지 않습니다',result['limitations'][-1])

    def test_confirmed_needs_are_echoed_when_research_mode_is_not_enabled(self):
        response={'total_matched':1,'records':[{'behavior':self.activity}],
                  'availability':{'status':'available'},'run_id':'fixture'}
        with patch('final_model.src.survey_area_index.query',return_value={
                'total_matched':1,'records':[self.i5],'availability':{'status':'available'}}), \
             patch('final_model.src.query.query',return_value=response), \
             patch('donghaeng_agent.welfare_pipeline.remote_configured') as remote:
            result=self.run_pipeline(confirmed_needs=['housing','employment','housing'],research_context=False)
        self.assertEqual(result['status'],'held')
        self.assertEqual(result['request']['confirmed_needs'],['employment','housing'])
        remote.assert_not_called()

    def test_remote_confirmed_needs_searches_only_confirmed_topics_with_or_without_decline(self):
        for decline in (-6, -1):
            activity=copy.deepcopy(self.activity)
            for source in activity['sources'].values(): source['change_percent']=decline
            response={'total_matched':1,'records':[{'behavior':activity}],
                      'availability':{'status':'available'},'run_id':'fixture'}
            with self.subTest(decline=decline), \
                 patch('final_model.src.survey_area_index.query',return_value={
                    'total_matched':1,'records':[self.i5],'availability':{'status':'available'}}), \
                 patch('final_model.src.query.query',return_value=response), \
                 patch('donghaeng_agent.welfare_pipeline.remote_configured',return_value=True), \
                 patch('donghaeng_agent.welfare_support_context.load_support_context',return_value={}), \
                 patch('donghaeng_agent.welfare_postgres.PostgresWelfareDB') as database:
                database.return_value.search_support.return_value={
                    'status':'ok','programs':[],'next_offset':None,'coverage':{}}
                result=self.run_pipeline(confirmed_needs=['housing','employment','housing'])
            expected=['employment','housing']
            self.assertEqual(database.return_value.search_support.call_args.kwargs['category_ids'],expected)
            self.assertEqual(result['request']['confirmed_needs'],expected)
            self.assertEqual(result['mapping']['category_mapping']['selected_categories'],expected)
            self.assertEqual(result['mapping']['category_mapping']['selection_basis'],'confirmed_needs_only')
            self.assertEqual(result['mapping']['route'],'confirmed_support_need')
            self.assertEqual(result['status'],'completed')
            self.assertEqual(result['recommendations'],[])
            self.assertEqual(result['quality_status'],'hold')
            self.assertFalse(result['operational_alert_allowed'])

    def test_local_empty_confirmed_needs_preserves_existing_erd_search(self):
        response={'total_matched':1,'records':[{'behavior':self.activity}],
                  'availability':{'status':'available'},'run_id':'fixture'}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'welfare.sqlite'
            create_demo(path)
            with patch('final_model.src.survey_area_index.query',return_value={
                    'total_matched':1,'records':[self.i5],'availability':{'status':'available'}}), \
                 patch('final_model.src.query.query',return_value=response), \
                 patch('donghaeng_agent.welfare_pipeline.remote_configured',return_value=False):
                result=self.run_pipeline(db_path=path,publication_id='erd-demo-v2',allow_synthetic=True)
        self.assertEqual(result['request']['confirmed_needs'],[])
        self.assertEqual(result['status'],'completed')
        self.assertEqual({program['program_id'] for program in result['recommendations']},{'social','work'})
        self.assertTrue(result['welfare_data_synthetic'])
        self.assertEqual(result['quality_status'],'hold')
        self.assertFalse(result['operational_alert_allowed'])

    def test_real_catalog_candidates_keep_service_and_observation_evidence_across_pages(self):
        response={'total_matched':1,'records':[{'behavior':self.activity}],
                  'availability':{'status':'available'},'run_id':'fixture'}
        program={'version_id':'published:v1','program_id':'published', 'title':'지역 교류 사업',
                 'support_content':'지역 주민의 교류 활동 프로그램을 제공합니다.',
                 'source_url':'https://www.gov.kr/', 'source_verified':False,
                 'min_age':19,'max_age':39,'age_status':'bounded',
                 'application_start':'2026-01-01','application_end':'2026-12-31',
                 'source_categories':[], 'regions':[]}
        pages=[{'status':'ok','programs':[program],'next_offset':1,'coverage':{'total':20,'matched':2}},
               {'status':'ok','programs':[{**program,'version_id':'published:v2'}],
                'next_offset':None,'coverage':{'total':20,'matched':2}}]
        with patch('final_model.src.survey_area_index.query',return_value={
                'total_matched':1,'records':[self.i5],'availability':{'status':'available'}}), \
             patch('final_model.src.query.query',return_value=response), \
             patch('donghaeng_agent.welfare_pipeline.remote_configured',return_value=True), \
             patch('donghaeng_agent.welfare_support_context.load_support_context',return_value={'risk_score_combined':False}), \
             patch('donghaeng_agent.welfare_postgres.PostgresWelfareDB') as database:
            database.return_value.search_support.side_effect=pages
            result=run(i5_bundle='/unused',activity_bundle='/unused',db_path='/unused',
                publication_id=None,region_code='11680',month='202508',sex_code='MAN',
                age_code='20G',as_of='2026-10-25',research_context=True)
        self.assertEqual(result['mapping_status'],'configured_research')
        self.assertEqual(len(result['recommendations']),2)
        self.assertFalse(result['coverage']['truncated'])
        self.assertEqual(result['coverage']['reviewed'],2)
        self.assertEqual(database.return_value.search_support.call_args_list[1].kwargs['offset'],1)
        candidate=result['recommendations'][0]
        self.assertFalse(candidate['source_verified'])
        self.assertTrue(candidate['recommendation_reasons'][0]['service_evidence']['source_quote'])
        self.assertEqual({r['domain'] for r in candidate['observation_reasons']},{'P','C','U'})
        self.assertFalse(result['operational_alert_allowed'])

    def test_publication_switch_between_pages_never_mixes_candidates(self):
        response={'total_matched':1,'records':[{'behavior':self.activity}],
                  'availability':{'status':'available'},'run_id':'fixture'}
        first={'status':'ok','programs':[],'next_offset':100,'coverage':{},
               'publication_ids':['publication-a'],'rule_version':'fixture-rule',
               'run_id':'run-a','corpus_sha256':'hash-a'}
        for changed in ({'publication_ids':['publication-b']},{'run_id':'run-b'},
                        {'corpus_sha256':'hash-b'},{'rule_version':'different-rule'}):
            with self.subTest(changed=changed), \
                 patch('final_model.src.survey_area_index.query',return_value={
                    'total_matched':1,'records':[self.i5],'availability':{'status':'available'}}), \
                 patch('final_model.src.query.query',return_value=response), \
                 patch('donghaeng_agent.welfare_pipeline.remote_configured',return_value=True), \
                 patch('donghaeng_agent.welfare_support_context.load_support_context',return_value={}), \
                 patch('donghaeng_agent.welfare_postgres.PostgresWelfareDB') as database:
                database.return_value.search_support.side_effect=[first,{**first,**changed,'next_offset':None}]
                with self.assertRaisesRegex(ValueError,'welfare_publication_changed'):
                    run(i5_bundle='/unused',activity_bundle='/unused',db_path='/unused',
                        publication_id=None,region_code='11680',month='202508',sex_code='MAN',
                        age_code='20G',as_of='2026-10-25',research_context=True)
