"""Keep population and historical-card context separate from support selection."""
import csv
import math
from pathlib import Path


def _population_record(bundle, *, region_code, month):
    from final_model.src.common import verify_bundle

    root = Path(bundle)
    manifest = verify_bundle(root)
    name = 'survey_mois_reference/region_month_summary.csv'
    if name not in manifest['outputs']:
        if (root / name).exists():
            raise ValueError('untracked_population_context')
        return None
    with (root / name).open(encoding='utf-8-sig', newline='') as stream:
        rows = [row for row in csv.DictReader(stream)
                if row['region_code'] == region_code and row['month'] == month]
    if len(rows) > 1:
        raise ValueError('ambiguous_population_context')
    if not rows:
        return None
    record = {key: value if value != '' else None for key, value in rows[0].items()}
    for key, value in record.items():
        if value is None or key in ('region_code', 'region', 'month'):
            continue
        numeric = float(value)
        if not math.isfinite(numeric) or (not key.startswith('monthly_change_') and numeric < 0):
            raise ValueError('invalid_population_context')
        if (key.startswith('share_') or key == 'one_person_household_share') and numeric > 1:
            raise ValueError('invalid_population_share')
        record[key] = numeric
    return record


def support_context(behavior, assessment, *, historical_reference=None, population=None, scope=None):
    """Validate exact scope; neither contextual source activates a new category."""
    key = behavior['key'] if behavior else (scope or {})
    if behavior and scope and any(key.get(field) != value for field, value in scope.items()):
        raise ValueError('support_context_scope_mismatch')
    if population is not None and any(population.get(field) != key.get(field)
                                      for field in ('region_code', 'month')):
        raise ValueError('population_context_scope_mismatch')
    history = historical_reference
    if history is not None:
        if (behavior is None or history.get('record_id') != behavior.get('historical_reference_id')
                or history.get('month', '').replace('-', '') != key.get('month')
                or history.get('region') != behavior.get('region_name')
                or history.get('sex') != {'MAN': '남성', 'WMAN': '여성'}.get(key.get('sex_code'))
                or str(history.get('age', '')) + 'G' != key.get('age_code')
                or history.get('dataset') != 'data2' or history.get('view') != 'age_sex'
                or history.get('calendar_type') != 'all'
                or any(history.get(field, 'ALL') != 'ALL'
                       for field in ('industry', 'time_band', 'residence_sido'))):
            raise ValueError('historical_support_context_scope_mismatch')
    current = assessment.get('sources', {}).get('card_a', {}).get('threshold_met')
    prior = history.get('count', {}).get('decline_flag') if history else None
    consistency = ('unavailable' if current is None or prior is None else
                   'both_decline' if current and prior else
                   'monthly_only' if current else 'historical_only' if prior else 'neither')
    return {
        'population': {'status': 'available' if population else 'unavailable', 'record': population,
                       'role': 'population_and_household_context', 'changes_category': False,
                       'automatic_10_to_6_group_join': False},
        'historical_card': {'status': 'available' if history else 'unavailable',
                            'record': history, 'consistency': consistency,
                            'independent_evidence': False, 'changes_category': False},
        'risk_score_combined': False,
    }


def load_support_context(bundle, behavior, assessment, *, historical_reference=None, scope=None):
    key = behavior['key'] if behavior else (scope or {})
    population = _population_record(bundle, region_code=key.get('region_code'), month=key.get('month'))
    return support_context(behavior, assessment, historical_reference=historical_reference,
                           population=population,scope=scope)
