"""Read-only research recommendation API using the agent's shared graph."""
import logging
import os
import sys
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo
from fastapi import APIRouter, HTTPException, Query, Request
from .config import Settings
from .final_model import FinalModel
from .final_model_api import Region, Month, Sex, Age

logger=logging.getLogger(__name__)

DOMAIN_LABELS = {'E': '경제', 'P': '참여', 'O': '외출', 'C': '교류', 'U': '지원 관계'}
PERIOD_LABELS = {'ongoing': '사업 기간 중', 'upcoming': '시작 전', 'ended': '기간 종료', 'unknown': '기간 확인 필요'}


def _period_status(program, as_of):
    """A date-range label, never an eligibility or current recruitment decision."""
    start = date.fromisoformat(program['start_date']) if program['start_date'] else None
    end = date.fromisoformat(program['end_date']) if program['end_date'] else None
    if start and end and start > end:
        return 'unknown'
    if end and as_of > end:
        return 'ended'
    if start and as_of < start:
        return 'upcoming'
    return 'ongoing' if start and end else 'unknown'


def _read_catalog(path, publication_id, allow_synthetic, as_of):
    """Read every member of one published corpus without recommendation filtering."""
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        publication = db.execute('SELECT * FROM publications WHERE publication_id=?', (publication_id,)).fetchone()
        if publication is None or publication['status'] != 'published':
            raise ValueError('publication unavailable')
        synthetic = bool(publication['is_synthetic'])
        if synthetic and not allow_synthetic:
            raise ValueError('synthetic corpus requires opt-in')
        rows = db.execute('''SELECT v.*, s.source_id, s.name AS source_name
            FROM publication_members m JOIN program_versions v ON v.version_id=m.version_id
            JOIN programs p ON p.program_id=v.program_id JOIN sources s ON s.source_id=p.source_id
            WHERE m.publication_id=? ORDER BY v.title, v.version_id''', (publication_id,)).fetchall()
        programs = []
        for row in rows:
            program = {key: row[key] for key in (
                'version_id', 'program_id', 'title', 'support_content', 'application_status',
                'start_date', 'end_date', 'source_url', 'content_review')}
            program.update(source={'source_id': row['source_id'], 'name': row['source_name']},
                           source_verified=bool(row['source_verified']), is_synthetic=synthetic,
                           period_status=_period_status(program, as_of))
            vid = row['version_id']
            program['source_categories'] = [dict(category) for category in db.execute('''
                SELECT c.category_id,c.source_id,c.source_code,c.label FROM source_categories c
                JOIN program_source_categories p ON p.category_id=c.category_id
                WHERE p.version_id=? ORDER BY c.label,c.category_id''', (vid,))]
            program['i5_domains'] = [{'code': link['domain'], 'label': DOMAIN_LABELS[link['domain']],
                                     'review_status': link['review_status'], 'reason': link['reason']}
                                    for link in db.execute('SELECT * FROM program_i5_domains WHERE version_id=? ORDER BY domain', (vid,))]
            program['regions'] = [dict(region) for region in db.execute('''
                SELECT p.scope_type,p.region_id,r.code,r.name FROM program_regions p
                LEFT JOIN administrative_regions r ON r.region_id=p.region_id
                WHERE p.version_id=? ORDER BY p.scope_type,r.code''', (vid,))]
            program['conditions'] = [{'kind': rule['kind'], 'value': json.loads(rule['value_json']),
                                      'evidence_id': rule['evidence_id']}
                                     for rule in db.execute('SELECT * FROM eligibility_rules WHERE version_id=? ORDER BY kind,evidence_id', (vid,))]
            programs.append(program)
        region_hierarchy = {row['region_id']: dict(row) for row in db.execute('SELECT * FROM administrative_regions')}
    return synthetic, programs, region_hierarchy


def _catalog_response(programs, *, publication_id, synthetic, as_of, region_hierarchy,
                      q, category_id, domain, region_code, status, offset, limit, region_level=None):
    from donghaeng_agent.welfare_postgres import catalog_region_facets, catalog_region_ranks
    categories, regions = {}, {}
    category_counts, region_counts, domain_counts, status_counts = Counter(), Counter(), Counter(), Counter()
    for program in programs:
        for category in program['source_categories']:
            key = category['category_id']
            categories[key] = {'category_id': key, 'label': category['label']}
            category_counts[key] += 1
        for region in {r['region_id']: r for r in program['regions'] if r['region_id']}.values():
            key = region['region_id']
            regions[key] = {field: region[field] for field in ('region_id', 'code', 'name')}
            region_counts[key] += 1
        domain_counts.update({link['code'] for link in program['i5_domains']})
        status_counts[program['period_status']] += 1

    # Explicit administrative parent links, never an inferred code-prefix hierarchy.
    allowed_regions = set()
    if region_code:
        selected = next((r for r in region_hierarchy.values() if r['code'] == region_code and r['code_system'] == 'KR_SGG'), None)
        while selected and selected['region_id'] not in allowed_regions:
            allowed_regions.add(selected['region_id'])
            selected = region_hierarchy.get(selected['parent_id'])
    query = q.strip().casefold()
    explicit = region_level is not None
    ranked_ids, search_order = [], []
    level_names = {'KR_SIDO': 'sido', 'KR_SGG': 'sigungu',
                   'KR_DONG': 'eupmyeondong', 'KR_ADMIN_DONG': 'eupmyeondong'}
    facets = catalog_region_facets([{
        'region_id': row['region_id'], 'parent_region_id': row['parent_id'],
        'region_code': row['code'], 'region_name': row['name'],
        'region_level': level_names.get(row['code_system']),
        'sgg_code': row['code'] if row['code_system'] == 'KR_SGG' else None,
        'sido_code': row['code'] if row['code_system'] == 'KR_SIDO' else None,
    } for row in region_hierarchy.values()])
    if explicit:
        ranked_ids, search_order = catalog_region_ranks(facets, region_level, region_code)
    ranks = dict(ranked_ids)
    matched = []
    for program in programs:
        searchable = ' '.join([program['title'], program['support_content'],
                               *[c['label'] for c in program['source_categories']]])
        if query and query not in searchable.casefold():
            continue
        if category_id and not any(c['category_id'] == category_id for c in program['source_categories']):
            continue
        if domain and not any(link['code'] == domain for link in program['i5_domains']):
            continue
        if not explicit and region_code and not any(r['scope_type'] == 'nationwide' or
                                   r['scope_type'] == 'specific' and r['region_id'] in allowed_regions
                                   for r in program['regions']):
            continue
        if status and program['period_status'] != status:
            continue
        if explicit:
            rank = min((2 if row['scope_type'] == 'nationwide' else ranks.get(row['region_id'], 3)
                        if row['scope_type'] == 'specific' else 3 for row in program['regions']), default=3)
            if rank == 3:
                continue
            program = {**program, 'region_match_rank': rank,
                       'region_match_label': next(item['label'] for item in search_order if item['rank'] == rank)}
        matched.append(program)
    if explicit:
        matched.sort(key=lambda program: (program['region_match_rank'], program['title'], program['version_id']))
    next_offset = offset + limit if offset + limit < len(matched) else None
    return {
        'schema_version': 'welfare_catalog_v1', 'publication_id': publication_id,
        'is_synthetic': synthetic, 'as_of': as_of.isoformat(),
        'total': len(programs), 'matched': len(matched), 'offset': offset, 'limit': limit,
        'next_offset': next_offset, 'programs': matched[offset:offset + limit],
        'categories': [{**value, 'count': category_counts[key]} for key, value in sorted(categories.items())],
        'domains': [{'code': code, 'label': label, 'count': domain_counts[code]} for code, label in DOMAIN_LABELS.items()],
        'regions': facets if explicit else [{**value, 'count': region_counts[key]} for key, value in sorted(regions.items())],
        'available_regions': facets,
        'region_level': region_level, 'region_code': region_code, 'region_search_order': search_order,
        'statuses': [{'code': code, 'label': label, 'count': status_counts[code]} for code, label in PERIOD_LABELS.items()],
    }


def welfare_router(settings: Settings):
    router=APIRouter()
    # Trusted server configuration only; browser never chooses filesystem paths.
    for path in (settings.agent_resource_root/'src',settings.model_root):
        if str(path) not in sys.path:
            sys.path.insert(0,str(path))
    from donghaeng_agent.welfare_connection import remote_configured

    @router.get('/api/model/welfare/catalog')
    def catalog(request: Request, q: Annotated[str, Query(max_length=200)] = '',
                category_id: Annotated[str | None, Query(max_length=100)] = None,
                domain: Literal['E', 'P', 'O', 'C', 'U'] | None = None,
                region_code: Annotated[str | None, Query(pattern=r'^(?:\d{2}|\d{5}|\d{10})$')] = None,
                region_level: Literal['national', 'sido', 'sgg', 'administrative_dong'] | None = None,
                status: Literal['ongoing', 'upcoming', 'ended', 'unknown', 'always',
                                'varies', 'announced', 'not_required'] | None = None,
                as_of: date | None = None,
                offset: Annotated[int, Query(ge=0)] = 0,
                limit: Annotated[int, Query(ge=1, le=500)] = 100,
                view: Literal['full', 'browser'] = 'full'):
        if set(request.query_params) - {'q', 'category_id', 'domain', 'region_code', 'region_level', 'status', 'as_of', 'offset', 'limit', 'view'}:
            raise HTTPException(422, '지원하지 않는 복지 목록 조회 조건입니다.')
        from donghaeng_agent.welfare_postgres import validate_catalog_region
        try:
            validate_catalog_region(region_level, region_code)
        except ValueError as exc:
            raise HTTPException(422, '시도·시군구·읍면동 선택과 지역 코드를 확인해 주세요.') from exc
        if settings.access_mode != 'local_development' and os.getenv('WELFARE_RESEARCH_ENABLED') != 'true':
            raise HTTPException(503, '복지 자료 조회가 활성화되지 않았습니다.')
        publication_id = os.getenv('WELFARE_PUBLICATION_ID') or (None if remote_configured() else 'erd-demo-v2')
        day = as_of or datetime.now(ZoneInfo('Asia/Seoul')).date()
        try:
            if remote_configured():
                from donghaeng_agent.welfare_postgres import PostgresWelfareDB
                return PostgresWelfareDB(publication_id).catalog(
                    as_of=day, q=q, category_id=category_id, domain=domain,
                    region_code=region_code, region_level=region_level, status=status, offset=offset, limit=limit,
                    **({'view': view} if view == 'browser' else {}))
            synthetic, programs, hierarchy = _read_catalog(
                os.getenv('WELFARE_DB_PATH', str(settings.agent_resource_root/'runtime/welfare_demo/welfare_erd_v2.sqlite')),
                publication_id, settings.access_mode == 'local_development' or os.getenv('WELFARE_ALLOW_SYNTHETIC') == 'true', day)
            return _catalog_response(programs, publication_id=publication_id, synthetic=synthetic, as_of=day,
                                     region_hierarchy=hierarchy, q=q, category_id=category_id, domain=domain,
                                     region_code=region_code, region_level=region_level, status=status, offset=offset, limit=limit)
        except Exception as exc:
            logger.exception('Welfare catalog unavailable')
            raise HTTPException(503, detail={'code': 'welfare_catalog_unavailable',
                                          'message': '복지 목록을 불러오지 못했습니다. 잠시 후 다시 확인해 주세요.'}) from exc

    @router.get('/api/model/welfare/regions')
    def regions(request: Request, as_of: date | None = None):
        if set(request.query_params) - {'as_of'}:
            raise HTTPException(422, '지원하지 않는 지역 목록 조회 조건입니다.')
        if settings.access_mode != 'local_development' and os.getenv('WELFARE_RESEARCH_ENABLED') != 'true':
            raise HTTPException(503, '복지 자료 조회가 활성화되지 않았습니다.')
        day = as_of or datetime.now(ZoneInfo('Asia/Seoul')).date()
        try:
            if remote_configured():
                from donghaeng_agent.welfare_postgres import PostgresWelfareDB
                values = PostgresWelfareDB().catalog_regions(as_of=day)
            else:
                values = catalog(request, as_of=day)['available_regions']
            return {'as_of': day.isoformat(), 'regions': values}
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception('Welfare regions unavailable')
            raise HTTPException(503, detail={'code': 'welfare_regions_unavailable',
                'message': '지역 목록을 불러오지 못했습니다. 잠시 후 다시 확인해 주세요.'}) from exc

    @router.get('/api/model/welfare')
    def welfare(request: Request, region_code: Region, month: Month, sex_code: Sex, age_code: Age,
                as_of: date | None = None,
                confirmed_needs: Annotated[list[Literal['social_connection', 'isolation_outreach',
                    'income_support', 'daily_living', 'employment', 'mental_health', 'health_care',
                    'housing', 'family_support']], Query(max_length=9)] = []):
        if set(request.query_params)-{'region_code','month','sex_code','age_code','as_of','confirmed_needs'}:
            raise HTTPException(422,'지원하지 않는 추천 조회 조건입니다.')
        if settings.access_mode!='local_development' and os.getenv('WELFARE_RESEARCH_ENABLED')!='true':
            raise HTTPException(503,'연구용 추천이 활성화되지 않았습니다.')
        try:
            from donghaeng_agent.welfare_graph import build_welfare_graph
            model=FinalModel(settings)
            args=dict(i5_bundle=model.bundle_path('survey'),activity_bundle=model.bundle_path('activity'),
                db_path=os.getenv('WELFARE_DB_PATH',str(settings.agent_resource_root/'runtime/welfare_demo/welfare_erd_v2.sqlite')),
                publication_id=os.getenv('WELFARE_PUBLICATION_ID') or (None if remote_configured() else 'erd-demo-v2'),
                region_code=region_code,month=month,sex_code=sex_code,age_code=age_code,
                as_of=(as_of or datetime.now(ZoneInfo('Asia/Seoul')).date()).isoformat(),
                research_context=True,allow_synthetic=settings.access_mode=='local_development' or os.getenv('WELFARE_ALLOW_SYNTHETIC')=='true')
            if confirmed_needs:
                args['confirmed_needs']=list(dict.fromkeys(confirmed_needs))
            result=build_welfare_graph().invoke({'request':args})['result']
            # Echo the validated query for every storage adapter, including held
            # responses, so the caller can reject candidates from another scope.
            result.setdefault('request',{}).update(
                region_code=region_code,month=month,sex_code=sex_code,age_code=age_code,
                support_as_of=args['as_of'],confirmed_needs=args.get('confirmed_needs',[]))
            # Keep server filesystem/provenance input paths out of the HTTP response.
            result.pop('provenance',None)
            if result.get('activity'):
                result['activity']={k:v for k,v in result['activity'].items() if k!='provenance'}
            return result
        except Exception as exc:
            logger.exception('Welfare recommendation unavailable')
            raise HTTPException(503,detail={'code':'welfare_unavailable','message':'추천 자료 또는 실행 환경을 확인해 주세요.'}) from exc
    return router
