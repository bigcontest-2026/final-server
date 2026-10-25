"""Read-only original V3 audit and explanation references. Never rewrites a model bundle."""
import argparse
import csv
import hashlib
import io
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

from .common import dump, read_jsonl, sha, verify_bundle, write_jsonl
from .pipeline import FINAL

PREFIX = '02_v3_behavior/outputs/v3/behavior/'
KEY_FIELDS = ('month','region_code','sex_code','age_code')
NUMERIC_FIELDS = ('activity_daily','previous_activity_daily','change_percent','Q','H','T','robust.z','robust.mad')
DETAIL_FIELDS = ('source_scope','source_quality','activity_change_type','regional_context','method_relation',
                 'rule','robust','reason_codes','explanation','synthetic_validation_context','calendar_sensitivity',
                 'card_scope_sensitivity','possible_explanations','unverified_factors','interpretation')
INPUT_MEMBERS = {
    'monthly':'step7_2/actual_validation/monthly_source_cells.csv',
    'step7_2':'step7_2/regional_evidence.jsonl',
    'step7_3':'step7_3/candidate_evidence.jsonl',
    'calendar':'step7_4_full/calendar/calendar_scenario_comparison.csv',
    'calendar_monthly':'step7_4_full/calendar/group_daily_monthly.csv',
    'synthetic_targets':'step7_4_full/injection/synthetic_targets.csv',
    'injection_manifest':'step7_4_full/injection/manifest.json',
    'calendar_manifest':'step7_4_full/calendar/manifest.json',
    'full_manifest':'step7_4_full/manifest.json',
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def field(row, path):
    for name in path.split('.'):
        row = row[name]
    return row


def keyed(rows, key_field):
    result = {}
    for row in rows:
        key = tuple(row[key_field][name] for name in KEY_FIELDS)
        if key in result:
            raise ValueError('duplicate_observation_key:'+':'.join(key))
        result[key] = row
    return result


def compare_behavior(original, current):
    old, new = keyed(original,'observation_key'), keyed(current,'key')
    if old.keys()!=new.keys():raise ValueError('V3_observation_key_mismatch')
    numeric = exact = categories = quality = 0
    maximum = 0.0
    for key, expected in old.items():
        actual = new[key]
        def equal(a,b,label):
            if a!=b:raise ValueError('V3_value_mismatch:'+':'.join(key)+':'+label)
        for source in ('skt','card_a'):
            for name in NUMERIC_FIELDS:
                a,b = field(expected['sources'][source],name),field(actual['sources'][source],name)
                numeric += 1
                if a is None or b is None:
                    equal(a,b,source+'.'+name)
                    exact += 1
                else:
                    if isinstance(a,bool) or isinstance(b,bool) or not math.isfinite(a) or not math.isfinite(b):
                        raise ValueError('V3_invalid_numeric:'+source+'.'+name)
                    maximum = max(maximum,abs(a-b))
                    exact += a==b
                    if not math.isclose(a,b,rel_tol=1e-12,abs_tol=1e-12):
                        raise ValueError('V3_numeric_mismatch:'+':'.join(key)+':'+source+'.'+name)
            for name in ('unit','direction','robust.status','robust.candidate'):
                equal(field(expected['sources'][source],name),field(actual['sources'][source],name),source+'.'+name)
                categories += 1
        for name in ('rule.candidate','rule.two_month_streak','robust.candidate','robust.status','robust.cutoff'):
            equal(field(expected,name),field(actual,name),name)
            categories += 1
        equal(expected['quality']['status'],actual['quality']['status'],'quality.status')
        equal(expected['quality']['operational_alert_allowed'],actual['operational_alert_allowed'],'operational_alert_allowed')
        equal(set(expected['quality']['hold_reason_codes']),set(actual['quality']['reasons']),'hold_reason_codes')
        quality += 3
    return dict(keys=len(old),numeric_comparisons=numeric,numeric_exact=exact,max_absolute_difference=maximum,
                categorical_comparisons=categories,quality_comparisons=quality,mismatches=0)


def compare_dong(original, current):
    old = {}
    for row in original:
        key = tuple(row[name] for name in ('STD_YM','sgg_code','administrative_dong_code','sex_code','age_code'))
        if key in old:raise ValueError('duplicate_original_dong_key')
        old[key] = row
    new = {}
    for row in current:
        key = tuple(row['key'][name] for name in ('month','region_code','dong_code','sex_code','age_code'))
        if key in new:raise ValueError('duplicate_model_dong_key')
        new[key] = row
    if old.keys()!=new.keys():raise ValueError('V3_dong_key_mismatch')
    changes = 0
    for key, row in new.items():
        source = old[key]
        if float(source['activity'])!=row['activity_daily']:raise ValueError('V3_dong_activity_mismatch')
        if row['card_observation'] is not None or row['quality_status']!='hold' or row['operational_alert_allowed'] is not False:
            raise ValueError('V3_dong_quality_mismatch')
        previous = row['previous_activity_daily']
        if previous is None:
            if source['mom_change_percent'] or source['mom_log_decline']:raise ValueError('V3_dong_unavailable_mismatch')
        else:
            if not math.isclose(float(source['mom_change_percent']),100*(row['activity_daily']/previous-1),rel_tol=1e-12,abs_tol=1e-12):
                raise ValueError('V3_dong_change_mismatch')
            if not math.isclose(float(source['mom_log_decline']),-math.log(row['activity_daily']/previous),rel_tol=1e-12,abs_tol=1e-12):
                raise ValueError('V3_dong_log_change_mismatch')
            changes += 1
    return dict(keys=len(old),activity_exact=len(old),previous_comparisons=changes,mismatches=0)


def audit(bundle, archive, index_root, output=None, expected_archive_sha256=None):
    bundle, archive, index_root = Path(bundle).resolve(),Path(archive).resolve(),Path(index_root).resolve()
    model = verify_bundle(bundle)
    archive_hash = sha(archive)
    if expected_archive_sha256 and archive_hash!=expected_archive_sha256:
        raise ValueError('V3_archive_hash_mismatch')
    references = {}
    def local_reference(name,path,expected,canonical=False):
        raw = path.read_bytes()
        if canonical:raw = raw.replace(b'\r\n',b'\n')
        if digest(raw)!=expected:raise ValueError('V3_upstream_hash_mismatch:'+name)
        references[name] = dict(upstream_relative_path=path.relative_to(index_root).as_posix(),sha256=expected,
                                hash_bytes='LF_canonical' if canonical else 'raw')
    with ZipFile(archive) as z:
        names = z.namelist()
        if len(names)!=len(set(names)):raise ValueError('duplicate_archive_member')
        def read(tail):
            name = PREFIX+tail
            raw = z.read(name)
            references[tail] = dict(zip_member=name,sha256=digest(raw))
            return raw
        def checked(tail,expected):
            raw = read(tail)
            if digest(raw)!=expected:raise ValueError('V3_member_hash_mismatch:'+tail)
            return raw
        def parse(raw):return json.loads(raw.decode('utf-8-sig'))
        manifest = parse(read('step7_5/manifest.json'))
        for name,expected in manifest['output_sha256'].items():checked('step7_5/'+name,expected)
        validation = parse(read('step7_6/validation_manifest.json'))
        checked('step7_5/manifest.json',validation['step7_5_manifest_sha256'])
        checked('step7_5/evidence.jsonl',validation['evidence_sha256'])
        checked('step7_5/dong_skt_monthly.csv',validation['dong_skt_sha256'])
        for name,tail in INPUT_MEMBERS.items():checked(tail,manifest['input_sha256'][name])
        local_reference('dong_skt_upstream',index_root/'v1/outputs/final_index_research/dong_skt_indicators.csv',manifest['input_sha256']['dong_skt'])
        local_reference('step7_5_config',index_root/'v3/behavior/configs/step7_5_evidence_v1.json',manifest['config_sha256'],True)
        local_reference('step7_5_code',index_root/'v3/behavior/src/analyze_step7_full_patterns.py',manifest['code_sha256'],True)
        experiments = {}
        for name in ('injection','calendar'):
            tail = 'step7_4_full/'+name+'/'
            experiment = parse(read(tail+'manifest.json'))
            for file,expected in experiment['output_sha256'].items():checked(tail+file,expected)
            experiments[name] = experiment['summary']
        for name in ('STEP7_3_ANOMALY_COMPARISON.md','STEP7_4_FULL_VALIDATION.md','STEP7_5_FULL_PATTERN_ANALYSIS.md','STEP7_6_HANDOFF_REPORT.md'):
            member = '02_v3_behavior/v3/behavior/reports/'+name
            references['reports/'+name] = dict(zip_member=member,sha256=digest(z.read(member)),role='original_descriptive_report')
        numbered = [(number,line) for number,line in enumerate(read('step7_5/evidence.jsonl').splitlines(),1) if line.strip()]
        lines = [line for number,line in numbered]
        original = [parse(line) for line in lines]
        comparison = compare_behavior(original,read_jsonl(bundle/'behavior_evidence.jsonl'))
        if comparison['keys']!=100:raise ValueError('V3_expected_100_observations')
        dong = list(csv.DictReader(io.StringIO(read('step7_5/dong_skt_monthly.csv').decode('utf-8-sig'))))
        dong_comparison = compare_dong(dong,read_jsonl(bundle/'dong_skt_evidence.jsonl'))
        sidecar = [dict(contract_version='original_v3_explanation_reference_v1',key=row['observation_key'],
                        source_evidence_member=PREFIX+'step7_5/evidence.jsonl',source_line=numbered[line-1][0],
                        source_record_sha256=digest(raw),source_archive_sha256=archive_hash,
                        reference_role='verified_original_explanation_for_matching_observed_values',
                        original_details={name:row[name] for name in DETAIL_FIELDS},
                        validation_references=['step7_4_full/injection/manifest.json','step7_4_full/calendar/manifest.json','step7_5/manifest.json','step7_6/validation_manifest.json'],
                        synthetic_validation_rerun=False,quality_status='hold',operational_alert_allowed=False)
                   for line,(raw,row) in enumerate(zip(lines,original),1)]
    report = dict(status='passed',model_run_id=model['run_id'],original_V3=comparison,dong=dong_comparison,
                  original_summary=manifest['summary'],original_experiment_summaries=experiments,
                  verified_reference_count=len(references),explanation_references=len(sidecar),
                  synthetic_validation_rerun=False,full_raw_input_repeat=False,
                  original_all_models_equivalence_claimed=False,joint_freeze_approved=False,
                  quality_status='hold',operational_alert_allowed=False)
    output = Path(output).resolve() if output else FINAL/'outputs'/datetime.now(timezone.utc).strftime('v3_review_%Y%m%d_%H%M%S_%f')
    if output.is_relative_to(bundle):raise ValueError('review_output_must_be_separate')
    output.mkdir(parents=True,exist_ok=False)
    write_jsonl(output/'original_v3_explanation_references.jsonl',sidecar)
    dump(output/'validation_report.json',report)
    dump(output/'manifest.json',dict(contract_version='original_v3_review_v1',status='completed',
         source_archive_name=archive.name,source_archive_sha256=archive_hash,
         model_run_id=model['run_id'],model_manifest_sha256=sha(bundle/'manifest.json'),
         reviewer_code_sha256=sha(Path(__file__)),references=references,
         outputs={name:sha(output/name) for name in ('original_v3_explanation_references.jsonl','validation_report.json')},
         interpretation='Verified original research references; no new synthetic experiment or isolation accuracy claim.',
         joint_freeze_approved=False,operational_alert_allowed=False))
    return output,report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--bundle',required=True)
    parser.add_argument('--archive',required=True)
    parser.add_argument('--index-root',required=True)
    parser.add_argument('--output')
    parser.add_argument('--expected-archive-sha256')
    args=parser.parse_args()
    output,report=audit(args.bundle,args.archive,args.index_root,args.output,args.expected_archive_sha256)
    print(json.dumps(dict(output=str(output),validation=report),ensure_ascii=False,allow_nan=False))


if __name__=='__main__':main()
