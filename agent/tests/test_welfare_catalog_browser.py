"""The lightweight browser query preserves paging and source-bound evidence."""
import pytest
from datetime import datetime
from unittest.mock import patch

from donghaeng_agent.welfare_catalog_browser import browser_page_query
from donghaeng_agent.welfare_postgres import CORPUS, _filters


def test_browser_query_bounds_child_reads_to_materialized_page_before_details():
    query = browser_page_query(CORPUS, 'corpus', '')
    assert query.index('page_rows AS MATERIALIZED') < query.index('LIMIT %s OFFSET %s') < query.index('AS tags')
    assert query.count('IN (SELECT program_version_id FROM page_rows)') == 5
    assert '(SELECT count(*) FROM corpus) AS total' in query
    assert '(SELECT count(*) FROM filtered) AS matched' in query
    assert query.count("'[]'::jsonb") == 7
    assert 'ORDER BY c.title,c.program_version_id LIMIT %s OFFSET %s' in query
    assert 'program_version_id=ANY(%s::uuid[])' not in query


def test_browser_query_preserves_region_rank_and_applies_filters_before_limit():
    prefix = CORPUS + ', ranked_corpus AS (SELECT c.*, 2 AS region_match_rank FROM corpus c)'
    query = browser_page_query(prefix, 'ranked_corpus', ' WHERE c.region_match_rank<3', explicit=True)
    assert 'filtered AS NOT MATERIALIZED (SELECT c.* FROM ranked_corpus c  WHERE c.region_match_rank<3)' in query
    assert 'ORDER BY c.region_match_rank,c.title,c.program_version_id LIMIT %s OFFSET %s' in query
    assert '(SELECT count(*) FROM corpus) AS total' in query  # Full published corpus, not selected geography.
    assert query.count('ORDER BY c.region_match_rank,c.title,c.program_version_id') == 2


def test_browser_query_keeps_literal_search_and_category_values_as_bound_parameters():
    search = "청년_%'); DROP TABLE programs; --"
    category = "social' OR TRUE --"
    filters, params = _filters(q=search, category_id=category, domain=None,
                              allowed_regions=[], region_code=None, status='always')
    query = browser_page_query(CORPUS, 'corpus', filters)
    assert search not in query
    assert category not in query
    assert params == [search, search, category, 'always']
    assert query.count('%s') == CORPUS.count('%s') + len(params) + 2
    assert not any(word in query.upper() for word in ('DROP TABLE', 'INSERT INTO', 'UPDATE ', 'DELETE FROM'))


def test_browser_query_keeps_evidence_and_collection_documents_with_owning_source():
    query = browser_page_query(CORPUS, 'corpus', '')
    for clause in [
        'e.evidence_id=t.evidence_id', 'e.program_version_id=t.program_version_id',
        "e.evidence_kind='category_basis'", "t.review_status<>'rejected'",
        'e.evidence_id=r.evidence_id', 'e.program_version_id=r.program_version_id',
        "e.evidence_kind='eligibility_basis'", "evidence_kind='official_link'",
        'd.document_id=vd.document_id AND d.source_id=p.source_id',
        'max(d.retrieved_at) AS source_retrieved_at',
    ]:
        assert clause in query
    # Internal ordering IDs are removed from JSON; source evidence fields remain.
    assert query.count("to_jsonb(child)-'_order_id'") == 2
    assert 'ORDER BY child.scope_type,child.region_code,child._order_id' in query
    assert 'ORDER BY child.condition_type,child._order_id' in query
    assert 'ORDER BY child.evidence_id' in query
    assert 'SELECT DISTINCT t.category_code AS category_id,k.category_name AS label' in query
    category_query = query.split('AS documents,')[1]
    assert 'FROM corpus c JOIN program_tags' in category_query
    assert 'count(' not in category_query


def test_browser_query_rejects_relation_names_outside_trusted_relations():
    with pytest.raises(ValueError, match='invalid catalog relation'):
        browser_page_query(CORPUS, 'corpus; DROP TABLE programs', '')


@pytest.mark.parametrize('as_of', ['2026-10-25', None, datetime(2026, 10, 25, 9, 30)])
def test_region_options_reject_invalid_date_before_readonly_connection(as_of):
    from donghaeng_agent.welfare_postgres import PostgresWelfareDB

    with patch('donghaeng_agent.welfare_postgres.postgres_connection') as connect:
        with pytest.raises(ValueError, match='as_of must be a date'):
            PostgresWelfareDB().catalog_regions(as_of=as_of)
    connect.assert_not_called()
