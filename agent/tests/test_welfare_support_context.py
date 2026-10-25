import copy
import unittest

from donghaeng_agent.welfare_support_context import support_context


class SupportContextTests(unittest.TestCase):
    def setUp(self):
        self.behavior = {'key': {'region_code': '11680', 'month': '202510',
            'sex_code': 'MAN', 'age_code': '20G'}, 'region_name': '강남구',
            'historical_reference_id': 'C3:fixture'}
        self.assessment = {'sources': {'card_a': {'threshold_met': True}}}
        self.history = {'record_id': 'C3:fixture', 'region': '강남구', 'month': '2025-10',
            'sex': '남성', 'age': '20', 'view': 'age_sex', 'calendar_type': 'all',
            'dataset': 'data2', 'count': {'decline_flag': False}}
        self.population = {'region_code': '11680', 'month': '202510',
                           'one_person_household_share': 0.5}

    def test_background_does_not_add_votes_or_select_categories(self):
        result = support_context(self.behavior, self.assessment,
            historical_reference=self.history, population=self.population)
        self.assertEqual(result['historical_card']['consistency'], 'monthly_only')
        self.assertFalse(result['historical_card']['independent_evidence'])
        self.assertFalse(result['population']['changes_category'])
        self.assertFalse(result['population']['automatic_10_to_6_group_join'])
        self.assertFalse(result['risk_score_combined'])

    def test_missing_historical_observation_is_unknown(self):
        result = support_context(self.behavior, self.assessment)
        self.assertEqual(result['historical_card']['consistency'], 'unavailable')
        self.assertEqual(result['population']['status'], 'unavailable')

    def test_historical_evidence_must_match_every_dimension(self):
        for key, value in [('record_id', 'C3:other'), ('month', '2025-11'),
                           ('age', '30'), ('sex', '여성'), ('region', '춘천시'),
                           ('calendar_type', 'weekend'), ('view', 'age'),
                           ('industry', '음식'), ('dataset', 'other')]:
            with self.subTest(key=key):
                row = {**self.history, key: value}
                with self.assertRaises(ValueError):
                    support_context(self.behavior, self.assessment, historical_reference=row)

    def test_population_remains_region_context_with_exact_month(self):
        before = copy.deepcopy(self.population)
        support_context(self.behavior, self.assessment, population=self.population)
        self.assertEqual(self.population, before)
        with self.assertRaises(ValueError):
            support_context(self.behavior, self.assessment,
                            population={**self.population, 'month': '202511'})

    def test_confirmed_need_keeps_population_context_without_activity(self):
        result=support_context(None,self.assessment,population=self.population,
            scope={'region_code':'11680','month':'202510','sex_code':'MAN','age_code':'20G'})
        self.assertEqual(result['population']['status'],'available')
        self.assertEqual(result['historical_card']['status'],'unavailable')
