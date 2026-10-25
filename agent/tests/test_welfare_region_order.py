"""Region browsing follows the stored administrative tree before pagination."""
from datetime import date
from unittest.mock import MagicMock, Mock, patch

import pytest

from donghaeng_agent.welfare_postgres import (
    PERIOD_LABELS, PostgresWelfareDB, catalog_region_facets,
    catalog_region_ranks, validate_catalog_region,
)


def region_tree():
    return [
        dict(region_id='province', parent_region_id=None, region_code='1100000000',
             region_name='서울특별시', region_level='sido', sido_code='11', sgg_code=None),
        dict(region_id='district', parent_region_id='province', region_code='1168000000',
             region_name='서울특별시 강남구', region_level='sigungu', sido_code=None, sgg_code='11680'),
        dict(region_id='sibling-district', parent_region_id='province', region_code='1171000000',
             region_name='서울특별시 송파구', region_level='sigungu', sido_code=None, sgg_code='11710'),
        dict(region_id='dong', parent_region_id='district', region_code='1168051000',
             region_name='서울특별시 강남구 신사동', region_level='eupmyeondong', sido_code=None, sgg_code=None),
        dict(region_id='sibling-dong', parent_region_id='district', region_code='1168052100',
             region_name='서울특별시 강남구 논현1동', region_level='eupmyeondong', sido_code=None, sgg_code=None),
        # Deliberately misleading numeric prefix: parent links govern membership.
        dict(region_id='unrelated-dong', parent_region_id='sibling-district', region_code='1168059999',
             region_name='다른 구의 동', region_level='eupmyeondong', sido_code=None, sgg_code=None),
    ]


def test_facets_use_stored_parents_and_exact_external_district_codes():
    facets = catalog_region_facets(region_tree())
    dong = next(row for row in facets if row['region_id'] == 'dong')
    assert dong['parent_code'] == '11680'
    assert (dong['province_code'], dong['province_name']) == ('11', '서울특별시')
    assert dong['province_region_id'] == 'province'
    unrelated = next(row for row in facets if row['region_id'] == 'unrelated-dong')
    assert unrelated['parent_code'] == '11710'
    rows = region_tree()
    rows[1]['sgg_code'] = None
    assert not any(row['region_id'] in ('district', 'dong', 'sibling-dong')
                   for row in catalog_region_facets(rows))


def test_district_order_includes_own_dongs_without_prefix_guessing():
    ranks, order = catalog_region_ranks(catalog_region_facets(region_tree()), 'sgg', '11680')
    assert dict(ranks) == {'district': 0, 'dong': 1, 'sibling-dong': 1}
    assert [item['level'] for item in order] == ['sgg', 'administrative_dong', 'national']


def test_dong_order_includes_parent_district_but_not_siblings():
    ranks, order = catalog_region_ranks(catalog_region_facets(region_tree()), 'administrative_dong', '1168051000')
    assert dict(ranks) == {'dong': 0, 'district': 1}
    assert [item['level'] for item in order] == ['administrative_dong', 'sgg', 'national']
    assert catalog_region_ranks(catalog_region_facets(region_tree()), 'national', None) == (
        [], [{'level': 'national', 'label': '전국', 'rank': 2}])


def test_city_scope_includes_descendant_dongs_while_dong_keeps_nearest_district():
    rows = region_tree()
    rows.append(dict(region_id='city', parent_region_id='province', region_code='4111000000',
                     region_name='수원시', region_level='sigungu', sido_code=None, sgg_code='41110'))
    rows[1].update(parent_region_id='city', region_level='gu')
    facets = catalog_region_facets(rows)
    dong = next(row for row in facets if row['region_id'] == 'dong')
    assert dong['ancestor_codes'] == ['11680', '41110']
    assert dict(catalog_region_ranks(facets, 'sgg', '41110')[0]) == {
        'city': 0, 'dong': 1, 'sibling-dong': 1}
    assert dict(catalog_region_ranks(facets, 'administrative_dong', '1168051000')[0]) == {
        'dong': 0, 'district': 1}


def test_province_scope_includes_its_native_id_and_tree_descendants_not_code_prefixes():
    rows = region_tree() + [
        dict(region_id='other-province', parent_region_id=None, region_code='2600000000',
             region_name='부산광역시', region_level='sido', sido_code='26', sgg_code=None),
        # These misleading public codes must not make the rows Seoul descendants.
        dict(region_id='other-district', parent_region_id='other-province', region_code='1169900000',
             region_name='부산 소속 구', region_level='sigungu', sido_code=None, sgg_code='11699'),
        dict(region_id='other-dong', parent_region_id='other-district', region_code='1169999999',
             region_name='부산 소속 동', region_level='eupmyeondong', sido_code=None, sgg_code=None),
    ]
    facets = catalog_region_facets(rows)
    ranks, order = catalog_region_ranks(facets, 'sido', '11')
    assert dict(ranks) == {
        'province': 0, 'district': 1, 'sibling-district': 1,
        'dong': 1, 'sibling-dong': 1, 'unrelated-dong': 1,
    }
    assert order[0] == {'level': 'sido', 'label': '서울특별시', 'rank': 0}
    assert order[1]['label'] == '소속 시군구·읍면동'
    assert order[-1] == {'level': 'national', 'label': '전국', 'rank': 2}
    assert len(facets) == 7  # Province metadata does not add extra selectable levels.


def test_province_scope_requires_exact_mapping_in_the_stored_ancestry():
    rows = region_tree()
    rows[0]['sido_code'] = None
    facets = catalog_region_facets(rows)
    assert all(row['province_region_id'] is None for row in facets)
    assert catalog_region_ranks(facets, 'sido', '11') == (
        [], [{'level': 'national', 'label': '전국', 'rank': 2}])
    validate_catalog_region('sido', '11')


@pytest.mark.parametrize(('level', 'code'), [
    ('national', '11680'), ('sgg', None), ('administrative_dong', None),
    ('sgg', '1168051000'), ('administrative_dong', '11680'), (None, '1168051000'),
    ('sido', None), ('sido', '11680'), ('sido', '1'), ('sgg', '11'),
])
def test_scope_validation_rejects_inconsistent_codes_before_database_access(level, code):
    with pytest.raises(ValueError):
        validate_catalog_region(level, code)


@pytest.mark.parametrize(('level', 'code', 'local_ids', 'related_ids'), [
    ('sgg', '11680', ['district'], ['dong', 'sibling-dong']),
    ('sido', '11', ['province'], ['district', 'dong', 'sibling-dong', 'unrelated-dong', 'sibling-district']),
])
def test_database_applies_minimum_scope_rank_and_filter_before_page_limit(level, code, local_ids, related_ids):
    db = Mock()
    calls = []

    def execute(query, params):
        calls.append((query, params))
        return Mock(fetchone=Mock(return_value={'matched': 0}), fetchall=Mock(return_value=[]))

    db.execute.side_effect = execute
    connection = MagicMock()
    connection.__enter__.return_value = db
    stats = {'total': 0, 'unclassified': 0, **{key: 0 for key in PERIOD_LABELS}}
    with patch('donghaeng_agent.welfare_postgres.postgres_connection', return_value=connection), \
            patch.object(PostgresWelfareDB, '_publications', return_value=[{'publication_id': 'publication'}]), \
            patch.object(PostgresWelfareDB, '_stats', return_value=stats), \
            patch.object(PostgresWelfareDB, '_region_tree', return_value=region_tree()):
        result = PostgresWelfareDB().catalog(as_of=date(2026, 10, 25), region_level=level,
                                           region_code=code, offset=12, limit=12)
    query, params = next(call for call in calls if 'SELECT c.* FROM ranked_corpus' in call[0])
    assert 'SELECT min(CASE' in query  # Multiple jurisdiction links produce one program.
    assert query.index('c.region_match_rank<3') < query.index('ORDER BY c.region_match_rank') < query.index('LIMIT')
    assert params[2] == local_ids
    assert set(params[3]) == set(related_ids)
    assert params[-2:] == [12, 12]
    assert len(result['available_regions']) == 5


def test_browser_province_scope_loads_native_tree_and_uses_the_same_rank_groups():
    page = dict(total=0, matched=0, rows=[], tags=[], scopes=[], rules=[], links=[], documents=[], categories=[])
    db = Mock()
    db.execute.return_value.fetchone.return_value = page
    connection = MagicMock()
    connection.__enter__.return_value = db
    with patch('donghaeng_agent.welfare_postgres.postgres_connection', return_value=connection), \
            patch.object(PostgresWelfareDB, '_publications', return_value=[{'publication_id': 'publication'}]), \
            patch.object(PostgresWelfareDB, '_region_tree', return_value=region_tree()) as tree:
        result = PostgresWelfareDB().catalog(as_of=date(2026, 10, 25), region_level='sido',
                                           region_code='11', view='browser', offset=12, limit=12)
    tree.assert_called_once()
    params = db.execute.call_args.args[1]
    assert params[2] == ['province']
    assert set(params[3]) == {'district', 'sibling-district', 'dong', 'sibling-dong', 'unrelated-dong'}
    assert params[-2:] == [12, 12]
    assert result['region_level'] == 'sido'
    assert result['region_search_order'][0]['label'] == '서울특별시'
