"""Opt-in PostgreSQL regression tests using SELECT-only, in-memory fixtures.

Set RUN_WELFARE_POSTGRES_TESTS=1 and provide connection settings explicitly to
run. This module never loads .env, reads catalog records, or writes database data.
"""
import os
import unittest
from datetime import date

from donghaeng_agent.welfare_connection import postgres_connection
from donghaeng_agent.welfare_postgres import APPLICATION_PERIOD_JOINS, PERIOD_STATUS_SQL


AS_OF = date(2026, 10, 25)
PERIOD_QUERY = f'''SELECT schedule.application_period_text,
    {PERIOD_STATUS_SQL} AS period_status
    FROM (SELECT %s::text AS application_text,
                 %s::date AS application_start,
                 %s::date AS application_end) v
    CROSS JOIN (SELECT %s::date AS as_of) day
    {APPLICATION_PERIOD_JOINS}'''


@unittest.skipUnless(os.getenv('RUN_WELFARE_POSTGRES_TESTS') == '1',
                     'set RUN_WELFARE_POSTGRES_TESTS=1 for explicit read-only PostgreSQL checks')
class ApplicationPeriodPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.connection = postgres_connection()
        cls.db = cls.connection.__enter__()
        cls.addClassCleanup(cls.connection.__exit__, None, None, None)
        read_only = cls.db.execute("SELECT current_setting('transaction_read_only') AS value").fetchone()
        if read_only['value'] != 'on':
            raise AssertionError('Regression fixtures require a read-only transaction')

    def assert_period(self, application_text, expected_text, expected_status, *, start=None, end=None):
        row = self.db.execute(PERIOD_QUERY, (application_text, start, end, AS_OF)).fetchone()
        self.assertEqual(row['application_period_text'], expected_text)
        self.assertEqual(row['period_status'], expected_status)

    def test_canonical_deadline_takes_precedence_over_period_in_method(self):
        self.assert_period(
            '신청방법: 방문 신청(신청기간: 2026. 1. 1. ~ 2026. 1. 2.)\n'
            '신청기한: 상시신청\n구비서류: 신분증',
            '상시신청', 'always',
        )

    def test_deadline_preserves_times_and_is_not_inferred_as_dates(self):
        period = '2026. 3. 3. 09:00 ~ 2026. 3. 23. 18:00'
        self.assert_period('신청방법: 온라인\r\n신청기한: ' + period + '\n구비서류: 신청서',
                           period, 'announced')

    def test_multiline_schedule_is_preserved_until_next_field(self):
        period = '1차: 2026. 3. 1. ~ 3. 15.\r\n2차: 2026. 9. 1. ~ 9. 15.\n※ 예산 소진 시 조기 마감'
        self.assert_period('신청기한: \n' + period + '\n구비서류: 신청서\n추가 서류',
                           period, 'announced')

    def test_url_colons_and_time_colons_do_not_end_period(self):
        period = 'https://www.example.go.kr/notice?id=123 에서 일정 확인, 마감일 18:00까지'
        self.assert_period('신청기한: ' + period + '\n온라인신청: https://www.example.go.kr/apply',
                           period, 'announced')

    def test_same_line_local_heading_stops_at_next_application_method(self):
        period = '2026. 3. 9. ~ 2026. 12. 11. - 예산 소진 시 당해연도 지급 불가 다음년도에 지급 예정'
        self.assert_period('신청방법: 인터넷\n· 신청기간 : ' + period + '· 신청방법 : 온라인(경기민원24)',
                           period, 'announced')

    def test_no_period_or_empty_period_is_unknown(self):
        samples = (
            None, '', '신청방법: 방문 신청, 평일 09:00부터',
            '신청기한:', '신청기한: \t ',
            '신청기한: \n구비서류: 신분증',
            '신청기한:\r\n\r\n온라인신청: https://www.example.go.kr/',
        )
        for sample in samples:
            with self.subTest(application_text=sample):
                self.assert_period(sample, None, 'unknown')

    def test_explicit_always_varies_and_no_application_statuses(self):
        for period, status in (
            ('상시신청', 'always'),
            ('접수기관 별 상이', 'varies'),
            ('별도의 신청절차가 없음', 'not_required'),
            ('개인 신청절차 없음', 'not_required'),
        ):
            with self.subTest(period=period):
                self.assert_period('신청기한: ' + period + '\n구비서류: 별도 안내', period, status)

    def test_conditional_always_text_retains_conditions_and_is_announced(self):
        period = '상시신청(예산 소진 시까지)'
        self.assert_period('신청기한: ' + period, period, 'announced')

    def test_structured_dates_override_text_status_and_include_boundaries(self):
        for start, end, expected in (
            (date(2026, 1, 1), date(2026, 10, 24), 'ended'),
            (date(2026, 10, 26), date(2026, 12, 31), 'upcoming'),
            (date(2026, 1, 1), date(2026, 12, 31), 'ongoing'),
            (AS_OF, AS_OF, 'ongoing'),
        ):
            with self.subTest(start=start, end=end):
                self.assert_period('신청기한: 상시신청', '상시신청', expected, start=start, end=end)

    def test_one_sided_dates_only_make_supported_time_claims(self):
        for start, end, expected in (
            (date(2026, 10, 26), None, 'upcoming'),
            (None, date(2026, 10, 24), 'ended'),
            (date(2026, 1, 1), None, 'announced'),
            (None, date(2026, 12, 31), 'announced'),
            (AS_OF, None, 'announced'),
            (None, AS_OF, 'announced'),
        ):
            with self.subTest(start=start, end=end):
                self.assert_period(None, None, expected, start=start, end=end)

    def test_reversed_dates_do_not_claim_active_or_closed_application(self):
        self.assert_period('신청기한: 상시신청', '상시신청', 'announced',
                           start=date(2026, 12, 1), end=date(2026, 1, 1))


if __name__ == '__main__':
    unittest.main()
