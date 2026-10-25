"""Exercise the actual statistics aggregation with isolated SQL-only fixtures.

The aggregate uses SQL shared by PostgreSQL and SQLite. Replace only the corpus
input, keeping the production aggregate intact; no remote database is needed.
Source deadline extraction has separate PostgreSQL-specific regression tests.
"""
import sqlite3
from unittest.mock import Mock

from donghaeng_agent.welfare_postgres import CORPUS, PERIOD_LABELS, PostgresWelfareDB


def aggregate_stats(programs, tags):
    db = Mock()
    PostgresWelfareDB._stats(db, [])
    query = db.execute.call_args.args[0]
    params = []

    def rows(values):
        if not values:
            return 'SELECT NULL,NULL WHERE FALSE'
        params.extend(value for row in values for value in row)
        return 'VALUES ' + ','.join('(?,?)' for _ in values)

    fixture = ('WITH corpus(program_version_id,period_status) AS (' + rows(programs) + '), '
               'program_tags(program_version_id,review_status) AS (' + rows(tags) + ')')
    with sqlite3.connect(':memory:') as connection:
        connection.row_factory = sqlite3.Row
        return dict(connection.execute(fixture + query[len(CORPUS):], params).fetchone())


def test_statistics_keep_all_buckets_and_count_classification_per_program():
    programs = [(str(index), status) for index, status in enumerate([*PERIOD_LABELS, 'unknown'])]
    stats = aggregate_stats(programs, [
        ('0', 'auto_assigned'), ('0', 'auto_confirmed'), ('0', 'rejected'),
        ('1', 'rejected'), ('2', 'source_structured'), ('3', None),
    ])
    assert stats == {
        'total': 9, 'unclassified': 7,
        **{status: 2 if status == 'unknown' else 1 for status in PERIOD_LABELS},
    }


def test_empty_published_corpus_keeps_zero_counts_even_with_unrelated_tags():
    assert aggregate_stats([], [('unpublished', 'auto_assigned')]) == {
        'total': 0, 'unclassified': 0, **{status: 0 for status in PERIOD_LABELS},
    }
