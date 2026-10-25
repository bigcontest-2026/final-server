"""Real model evidence -> explicit research rule -> ERD welfare candidates.

No I5 threshold is assumed. Context mode uses available domain scores as
explanatory references, not high/low classifications or individual needs.
"""
import argparse
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

from .welfare_erd import ERDWelfareDB, review_candidates
from .welfare_connection import remote_configured
from .signal_rules import classify_activity, previous_month
from .welfare_support_mapping import CATEGORY_LABELS

RULE_VERSION = 'research-context-v1'
AGES = {'20G': (20,29), '30G': (30,39), '40G': (40,49),
        '50G': (50,59), '60GU': (60,float('inf'))}


def build_intent(i5, behavior, *, research_context=False, previous_behavior=None):
    assessment = classify_activity(behavior, previous_behavior)
    result = {'status':'held', 'rule_version':RULE_VERSION, 'rule_status':'draft',
              'mode':'context_only' if research_context else 'unconfigured',
              'i5_domains':[], 'evidence':[], 'reasons':[], 'route':'insufficient_evidence',
              'activity_assessment':assessment}
    if not research_context:
        result['reasons']=['I5 임계값 미확정: 연구용 context 모드를 명시해야 실행합니다.']
        return result
    if i5 is None or behavior is None:
        result['reasons']=['동일 지역·월의 I5 또는 활동 근거가 없습니다.']
        return result
    key=behavior['key']
    if (i5['region_code'],i5['month']) != (key['region_code'],key['month']):
        raise ValueError('I5 and activity region/month mismatch')
    if i5.get('spatial_unit')!='sgg' or behavior.get('spatial_unit')!='sgg':
        raise ValueError('only explicit SGG context join is supported')
    if assessment['category']=='assessment_hold':
        result['reasons']=assessment['reasons']
        return result
    joint=assessment['signals']['joint_decline']
    robust=assessment['signals']['robust_candidate']
    streak=assessment['signals']['two_month_streak']
    declines=[source for source,data in assessment['sources'].items() if data['threshold_met'] is True]
    if joint:
        result['route']='focused_investigation' if assessment['category']=='priority_review' else 'common_factor_review'
        proposed=['P','C','U']
        if 'card_a' in declines:
            proposed.append('E')
    elif declines:
        result['route']='single_source_review'
        proposed=['O','P'] if 'skt' in declines else ['E']
    else:
        result.update(status='no_signal',route='no_active_signal',reasons=['5% 이상 감소 기준에 해당하는 활동 원천이 없어 자동 검색하지 않습니다.'])
        return result
    for domain in proposed:
        score=i5.get('domain_scores',{}).get(domain)
        if not isinstance(score,(int,float)) or isinstance(score,bool) or not math.isfinite(score) or not 0<=score<=100:
            continue
        result['i5_domains'].append(domain)
        result['evidence'].append({'domain':domain,'score':score,'i5_record_id':i5['record_id'],
                                  'behavior_record_id':behavior['record_id']})
    result['status']='selected' if result['i5_domains'] else 'held'
    result['signals']={'joint_decline':joint,'robust':robust,'two_month_streak':streak,'declining_sources':declines}
    result['reasons']=[*assessment['reasons'], '제안 규칙: 활동신호의 조사 경로와 관련 I5 영역 참고값으로 사업을 탐색합니다.',
                       'I5가 높다고 판정하지 않았으며, 소비 감소를 소득 부족으로 확정하지 않습니다.']
    return result


def run(*, i5_bundle, activity_bundle, db_path, publication_id, region_code, month,
        sex_code, age_code, as_of, research_context=False, allow_synthetic=False,
        confirmed_needs=()):
    # Use model public readers: completion, real-data marker and output hashes are verified.
    from final_model.src.survey_area_index import query as i5_query
    from final_model.src.query import query as activity_query
    from datetime import date
    date.fromisoformat(as_of)
    if age_code not in AGES:
        raise ValueError('unsupported age group')
    if (not isinstance(confirmed_needs, (list, tuple))
            or any(not isinstance(need, str) or need not in CATEGORY_LABELS for need in confirmed_needs)):
        raise ValueError('confirmed_needs must contain known category codes')
    confirmed_needs=sorted(set(confirmed_needs))
    survey=i5_query(i5_bundle,level='sgg',region_code=region_code,month=month,limit=2)
    activity=activity_query(activity_bundle,region_code=region_code,month=month,
                            sex_code=sex_code,age_code=age_code,limit=2)
    prior_month=previous_month(month)
    previous=activity_query(activity_bundle,region_code=region_code,month=prior_month,
                            sex_code=sex_code,age_code=age_code,limit=2) if prior_month else {'total_matched':0,'records':[]}
    if survey['total_matched']>1 or activity['total_matched']>1 or previous['total_matched']>1:
        raise ValueError('ambiguous model evidence')
    i5=survey['records'][0] if survey['records'] else None
    behavior=activity['records'][0]['behavior'] if activity['records'] else None
    historical=activity['records'][0].get('historical_reference') if activity['records'] else None
    previous_behavior=previous['records'][0]['behavior'] if previous['records'] else None
    intent=build_intent(i5,behavior,research_context=research_context,previous_behavior=previous_behavior)
    result={'contract_version':'welfare_research_pipeline_v1','request':{
        'region_code':region_code,'month':month,'sex_code':sex_code,'age_code':age_code,'support_as_of':as_of,
        'confirmed_needs':confirmed_needs},
        'mapping':intent,'quality_status':'hold','operational_alert_allowed':False,
        'model_inputs_synthetic':False,'i5':i5,'activity':behavior,
        'availability':{'i5':survey['availability'],'activity':activity['availability']},
        'provenance':{'i5_bundle':str(Path(i5_bundle).resolve()),
                      'activity_bundle':str(Path(activity_bundle).resolve()),'activity_run_id':activity['run_id']},
        'limitations':['지역 I5는 전국 성·연령 평균을 지역 인구구성으로 가중한 참고값입니다.',
            '활동자료는 성·연령 집단의 장소 유동·가맹점 거래입니다. 지역 거주자의 개인 상태가 아닙니다.',
            '지역 전체 I5와 활동 집단을 맥락으로 연결했으며 동일 성·연령 집단 점수로 결합하지 않았습니다.',
            '관측 월과 사업 검토 기준일은 별개입니다. 과거 활동신호를 현재 위험으로 단정하지 않습니다.',
            '연구·시연용 제안 규칙이며 운영 경보·개인 자격 확정에 사용하지 않습니다.'],
        'recommendations':[],'excluded':[],'held':[]}
    if intent['status']!='selected':
        if not confirmed_needs or not research_context:
            result['status']=intent['status']
            return result
    if remote_configured():
        # The remote catalog uses provided services, not its broad target tags.
        from .welfare_postgres import PostgresWelfareDB
        from .welfare_support_mapping import category_plan, review_support_candidates
        from .welfare_support_context import load_support_context
        # Explicit confirmed-need searches must not add observation-led topics.
        plan=category_plan(() if confirmed_needs else intent['i5_domains'], confirmed_needs=confirmed_needs)
        if confirmed_needs:
            plan['selection_basis']='confirmed_needs_only'
        result['mapping'].update(category_mapping=plan, support_rule_version=plan['rule_version'])
        if confirmed_needs:
            if intent['status']=='no_signal':
                result['mapping']['reasons']=['활동 변화에 따른 자동 탐색 기준에는 해당하지 않습니다.']
            result['mapping'].update(status='selected',route='confirmed_support_need')
            result['mapping']['reasons'].append('담당자가 확인한 지원 필요에 따라 사업을 탐색합니다.')
        result['support_context']=load_support_context(activity_bundle,behavior,
            intent['activity_assessment'],historical_reference=historical,
            scope={'region_code':region_code,'month':month,'sex_code':sex_code,'age_code':age_code})
        db=PostgresWelfareDB(publication_id)
        programs=[]
        offset=0
        corpus_identity=None
        seen_versions=set()
        while True:
            page_limit=500-len(programs)
            candidates=db.search_support(category_ids=plan['selected_categories'],
                as_of=date.fromisoformat(as_of),region_code=region_code,offset=offset,limit=page_limit)
            if candidates.get('status')!='ok':
                result['status']='held'
                result['mapping_status']=candidates.get('mapping_status','unconfigured')
                result['mapping'].update(status='held')
                result['mapping']['reasons'].append(candidates.get('mapping_reason','사업 연결 규칙과 근거 등록 상태를 확인해 주세요. 복지 목록에서 실제 사업을 검색할 수 있습니다.'))
                result.update(data_source='supabase',welfare_data_synthetic=False,
                    coverage=candidates.get('coverage',{}))
                return result
            identity=(tuple(sorted(candidates.get('publication_ids',[]))),
                candidates.get('rule_version'),candidates.get('run_id'),candidates.get('corpus_sha256'))
            if corpus_identity is not None and identity!=corpus_identity:
                raise ValueError('welfare_publication_changed_during_search')
            corpus_identity=identity
            if len(candidates['programs'])>page_limit:
                raise ValueError('welfare_candidate_page_exceeds_limit')
            versions=[program['version_id'] for program in candidates['programs']]
            if len(versions)!=len(set(versions)) or seen_versions.intersection(versions):
                raise ValueError('duplicate_welfare_candidate_version')
            seen_versions.update(versions)
            programs.extend(candidates['programs'])
            next_offset=candidates.get('next_offset')
            if next_offset is None or len(programs)>=500:
                break
            if type(next_offset) is not int or next_offset<=offset:
                raise ValueError('invalid_welfare_pagination')
            offset=next_offset
        reviewed=review_support_candidates(programs,plan,
            region_context={'region_code':region_code},age_range=AGES[age_code],
            sex=sex_code,as_of=as_of)
        for program in reviewed:
            domains={domain for category in plan['categories']
                     if any(link.get('category_code',link.get('category_id'))==category['category_code']
                            for link in program.get('service_links',program.get('support_links',[])))
                     for domain in category['matched_domains']}
            program['observation_reasons']=[e for e in intent['evidence'] if e['domain'] in domains]
            program['mapping_rule_version']=plan['rule_version']
            program['unconfirmed_conditions']=list(dict.fromkeys([
                *program['unconfirmed_conditions'],'실제 지원 필요 및 활동 감소 원인']))
            result[{'needs_confirmation':'recommendations','excluded':'excluded',
                    'source_hold':'held'}[program['status']]].append(program)
        result.update(status='completed',welfare_data_synthetic=False,data_source='supabase',
            mapping_status='configured_research',publication_id=candidates.get('publication_id'),
            publication_ids=candidates.get('publication_ids',[]),coverage={
                **candidates.get('coverage',{}),'reviewed':len(programs),
                'truncated':next_offset is not None})
        result['mapping']['source_mapping_run']={key:candidates.get(key)
            for key in ('run_id','rule_version','corpus_sha256')}
        result['limitations'].extend([
            '사업 원문에서 추출한 제공 서비스와 지원 분야의 연구용 연결입니다. 수동 검토 완료가 아닙니다.',
            '인구·세대 구성과 과거 카드 비교는 배경 근거이며 새로운 위험점수나 독립 신호를 더하지 않습니다.',
            '담당기관 관할 지역은 사업 탐색 범위이며 신청자의 거주 자격은 원문에서 별도로 확인합니다.'])
        return result
    else:
        if confirmed_needs:
            # Local ERD category IDs belong to their source catalogs; they do
            # not implement the nine offered-service categories used above.
            # Never silently replace a confirmed need with an I5-domain search.
            reason = ('로컬 복지 자료에는 확인된 지원 필요를 9개 복지 분야의 제공서비스로 연결하는 기준이 없어 '
                      '탐색을 보류합니다. 요청한 지원 필요는 유지하며, 검색되지 않았다는 이유로 사업 부재나 지원 공백을 확정하지 않습니다.')
            result['mapping'].update(status='held', route='confirmed_support_need_unavailable',
                                     reasons=[*intent['reasons'], reason])
            result['limitations'].append(reason)
            result.update(status='held', mapping_status='confirmed_needs_unsupported', data_source='sqlite')
            return result
        db=ERDWelfareDB(db_path,publication_id,allow_synthetic=allow_synthetic)
    candidates=db.search(i5_domains=intent['i5_domains'])
    if candidates.get('status') == 'mapping_required':
        reason = '배포된 복지 DB의 9개 분류와 I5 영역을 연결하는 검토된 기준이 없어 자동 추천을 보류합니다. 복지 목록에서 실제 사업을 검색할 수 있습니다.'
        result['mapping'] = {**intent, 'status':'held', 'reasons':[*intent['reasons'], reason]}
        result['limitations'].append(reason)
        result.update(status='held', welfare_data_synthetic=False, data_source='supabase',
                      mapping_status='unconfigured', publication_id=candidates.get('publication_id'),
                      publication_ids=candidates.get('publication_ids', []), coverage=candidates['coverage'])
        return result
    # Exact same code system only. No name/prefix or automatic 10->5 digit conversion.
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro',uri=True)) as con:
        regions={r[0] for r in con.execute('SELECT region_id FROM administrative_regions WHERE code_system=? AND code=?',('KR_SGG',region_code))}
    if not regions:
        # Unknown mapping must not be interpreted as known regional mismatch.
        for p in candidates['programs']:
            if any(r['scope_type']=='specific' for r in p['regions']):
                p['regions']=[{'scope_type':'unknown','region_id':None}]
    reviewed=review_candidates(candidates,region_ids=regions,age_range=AGES[age_code],as_of=as_of)
    for p in reviewed['programs']:
        matched={r['domain'] for r in p['search_reasons'] if r['type']=='i5_link'}
        p['observation_reasons']=[e for e in intent['evidence'] if e['domain'] in matched]
        p['mapping_rule_version']=RULE_VERSION
        p['unconfirmed_conditions'].append('실제 지원 필요 및 활동 감소 원인')
        result[{'needs_confirmation':'recommendations','excluded':'excluded','source_hold':'held'}[p['status']]].append(p)
    result.update(status='completed',welfare_data_synthetic=reviewed['is_synthetic'],
                  publication_id=publication_id,coverage=reviewed['coverage'],region_mapping_verified=bool(regions))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('i5-bundle','activity-bundle','db-path','publication-id','region-code','month','sex-code','age-code','as-of','output'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--research-context',action='store_true')
    parser.add_argument('--allow-synthetic',action='store_true')
    args=vars(parser.parse_args())
    output=Path(args.pop('output'))
    result=run(**args)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps({'status':result['status'],'domains':result['mapping']['i5_domains'],
                      **{k:len(result[k]) for k in ('recommendations','excluded','held')}},ensure_ascii=False))


if __name__=='__main__':
    main()
