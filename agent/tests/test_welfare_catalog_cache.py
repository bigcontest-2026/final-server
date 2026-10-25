"""Catalog caching must preserve isolation and never retain failed reads."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from donghaeng_agent.welfare_catalog_cache import CatalogCache, catalog_cache_namespace


def test_success_cache_expires_from_completion_and_returns_independent_copies():
    now = [10]
    cache = CatalogCache(ttl_seconds=60, clock=lambda: now[0])
    source = {'regions': [{'name': '원문'}]}

    def load():
        now[0] += 5
        return source

    loader = Mock(side_effect=load)
    first = cache.get_or_load('same', loader)
    first['regions'][0]['name'] = '호출자 수정'
    source['regions'][0]['name'] = '원본 수정'
    now[0] = 74
    assert cache.get_or_load('same', loader) == {'regions': [{'name': '원문'}]}
    assert loader.call_count == 1
    now[0] = 75
    assert cache.get_or_load('same', loader) == source
    assert loader.call_count == 2


def test_failed_load_is_retried_and_completed_clear_invalidates_reads():
    cache = CatalogCache()
    loader = Mock(side_effect=[RuntimeError('일시 실패'), {'total': 1}, {'total': 2}])
    with pytest.raises(RuntimeError, match='일시 실패'):
        cache.get_or_load('key', loader)
    assert cache.get_or_load('key', loader) == {'total': 1}
    assert cache.get_or_load('key', loader) == {'total': 1}
    cache.clear()
    assert cache.get_or_load('key', loader) == {'total': 2}
    assert loader.call_count == 3


def test_filter_keys_are_independent_and_least_recently_used_entry_is_bounded():
    cache = CatalogCache(max_entries=2)
    loader = Mock(side_effect=lambda: {'load': loader.call_count})
    assert cache.get_or_load(('db-a', '2026-10-25', 'social'), loader) == {'load': 1}
    assert cache.get_or_load(('db-a', '2026-10-25', 'income'), loader) == {'load': 2}
    assert cache.get_or_load(('db-a', '2026-10-25', 'social'), loader) == {'load': 1}
    assert cache.get_or_load(('db-b', '2026-10-25', 'social'), loader) == {'load': 3}
    assert cache.get_or_load(('db-a', '2026-10-25', 'income'), loader) == {'load': 4}
    assert len(cache._entries) == 2


def test_none_key_bypasses_storage_and_pending_loads():
    cache = CatalogCache()
    loader = Mock(side_effect=[{'total': 1}, {'total': 2}])
    assert cache.get_or_load(None, loader) == {'total': 1}
    assert cache.get_or_load(None, loader) == {'total': 2}
    assert not cache._entries
    assert not cache._pending


def test_same_key_shares_inflight_read_while_other_keys_remain_available():
    cache = CatalogCache()
    started, release, waiting = Event(), Event(), Event()

    def load():
        started.set()
        assert release.wait(5)
        return {'regions': ['서울']}

    loader = Mock(side_effect=load)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(cache.get_or_load, 'same', loader)
        assert started.wait(5)
        # Instrument the existing future so the main thread knows the second
        # reader has entered its wait before releasing the original loader.
        future = cache._pending['same']
        original_result = future.result

        def observed_result(*args, **kwargs):
            waiting.set()
            return original_result(*args, **kwargs)

        future.result = observed_result
        second = pool.submit(cache.get_or_load, 'same', loader)
        assert waiting.wait(5)
        unrelated = pool.submit(cache.get_or_load, 'different', lambda: {'total': 2})
        assert unrelated.result(timeout=5) == {'total': 2}
        release.set()
        first_result, second_result = first.result(timeout=5), second.result(timeout=5)
    assert loader.call_count == 1
    first_result['regions'].append('부산')
    assert second_result == {'regions': ['서울']}
    assert not cache._pending


def test_waiters_receive_failure_and_next_read_can_retry():
    cache = CatalogCache()
    started, release, waiting = Event(), Event(), Event()

    def load():
        started.set()
        assert release.wait(5)
        raise RuntimeError('일시 실패')

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cache.get_or_load, 'same', load)
        assert started.wait(5)
        future = cache._pending['same']
        original_result = future.result

        def observed_result(*args, **kwargs):
            waiting.set()
            return original_result(*args, **kwargs)

        future.result = observed_result
        second = pool.submit(cache.get_or_load, 'same', load)
        assert waiting.wait(5)
        release.set()
        for item in (first, second):
            with pytest.raises(RuntimeError, match='일시 실패'):
                item.result(timeout=5)
    assert not cache._entries
    assert not cache._pending
    assert cache.get_or_load('same', lambda: {'ok': True}) == {'ok': True}


def test_namespace_separates_configurations_without_returning_secrets(monkeypatch):
    monkeypatch.delenv('WELFARE_DATABASE_URL', raising=False)
    assert catalog_cache_namespace() is None
    monkeypatch.setenv('WELFARE_DATABASE_URL', 'postgresql://user:secret@db.test/postgres')
    monkeypatch.setenv('WELFARE_DATABASE_PASSWORD', 'raw-secret')
    monkeypatch.setenv('WELFARE_DATABASE_SCHEMA', 'welfare')
    original = catalog_cache_namespace()
    assert len(original) == 64
    assert 'secret' not in original
    for name, value in [('WELFARE_DATABASE_PASSWORD', 'other-secret'),
                        ('WELFARE_DATABASE_SCHEMA', 'other_schema'),
                        ('WELFARE_DATABASE_URL', 'postgresql://user:secret@other.test/postgres')]:
        with monkeypatch.context() as changed:
            changed.setenv(name, value)
            assert catalog_cache_namespace() != original


@pytest.fixture
def catalog_harness(monkeypatch):
    """Exercise catalog orchestration with a deterministic paged database."""
    from contextlib import contextmanager
    import donghaeng_agent.welfare_postgres as module

    now, namespace, publications = [0], ['database-a'], ['publication-a']
    stats = Mock(return_value={'total': 24, 'unclassified': 0,
                              **{key: 0 for key in module.PERIOD_LABELS}})
    tree = Mock(return_value=[])
    regions = Mock(return_value=[{'code': '11680', 'name': '강남구', 'count': 24}])
    counts = {'matched': 0, 'categories': 0, 'pages': 0}

    def execute(query, params):
        if 'to_regclass' in query:
            return Mock(fetchone=lambda: {'rules':False,'runs':False,'actions':False})
        if 'SELECT count(*) AS matched FROM' in query:
            counts['matched'] += 1
            return Mock(fetchone=lambda: {'matched': 24})
        if 'SELECT c.* FROM' in query:
            counts['pages'] += 1
            limit, offset = params[-2:]
            return Mock(fetchall=lambda: [
                {'program_version_id': str(index), 'title': f'사업 {index}', 'region_match_rank': 2}
                for index in range(offset, min(24, offset + limit))])
        assert 'SELECT t.category_code AS category_id' in query
        counts['categories'] += 1
        return Mock(fetchall=lambda: [{'category_id': 'social', 'label': '관계', 'count': 24}])

    @contextmanager
    def connection():
        yield SimpleNamespace(execute=execute)

    publication_reads = Mock(side_effect=lambda db: [
        {'publication_id': value, 'source_id': 'gov24'} for value in publications])
    monkeypatch.setattr(module, 'CATALOG_CACHE', CatalogCache(clock=lambda: now[0]))
    monkeypatch.setattr(module, 'catalog_cache_namespace', lambda: namespace[0])
    monkeypatch.setattr(module, 'postgres_connection', connection)
    monkeypatch.setattr(module.PostgresWelfareDB, '_publications', publication_reads)
    monkeypatch.setattr(module.PostgresWelfareDB, '_stats', stats)
    monkeypatch.setattr(module.PostgresWelfareDB, '_region_tree', tree)
    monkeypatch.setattr(module.PostgresWelfareDB, '_region_facets', regions)
    monkeypatch.setattr(module.PostgresWelfareDB, '_children', lambda *args: ({}, {}, {}, {}, {}))
    monkeypatch.setattr(module, '_normalize_program', lambda row, *args: {
        'version_id': row['program_version_id'], 'title': row['title']})
    return SimpleNamespace(store=module.PostgresWelfareDB(), now=now, namespace=namespace,
                           publications=publications, publication_reads=publication_reads,
                           stats=stats, tree=tree, regions=regions, counts=counts)


def test_next_catalog_page_reuses_metadata_and_count_but_reads_its_own_programs(catalog_harness):
    h = catalog_harness
    first = h.store.catalog(as_of=date(2026, 10, 25), offset=0, limit=12)
    first['regions'][0]['name'] = '호출자 수정'
    first['categories'][0]['label'] = '호출자 수정'
    second = h.store.catalog(as_of=date(2026, 10, 25), offset=12, limit=12)
    assert [row['version_id'] for row in first['programs']] == [str(value) for value in range(12)]
    assert [row['version_id'] for row in second['programs']] == [str(value) for value in range(12, 24)]
    assert (first['next_offset'], second['next_offset']) == (12, None)
    assert h.counts == {'matched': 1, 'categories': 1, 'pages': 2}
    assert h.stats.call_count == h.tree.call_count == h.regions.call_count == 1
    assert h.publication_reads.call_count == 2  # Resolve the active publication on every request.
    assert second['regions'][0]['name'] == '강남구'
    assert second['categories'][0]['label'] == '관계'


@pytest.mark.parametrize('change', ['publication', 'date', 'database', 'expiry'])
def test_shared_catalog_metadata_invalidates_when_its_basis_changes(catalog_harness, change):
    h = catalog_harness
    day = date(2026, 10, 25)
    h.store.catalog(as_of=day, limit=12)
    if change == 'publication':
        h.publications[:] = ['publication-b']
    elif change == 'date':
        day = date(2026, 10, 26)
    elif change == 'database':
        h.namespace[0] = 'database-b'
    else:
        h.now[0] = 60
    h.store.catalog(as_of=day, offset=12, limit=12)
    assert h.counts == {'matched': 2, 'categories': 2, 'pages': 2}
    assert h.stats.call_count == h.tree.call_count == h.regions.call_count == 2


def test_catalog_counts_are_separate_for_search_category_and_region_filters(catalog_harness):
    h = catalog_harness
    filters = [{}, {'q': '청년'}, {'category_id': 'social'}, {'region_level': 'national'},
               {'status': 'always'}, {'domain': 'E'}]
    for query in filters:
        result = h.store.catalog(as_of=date(2026, 10, 25), limit=12, **query)
        assert result['matched'] == 24
    assert h.counts == {'matched': len(filters), 'categories': 1, 'pages': len(filters)}
    assert h.stats.call_count == h.tree.call_count == h.regions.call_count == 1
