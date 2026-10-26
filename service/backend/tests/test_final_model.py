"""Fabricated bundles are test fixtures only, never application data."""
from final_model.src.i5_display import calculate_i5_display_score

import copy
import hashlib
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from app.config import DEFAULT_MODEL_ROOT, Settings
from app.final_model import ACTIVITY_CONTRACT, SURVEY_CONTRACT, model_modules
from app.main import create_app


def write_bundle(root, files, family):
    root.mkdir(parents=True, exist_ok=True)
    for name, rows in files.items():
        (root / name).write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
    manifest = {"run_id": "synthetic-test-only", "status": "completed", "is_synthetic": False,
                "contract_version": SURVEY_CONTRACT if family == "survey" else ACTIVITY_CONTRACT,
                "provenance": {"model_version": ACTIVITY_CONTRACT},
                "outputs": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}}
    (root / 'manifest.json').write_text(json.dumps(manifest))
    return root


def source(unit):
    return {"unit": unit, "activity_daily": 80, "previous_activity_daily": 100, "change_percent": -20,
            "Q": 0.2231435513142097, "H": 0.1, "T": 0.1231435513142097, "direction": "decrease",
            "robust": {"z": None, "mad": 0, "status": "unavailable_zero_mad", "candidate": None},
            "calculation_status": "computed", "reason": None}


def behavior(month="202512", age="20G"):
    return {
        "contract_version": ACTIVITY_CONTRACT, "record_id": f"behavior:{month}:11680:MAN:{age}",
        "key": {"month": month, "region_code": "11680", "sex_code": "MAN", "age_code": age},
        "region_name": "강남구", "spatial_unit": "sgg",
        "geography_basis": {"skt": "observed_place", "card_a": "merchant_location"},
        "population_basis": "source_aggregate_sex_age_not_identified_residents",
        "sources": {"skt": source("daily_place_flow_sum"), "card_a": source("payment_count_per_day")},
        "observation": {"card_count": 2480, "card_unit": "transactions"},
        "rule": {"candidate": True, "two_month_streak": None},
        "robust": {"candidate": None, "status": "unavailable", "cutoff": 3.5},
        "comparison_status": "computed",
        "coverage": {"skt_observed_blocks": 10, "skt_coverage_ratio": None, "reason": "expected_block_universe_unverified"},
        "historical_reference_id": None,
        "spatial_context": {"status": "not_comparable", "reason": "M3_official_mapping_unverified"},
        "calendar_context": {"skt_daily_adjustment": None, "card_previous_month_basis": "calendar_days"},
        "reason_codes": ["BOTH_SOURCES_DECREASE"], "explanation": "테스트 전용 관측",
        "possible_explanations": [], "unverified_factors": [], "quality": {"status": "hold", "reasons": ["test_fixture"]},
        "operational_alert_allowed": False,
        "provenance": {"model_version": ACTIVITY_CONTRACT, "run_id": "synthetic-test-only", "config_sha256": "0" * 64,
                       "inputs": {str(i): "0" * 64 for i in range(2)},
                       "source_code_sha256": {str(i): "0" * 64 for i in range(7)},
                       "analysis_period": ["202507", "202512"], "source_release_dates_verified": False},
        "limitations": ["synthetic_test_fixture"],
    }


def survey_row(level="sgg", code="11680", name="강남구", month="202512"):
    return {
        "schema_version": SURVEY_CONTRACT, "record_id": f"survey_i5:2025:{level}:{code}:{month}",
        "month": month, "survey_year": 2025, "spatial_unit": level, "region_code": "11680", "region_name": "강남구",
        "area_code": code, "area_name": name, "dong_code": code if level == "administrative_dong" else None,
        "index_name": "사회조사 기반 인구구성 고립취약성 참고지수(I5)",
        "index_kind": "population_composition_weighted_survey_reference", "isolation_index": 35, "i5_reference": 35, **calculate_i5_display_score(35),
        "scale": {"min": 0, "max": 100, "higher_means": "higher_reference_vulnerability"},
        "domain_scores": {key: 35 for key in "EPOCU"}, "population_20plus": 600, "population_total": 800,
        "population_basis": "MOIS_month_end_registered_population_20plus",
        "group_weights": {f"{sex}_{age}": 1/6 for sex in ("남", "여") for age in ("20-39", "40-59", "60+")},
        "national_survey_n_valid": 31817, "local_survey_n_valid": None, "local_survey_measured": False,
        "is_isolation_rate": False, "behavior_signals_included": False,
        "quality": {"status": "reference_only", "formula_verified": True}, "operational_alert_allowed": False,
        "source_contract": "survey_mois_reference_v1", "source_index_version": "I5_2025_final_v1",
        "source_data_version": "test-only", "limitations": ["test_fixture", "population_reference", "not_local_survey", "no_activity_join"],
    }


@pytest.fixture
def settings(tmp_path):
    activity = write_bundle(tmp_path / "activity", {
        "behavior_evidence.jsonl": [behavior("202511"), behavior(), behavior(age="40G")],
        "historical_reference.jsonl": [],
    }, "activity")
    survey = write_bundle(tmp_path / "survey", {
        "sgg_isolation_index.jsonl": [survey_row()],
        "dong_isolation_index.jsonl": [survey_row("administrative_dong", "1168051000", "신사동")],
    }, "survey")
    return Settings(final_model_bundle=activity, survey_area_bundle=survey)


def test_default_model_and_separate_legacy_routes(settings, monkeypatch):
    monkeypatch.delenv("APP_MODE", raising=False)
    assert Settings.from_env().mode == "final"
    with TestClient(create_app(settings)) as client:
        status = client.get('/api/health').json()
        assert status['status'] == 'ok'
        assert status['sources']['activity']['contract_version'] == ACTIVITY_CONTRACT
        assert status['sources']['survey']['contract_version'] == SURVEY_CONTRACT
        assert client.post('/api/investigations', json={}).status_code == 404
        assert client.get('/api/research').status_code == 404


def test_behavior_filters_pagination_and_nulls(settings):
    with TestClient(create_app(settings)) as client:
        page = client.get('/api/model/behavior', params={'limit': 1}).json()
        assert page['total_matched'] == 3 and page['next_offset'] == 1
        result = client.get('/api/model/behavior', params={'month':'202512', 'age_code':'40G'}).json()
        assert result['total_matched'] == 1
        row = result['records'][0]
        assert row['historical_reference'] is None
        assert row['behavior']['sources']['skt']['robust']['z'] is None
        assert row['behavior']['sources']['card_a']['change_percent'] == -20
        assert result['quality_status'] == 'hold' and result['operational_alert_allowed'] is False
        assert client.get('/api/model/behavior?month=202507').json()['records'] == []
        assert client.get('/api/model/behavior?sex_code=WMAN').json()['records'] == []


def test_survey_reference_and_exact_dong(settings):
    with TestClient(create_app(settings)) as client:
        page = client.get('/api/model/survey-area?level=administrative_dong&dong_code=1168051000').json()
        assert page['contract_version'] == SURVEY_CONTRACT
        assert page['records'][0]['isolation_index'] == 35
        assert page['records'][0]['local_survey_n_valid'] is None
        assert page['records'][0]['behavior_signals_included'] is False
        assert client.get('/api/model/survey-area?level=administrative_dong&dong_code=1162068500').json()['records'] == []


@pytest.mark.parametrize('url', [
    '/behavior?region_code=1168051000', '/behavior?month=202513', '/behavior?age_code=20-39',
    '/behavior?limit=101', '/behavior?offset=-1', '/survey-area?level=sgg&dong_code=1168051000',
    '/survey-area?level=administrative_dong&dong_code=1168051000&region_code=51110',
    '/survey-area?level=dong', '/survey-area?limit=501',
    '/behavior?dong_code=1168051000', '/survey-area?age_code=20G',
    '/behavior?region_code=99999', '/survey-area?province_code=99',
    '/survey-area?province_code=11&region_code=26110', '/survey-area?province_code=26&dong_code=1168051000',
])
def test_invalid_dimensions(settings, url):
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/model' + url).status_code == 422


@pytest.mark.parametrize('mutation', ['tamper', 'failed', 'synthetic', 'untracked', 'wrong_version', 'schema', 'duplicate'])
def test_invalid_bundle_never_falls_back(settings, mutation):
    root = settings.final_model_bundle
    path = root / 'manifest.json'
    manifest = json.loads(path.read_text())
    if mutation in ('tamper', 'schema', 'duplicate'):
        file = root / 'behavior_evidence.jsonl'
        rows = [json.loads(line) for line in file.read_text().splitlines()]
        if mutation == 'duplicate': rows.append(copy.deepcopy(rows[0]))
        else: rows[0]['operational_alert_allowed'] = True
        file.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        if mutation != 'tamper': manifest['outputs'][file.name] = hashlib.sha256(file.read_bytes()).hexdigest()
    elif mutation == 'failed': manifest['status'] = 'failed'
    elif mutation == 'synthetic': manifest['is_synthetic'] = True
    elif mutation == 'untracked': manifest['outputs'].pop('behavior_evidence.jsonl')
    else: manifest['provenance']['model_version'] = 'old_snapshot'
    path.write_text(json.dumps(manifest))
    with TestClient(create_app(settings)) as client:
        response = client.get('/api/model/behavior')
        assert response.status_code == 503
        assert str(root) not in response.text
        assert client.get('/api/model/status').json()['status'] == 'partial'
        assert client.get('/api/model/survey-area').status_code == 200


def test_missing_bundle(settings, tmp_path):
    with TestClient(create_app(replace(settings, final_model_bundle=tmp_path/'absent'))) as client:
        assert client.get('/api/model/behavior').json()['detail']['code'] == 'bundle_missing'
        assert client.get('/api/health').json()['sources']['survey']['ready'] is True


def test_pointer_traversal_rejected(settings, tmp_path, monkeypatch):
    root = tmp_path/'runtime'
    root.mkdir()
    (root/'latest.json').write_text(json.dumps({'run_id':'../activity'}))
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app(replace(settings, final_model_bundle=None, model_runtime_root=root))) as client:
        assert client.get('/api/model/behavior').status_code == 503


def test_survey_tamper_is_independent(settings):
    with (settings.survey_area_bundle/'dong_isolation_index.jsonl').open('a') as stream: stream.write('\n')
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/model/survey-area').status_code == 503
        assert client.get('/api/model/behavior').status_code == 200


def test_actual_frozen_I5_roundtrip(tmp_path):
    source_path = DEFAULT_MODEL_ROOT.parent/'isolation-index/v3/exports/2025_final_v1'
    if not source_path.is_dir(): pytest.skip('real frozen V3 export is not present in this checkout')
    _, survey, _, _ = model_modules(DEFAULT_MODEL_ROOT)
    bundle = survey.build(source=source_path, output_root=tmp_path/'generated')
    with TestClient(create_app(Settings(survey_area_bundle=bundle, final_model_bundle=tmp_path/'absent'))) as client:
        dongs = client.get('/api/model/survey-area?level=administrative_dong&limit=500').json()
        assert dongs['total_matched'] == 282 and len(dongs['records']) == 282
        row = client.get('/api/model/survey-area?month=202512&region_code=11680').json()['records'][0]
        assert row['isolation_index'] == pytest.approx(34.12395682037197)
        assert row['local_survey_measured'] is False


def test_linked_C3_is_preserved_as_a_separate_baseline(settings):
    row = behavior()
    row['historical_reference_id'] = 'C3:test'
    metric = {'reported_sum': 100, 'comparable_sum': 100, 'baseline_sum': 80, 'change_pct': 25,
              'coverage': 1, 'comparable_days': 31, 'decline_flag': False, 'withheld_reasons': [], 'unit': 'transactions'}
    history = {'schema_version': 'c3.agent.v1', 'record_id': 'C3:test', 'dataset': 'data2',
               'view': 'age_sex', 'region': '강남구', 'age': '20', 'sex': '남성', 'month': '2025-12',
               'calendar_type': 'all', 'review_flag': False, 'count': metric,
               'amount': {**metric, 'unit': 'source_amount_unit'}, 'daily_support': {},
               'source_fingerprint': '0'*64, 'config_sha256': '0'*64, 'future_values_used': False, 'is_demo': False,
               'reference_start': '2025-07-01', 'reference_end': '2025-09-30',
               'period_start': '2025-12-01', 'period_end': '2025-12-31'}
    write_bundle(settings.final_model_bundle, {'behavior_evidence.jsonl': [row], 'historical_reference.jsonl': [history]}, 'activity')
    with TestClient(create_app(settings)) as client:
        response = client.get('/api/model/behavior?month=202512&region_code=11680&sex_code=MAN&age_code=20G')
        assert response.status_code == 200
        result = response.json()['records'][0]
        assert result['historical_reference'] == history
        assert result['behavior']['sources']['card_a']['change_percent'] == -20
        assert result['historical_reference']['count']['change_pct'] == 25


def test_valid_runtime_pointer_is_discovered(settings, tmp_path, monkeypatch):
    root = tmp_path/'pointers'
    write_bundle(root/'activity', {'behavior_evidence.jsonl': [behavior()], 'historical_reference.jsonl': []}, 'activity')
    (root/'latest.json').write_text(json.dumps({'run_id':'activity'}))
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app(replace(settings, final_model_bundle=None, model_runtime_root=root))) as client:
        assert client.get('/api/model/behavior').json()['total_matched'] == 1


def test_new_region_is_accepted_without_fabricating_activity(settings):
    with TestClient(create_app(settings)) as client:
        result = client.get('/api/model/behavior?region_code=26110')
        assert result.status_code == 200
        assert result.json()['records'] == []
        assert result.json()['availability']['status'] == 'data_unavailable'


def test_survey_cache_revalidates_new_output_and_detects_tamper(settings):
    root = settings.survey_area_bundle
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/model/survey-area').status_code == 200
        invalid = survey_row()
        invalid['isolation_index'] = 999
        write_bundle(root, {'sgg_isolation_index.jsonl': [invalid], 'dong_isolation_index.jsonl': []}, 'survey')
        assert client.get('/api/model/regions').status_code == 503


def test_i5_environment_alias_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv('SURVEY_AREA_BUNDLE', str(tmp_path/'old'))
    monkeypatch.setenv('I5_BUNDLE_PATH', str(tmp_path/'national'))
    assert Settings.from_env().survey_area_bundle == tmp_path/'national'
    monkeypatch.delenv('I5_BUNDLE_PATH')
    assert Settings.from_env().survey_area_bundle == tmp_path/'old'


def test_published_national_bundle_overrides_old_runtime_and_serves_all_seoul(tmp_path):
    bundle = DEFAULT_MODEL_ROOT/'data/i5/national_2025'
    assert bundle.is_dir(), 'Latest model with published national I5 data is required'
    runtime = tmp_path/'runtime'
    old = write_bundle(runtime/'old', {'sgg_isolation_index.jsonl': [survey_row()],
                                      'dong_isolation_index.jsonl': []}, 'survey')
    (runtime/'survey_area_index_latest.json').write_text(json.dumps({'run_id': old.name}))
    with TestClient(create_app(Settings(model_runtime_root=runtime))) as client:
        catalog = client.get('/api/model/regions?month=202512').json()
        assert len(catalog['regions']) == 264
        assert len({r['province_code'] for r in catalog['regions']}) == 17
        seoul = [r for r in catalog['regions'] if r['province_code'] == '11']
        assert len(seoul) == 25 and sum(r['dong_count'] for r in seoul) == 427
        for month in ('202507', '202512'):
            page = client.get('/api/model/survey-area', params={
                'province_code': '11', 'level': 'administrative_dong', 'month': month, 'limit': 500}).json()
            assert page['geographic_scope'] == 'nationwide'
            assert page['total_matched'] == len(page['records']) == 427
            assert len({r['region_code'] for r in page['records']}) == 25
        page = client.get('/api/model/survey-area?province_code=41&level=administrative_dong&month=202512&limit=500').json()
        assert page['next_offset'] == 500 and page['total_matched'] > 500
        rest = client.get('/api/model/survey-area?province_code=41&level=administrative_dong&month=202512&limit=500&offset=500').json()
        assert len(page['records']) + len(rest['records']) == page['total_matched']
        busan = client.get('/api/research/i5?dong_code=2611051000&month=202512').json()
        assert busan['total_matched'] == 1 and busan['records'][0]['region_code'] == '26110'
        for level in ('sgg', 'administrative_dong'):
            page = client.get('/api/model/survey-area', params={'level': level, 'limit': 1}).json()
            assert page['total_matched'] == (1584 if level == 'sgg' else 21698)
    # An explicit bad path must never silently switch to the published bundle.
    with TestClient(create_app(Settings(survey_area_bundle=tmp_path/'missing'))) as client:
        assert client.get('/api/model/survey-area').status_code == 503
