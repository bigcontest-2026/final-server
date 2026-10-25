"""Read-only welfare catalog and evidence-backed research service search.

The remote database contains internal nine-category tags and administrative
jurisdictions. Neither is treated as an approved I5 mapping or personal eligibility.
"""
import re
import json
from hashlib import sha256
from collections import defaultdict
from datetime import date, datetime
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from .welfare_connection import WelfareDatabaseError, postgres_connection, configured_schema
from .welfare_catalog_cache import CATALOG_CACHE, catalog_cache_namespace
from .welfare_catalog_browser import browser_page_query


DOMAIN_LABELS = {'E': '경제', 'P': '참여', 'O': '외출', 'C': '교류', 'U': '지원 관계'}
PERIOD_LABELS = {'always': '상시 신청', 'ongoing': '신청기간 내',
                 'upcoming': '신청기간 전', 'ended': '신청기간 종료',
                 'varies': '기관별 일정 상이', 'announced': '신청 일정 안내',
                 'not_required': '별도 신청 없음', 'unknown': '신청 기간 미기재'}
MAPPING_REASON = '배포된 DB의 내부 분류와 I5 영역을 연결하는 승인 규칙이 없어 자동 추천을 보류합니다.'
SUPPORT_MAPPING_REASON = '지원내용의 명시된 제공서비스를 연구용 규칙으로 연결했습니다. 개인 지원 필요·자격·현재 접수와 추출 근거는 별도 확인이 필요합니다.'


def support_rule_definition():
    """Fingerprint extraction behavior as well as the declarative patterns."""
    import inspect
    from .welfare_support_mapping import RULE_DEFINITION, extract_program_services, _sentences
    implementation = inspect.getsource(extract_program_services) + inspect.getsource(_sentences)
    return {**RULE_DEFINITION, 'extractor_implementation_sha256':sha256(implementation.encode()).hexdigest()}


def validate_catalog_region(region_level, region_code):
    """Keep the old district filter while making explicit scopes unambiguous."""
    expected = {None: 5, 'national': None, 'sido': 2, 'sgg': 5, 'administrative_dong': 10}
    if region_level not in expected:
        raise ValueError('invalid region level')
    if region_level == 'national':
        if region_code is not None:
            raise ValueError('national scope cannot have a region code')
    elif region_level is not None and not region_code:
        raise ValueError('region code is required')
    if region_code is not None and not re.fullmatch(r'\d{' + str(expected[region_level]) + '}', region_code):
        raise ValueError('invalid region code')


def catalog_region_facets(rows):
    """Resolve region labels and parents from the stored tree, never code prefixes."""
    tree = {str(row['region_id']): row for row in rows}
    facets = []
    for row in rows:
        # Exact SGG5 mappings also cover non-autonomous city districts whose
        # native level label may differ from "sigungu".
        level = 'sgg' if row.get('sgg_code') else (
            'administrative_dong' if row['region_level'] == 'eupmyeondong' else None)
        code = row.get('sgg_code') if level == 'sgg' else row['region_code']
        if not level or not code or not re.fullmatch(r'\d{5}' if level == 'sgg' else r'\d{10}', code):
            continue
        ancestor = tree.get(str(row['parent_region_id']))
        visited, ancestor_codes, parent_code, province_code, province_name = set(), [], None, None, None
        province_region_id = None
        while ancestor and str(ancestor['region_id']) not in visited:
            visited.add(str(ancestor['region_id']))
            if ancestor.get('sgg_code'):
                ancestor_codes.append(ancestor['sgg_code'])
                if parent_code is None:
                    parent_code = ancestor['sgg_code']
            if ancestor.get('sido_code'):
                province_code, province_name = ancestor['sido_code'], ancestor['region_name']
                province_region_id = str(ancestor['region_id'])
                break
            ancestor = tree.get(str(ancestor['parent_region_id']))
        # A dong without a known district cannot enter district-based browsing.
        if level == 'administrative_dong' and parent_code is None:
            continue
        facets.append({'region_id': str(row['region_id']), 'code': code, 'name': row['region_name'],
            'level': level, 'parent_code': parent_code or province_code,
            'ancestor_codes': ancestor_codes,
            'province_code': province_code, 'province_name': province_name,
            'province_region_id': province_region_id})
    return sorted(facets, key=lambda row: (row['code'], row['region_id']))


def catalog_region_ranks(facets, region_level, region_code):
    """Return ordered native IDs and public scope labels for one explicit search."""
    if region_level == 'national':
        return [], [{'level': 'national', 'label': '전국', 'rank': 2}]
    if region_level == 'sido':
        # A province is resolved from exact SIDO2 mappings reached through the
        # stored parent tree, never inferred from a district/dong code prefix.
        related = [row for row in facets
                   if row.get('province_code') == region_code and row.get('province_region_id')]
        province_ids = list(dict.fromkeys(row['province_region_id'] for row in related))
        if not province_ids:
            return [], [{'level': 'national', 'label': '전국', 'rank': 2}]
        ranks = [(key, 0) for key in province_ids]
        ranks += [(row['region_id'], 1) for row in related if row['region_id'] not in province_ids]
        return ranks, [
            {'level': 'sido', 'label': related[0]['province_name'], 'rank': 0},
            {'level': 'local', 'label': '소속 시군구·읍면동', 'rank': 1},
            {'level': 'national', 'label': '전국', 'rank': 2},
        ]
    selected = next((row for row in facets if row['code'] == region_code and row['level'] == region_level), None)
    if not selected:
        return [], [{'level': 'national', 'label': '전국', 'rank': 2}]
    if region_level == 'sgg':
        local = [selected]
        related = [row for row in facets if row['level'] == 'administrative_dong'
                   and (row['parent_code'] == region_code or region_code in row.get('ancestor_codes', []))]
    else:
        local = [selected]
        related = [row for row in facets if row['level'] == 'sgg' and row['code'] == selected['parent_code']]
    ranks = [(row['region_id'], 0) for row in local] + [(row['region_id'], 1) for row in related]
    labels = [{'level': region_level, 'label': selected['name'], 'rank': 0},
              {'level': 'administrative_dong' if region_level == 'sgg' else 'sgg',
               'label': '소속 읍면동' if region_level == 'sgg' else (related[0]['name'] if related else '소속 시군구'), 'rank': 1},
              {'level': 'national', 'label': '전국', 'rank': 2}]
    return ranks, labels

# Shared with the read-only SQL regression checks.
PERIOD_STATUS_SQL = '''CASE
          WHEN v.application_start > v.application_end THEN 'announced'
          WHEN v.application_end < day.as_of THEN 'ended'
          WHEN v.application_start > day.as_of THEN 'upcoming'
          WHEN v.application_start IS NOT NULL AND v.application_end IS NOT NULL THEN 'ongoing'
          WHEN compact.period IN ('상시신청', '상시접수', '상시', '연중', '연중신청', '연중접수', '연중상시', '연중상시신청') THEN 'always'
          WHEN compact.period IN ('접수기관별상이', '신청기관별상이', '기관별상이') THEN 'varies'
          WHEN compact.period IN ('별도의신청절차가없음', '별도신청절차없음', '신청불필요', '신청불요', '개인신청절차없음') THEN 'not_required'
          WHEN schedule.application_period_text IS NOT NULL
            OR v.application_start IS NOT NULL OR v.application_end IS NOT NULL THEN 'announced'
          ELSE 'unknown'
        END'''

APPLICATION_PERIOD_JOINS = r'''    -- The source's canonical deadline field takes precedence over a period
    -- mentioned inside 신청방법. Extract the tail, then stop at the next known
    -- field (including inline local-source headings); times such as 09:00 stay.
    CROSS JOIN LATERAL (SELECT COALESCE(
        substring(v.application_text FROM '(?:^|\n)신청기한[[:blank:]]*[:：][[:blank:]]*(.*)'),
        substring(v.application_text FROM '(?:신청[[:blank:]]*(?:기간|기한)|접수[[:blank:]]*기간)[[:blank:]]*[:：][[:blank:]]*(.*)')
    ) AS period_tail) extracted
    CROSS JOIN LATERAL (SELECT NULLIF(btrim(
        (regexp_split_to_array(extracted.period_tail,
            '(?:(?:신청|접수)[[:blank:]]*(?:방법|장소|대상)|구비[[:blank:]]*서류|제출[[:blank:]]*서류|온라인[[:blank:]]*신청|문의(?:처|사항)?)[[:blank:]]*[:：]'))[1],
        E' \t\r\n·○□•'), '') AS application_period_text) schedule
    CROSS JOIN LATERAL (SELECT regexp_replace(schedule.application_period_text,
        '[[:space:].]', '', 'g') AS period) compact'''

# Every query is run in the helper's single read-only repeatable-read transaction.
# Selected publication membership is additionally tied to its owning source.
CORPUS = f'''WITH corpus AS NOT MATERIALIZED (
    SELECT v.program_version_id, v.program_id, v.title, v.summary,
        v.institution, v.support_content, v.target_text, v.selection_text,
        v.application_text, v.application_start, v.application_end,
        schedule.application_period_text,
        v.min_age, v.max_age, v.age_status, v.age_basis_text, v.life_stage_raw,
        v.gender_scope, v.official_categories_raw, v.verification_level,
        p.source_id, p.source_program_id, s.source_name,
        {PERIOD_STATUS_SQL} AS period_status
    FROM publication_members m
    JOIN publications pub ON pub.publication_id=m.publication_id
    JOIN program_versions v ON v.program_version_id=m.program_version_id
    JOIN programs p ON p.program_id=v.program_id AND p.source_id=pub.source_id
    JOIN sources s ON s.source_id=p.source_id
    CROSS JOIN (SELECT %s::date AS as_of) day
{APPLICATION_PERIOD_JOINS}
    WHERE m.publication_id=ANY(%s::uuid[]) AND pub.publication_status='published'
)'''


def _json_value(value):
    """Psycopg already decodes jsonb, including scalar strings; preserve its type."""
    return value


def _serializable(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {key: _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value


def _official_url(value):
    """Allow recorded web links, never credentials, script URLs, or API secrets."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or re.search(r'[\x00-\x20\x7f]', value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        forbidden = {'servicekey', 'apikey', 'api_key', 'access_token', 'token', 'password', 'key'}
        if any(key.casefold() in forbidden for key, _ in parse_qsl(parsed.query)):
            return None
        return value
    except ValueError:
        return None


def _normalize_program(row, tags=(), regions=(), rules=(), links=(), documents=(), support_links=()):
    source_url, source_evidence_id = None, None
    for link in links:
        source_url = _official_url(link['source_quote'])
        if source_url:
            source_evidence_id = str(link['evidence_id'])
            break
    url_kind = None
    if source_url:
        parsed = urlsplit(source_url)
        url_kind = ('official_site' if parsed.path in ('', '/') and not parsed.query
                    and not parsed.fragment else 'official_source')
    program = {
        'version_id': str(row['program_version_id']), 'program_id': str(row['program_id']),
        'title': row['title'], 'support_content': row['support_content'] or '',
        'summary': row['summary'], 'institution': row['institution'],
        'target_text': row['target_text'], 'selection_text': row['selection_text'],
        'application_text': row['application_text'],
        'application_period_text': row.get('application_period_text'),
        'application_period_basis': ('application_text' if row.get('application_period_text')
            else 'structured_dates' if row['application_start'] or row['application_end'] else None),
        'start_date': row['application_start'], 'end_date': row['application_end'],
        'application_start': row['application_start'], 'application_end': row['application_end'],
        'application_status': 'unknown', 'period_status': row['period_status'],
        'date_basis': 'application', 'source_url': source_url, 'source_url_type': url_kind,
        'source_evidence_id': source_evidence_id,
        # Collection time is distinct from the source's own modification date.
        'source_retrieved_at': documents[0]['source_retrieved_at'] if documents else None,
        'source': {'source_id': row['source_id'], 'name': row['source_name'],
                   'source_program_id': row['source_program_id']},
        'source_verified': False, 'content_review': 'unknown', 'is_synthetic': False,
        'verification_level': row['verification_level'],
        'min_age': row['min_age'], 'max_age': row['max_age'], 'age_status': row['age_status'],
        'age_basis_text': row['age_basis_text'], 'life_stage_raw': row['life_stage_raw'],
        'gender_scope': row['gender_scope'],
        'official_categories_raw': _json_value(row['official_categories_raw']),
        'classification_basis': 'internal_tags', 'region_basis': 'administering_jurisdiction',
        'source_categories': [{
            'category_id': tag['category_code'], 'source_id': 'internal',
            'source_code': tag['category_code'], 'label': tag['category_name'],
            'assignment_method': tag['assignment_method'], 'review_status': tag['review_status'],
            'rule_version': tag['rule_version'], 'evidence_id': tag['evidence_id'],
            'description': tag.get('description'), 'source_quote': tag.get('source_quote'),
            'classification_basis': 'internal_tags',
        } for tag in tags],
        'i5_domains': [],
        'support_links': list(support_links),
        'regions': [{
            'scope_type': region['scope_type'], 'region_id': region['region_id'],
            'code': region['region_code'], 'name': region['region_name'],
            'code_system': region['code_system'], 'scope_purpose': region['scope_purpose'],
            'mapping_status': region['mapping_status'],
            'original_region_text': region['original_region_text'],
        } for region in regions],
        'conditions': [{
            'kind': rule['condition_type'], 'operator': rule['operator'],
            'value': _json_value(rule['condition_value']),
            'logic_group': _json_value(rule['logic_group']),
            'review_status': rule['review_status'], 'evidence_id': rule['evidence_id'],
            'source_quote': rule.get('source_quote'),
        } for rule in rules],
    }
    return _serializable(program)


def _filters(*, q, category_id, domain, allowed_regions, region_code, status):
    clauses, params = [], []
    if q.strip():
        # strpos is literal: %, _ and quotes never become SQL wildcard syntax.
        clauses.append('''(strpos(lower(concat_ws(' ', c.title, c.support_content)), lower(%s)) > 0
            OR EXISTS (SELECT 1 FROM program_tags t JOIN categories k USING(category_code)
                WHERE t.program_version_id=c.program_version_id AND t.review_status<>'rejected'
                AND strpos(lower(k.category_name), lower(%s)) > 0))''')
        params.extend([q.strip(), q.strip()])
    if category_id:
        clauses.append('''EXISTS (SELECT 1 FROM program_tags t
            WHERE t.program_version_id=c.program_version_id AND t.category_code=%s
            AND t.review_status<>'rejected')''')
        params.append(category_id)
    if domain:
        # Internal categories are not approved links to I5 domains.
        clauses.append('FALSE')
    if region_code:
        clauses.append('''EXISTS (SELECT 1 FROM program_regions r
            WHERE r.program_version_id=c.program_version_id
              AND r.scope_purpose='administering_jurisdiction'
              AND (r.scope_type='nationwide' OR
                  (r.scope_type='specific' AND r.region_id=ANY(%s::uuid[]))))''')
        params.append(allowed_regions)
    if status:
        clauses.append('c.period_status=%s')
        params.append(status)
    return (' WHERE ' + ' AND '.join(clauses) if clauses else ''), params


class PostgresWelfareDB:
    def __init__(self, publication_id=None):
        self.publication_id = None
        if publication_id:
            try:
                self.publication_id = str(UUID(str(publication_id)))
            except (ValueError, TypeError, AttributeError):
                raise WelfareDatabaseError('invalid_publication', '복지 자료 묶음 ID는 UUID여야 합니다.') from None

    def _publications(self, db):
        if self.publication_id:
            rows = db.execute('''SELECT p.publication_id,p.source_id,p.publication_status,p.published_at,s.source_name
                FROM publications p JOIN sources s USING(source_id)
                WHERE p.publication_id=%s::uuid AND p.publication_status='published'
                ORDER BY p.source_id,p.publication_id''', (self.publication_id,)).fetchall()
        else:
            rows = db.execute('''SELECT p.publication_id,p.source_id,p.publication_status,p.published_at,s.source_name
                FROM active_publications a JOIN publications p
                  ON p.publication_id=a.publication_id AND p.source_id=a.source_id
                JOIN sources s ON s.source_id=p.source_id
                WHERE p.publication_status='published' AND s.is_active
                ORDER BY p.source_id,p.publication_id''').fetchall()
        if not rows:
            raise WelfareDatabaseError('publication_unavailable', '조회 가능한 공개 복지 자료 묶음이 없습니다.')
        return _serializable(rows)

    @staticmethod
    def _support_mapping_status(db, publication_ids):
        """Only an installed matching rule and completed corpus run enable search."""
        schema = configured_schema()
        present = db.execute('''SELECT to_regclass(%s) IS NOT NULL AS rules,
            to_regclass(%s) IS NOT NULL AS runs,to_regclass(%s) IS NOT NULL AS actions''',
            [f'{schema}.support_mapping_rule_sets', f'{schema}.support_mapping_runs',
             f'{schema}.program_support_actions']).fetchone()
        absent = {'mapping_status':'unconfigured','mapping_reason':MAPPING_REASON,'rule_version':None}
        if not isinstance(present, dict) or not all(present.get(key) is True for key in ('rules','runs','actions')):
            return absent
        from .welfare_support_mapping import RULE_VERSION
        definition_hash = sha256(json.dumps(support_rule_definition(), ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), default=str).encode()).hexdigest()
        row = db.execute('''SELECT r.run_id,r.rule_version,r.total_programs,r.mapped_programs,
            r.action_count,r.corpus_sha256,r.completed_at FROM support_mapping_runs r
            JOIN support_mapping_rule_sets s USING(rule_version)
            WHERE r.rule_version=%s AND s.definition_sha256=%s AND s.review_status='research_rule'
              AND r.status='completed' AND r.publication_ids @> %s::uuid[]
              AND r.total_programs=(SELECT count(DISTINCT m.program_version_id)
                  FROM publication_members m WHERE m.publication_id=ANY(r.publication_ids))
            ORDER BY r.completed_at DESC,r.run_id LIMIT 1''',
            (RULE_VERSION,definition_hash,publication_ids)).fetchone()
        if not row:
            return absent
        return {'mapping_status':'configured_research','mapping_reason':SUPPORT_MAPPING_REASON,
                **_serializable(row)}

    @staticmethod
    def _support_links(db, version_ids, rule_version):
        if not version_ids or not rule_version:
            return {}
        rows = db.execute('''SELECT support_evidence_id,program_version_id,category_code,service_action,
            rule_version,rule_id,source_field,source_quote,quote_start,quote_end,source_text_sha256,
            extraction_reason AS reason,review_status FROM program_support_actions
            WHERE program_version_id=ANY(%s::uuid[]) AND rule_version=%s AND review_status<>'rejected'
            ORDER BY category_code,service_action,support_evidence_id''', (version_ids,rule_version)).fetchall()
        grouped = defaultdict(list)
        for row in rows:
            item = _serializable(row)
            item['category_id'] = item['category_code']
            item['evidence_id'] = 'support:' + item['support_evidence_id']
            item['content_hash'] = item['source_text_sha256']
            grouped[str(row['program_version_id'])].append(item)
        return grouped

    def support_mapping_status(self):
        with postgres_connection() as db:
            publications = self._publications(db)
            return self._support_mapping_status(db, [row['publication_id'] for row in publications])

    def search_support(self, *, category_ids, service_actions=(), as_of, region_code=None,
                       region_level=None, offset=0, limit=100):
        """Page through explicit service evidence; I5 alone never selects programs."""
        from .welfare_mapping import TAGS
        categories = list(dict.fromkeys(category_ids))
        actions = sorted(set(service_actions))
        if not categories or not set(categories) <= TAGS:
            raise ValueError('invalid support categories')
        if any(not isinstance(value,str) or not 1<=len(value)<=300 or re.search(r'[\x00-\x1f]',value) for value in actions):
            raise ValueError('invalid service actions')
        if not isinstance(as_of,date) or isinstance(as_of,datetime) or offset<0 or not 1<=limit<=500:
            raise ValueError('invalid support search')
        validate_catalog_region(region_level,region_code)
        with postgres_connection() as db:
            publications = self._publications(db)
            ids = [row['publication_id'] for row in publications]
            mapping = self._support_mapping_status(db,ids)
            base = [as_of,ids]
            stats = self._stats(db,base)
            common = {'publication_ids':ids,'publications':publications,
                'publication_id':ids[0] if len(ids)==1 else None,'data_source':'supabase',
                'is_synthetic':False,'offset':offset,'limit':limit,**mapping}
            if mapping['mapping_status']!='configured_research':
                return {**common,'status':'mapping_required','programs':[],'next_offset':None,
                    'coverage':{'total':int(stats['total']),'matched':0,
                                'unclassified':int(stats['unclassified']),'complete':False}}
            clause = ''' EXISTS (SELECT 1 FROM program_support_actions a
                WHERE a.program_version_id=c.program_version_id AND a.rule_version=%s
                  AND a.category_code=ANY(%s::text[]) AND a.review_status<>'rejected' '''
            params = [mapping['rule_version'],categories]
            if actions:
                clause += ' AND a.service_action=ANY(%s::text[])'
                params.append(actions)
            clause += ')'
            search_order=[]
            region_rank_sql,region_rank_params='0',[]
            if region_level is not None:
                facets=catalog_region_facets(self._region_tree(db,as_of))
                ranked,search_order=catalog_region_ranks(facets,region_level,region_code)
                allowed=[key for key,_ in ranked]
                clause+=''' AND EXISTS(SELECT 1 FROM program_regions r
                    WHERE r.program_version_id=c.program_version_id
                    AND r.scope_purpose='administering_jurisdiction'
                    AND (r.scope_type='nationwide' OR
                         (r.scope_type='specific' AND r.region_id=ANY(%s::uuid[]))))'''
                params.append(allowed)
                region_rank_sql='''COALESCE((SELECT min(CASE
                    WHEN r.scope_type='specific' AND r.region_id=ANY(%s::uuid[]) THEN 0
                    WHEN r.scope_type='specific' AND r.region_id=ANY(%s::uuid[]) THEN 1
                    WHEN r.scope_type='nationwide' THEN 2 END) FROM program_regions r
                    WHERE r.program_version_id=c.program_version_id
                    AND r.scope_purpose='administering_jurisdiction'),3)'''
                region_rank_params=[[key for key,rank in ranked if rank==0],
                                    [key for key,rank in ranked if rank==1]]
            elif region_code:
                allowed=self._allowed_regions(db,region_code,as_of)
                region_filter,region_params=_filters(q='',category_id=None,domain=None,
                    allowed_regions=allowed,region_code=region_code,status=None)
                clause+=' AND '+region_filter.removeprefix(' WHERE ')
                params.extend(region_params)
                region_rank_sql='''COALESCE((SELECT min(CASE
                    WHEN r.scope_type='specific' AND EXISTS(SELECT 1 FROM region_code_mappings m
                        WHERE m.region_id=r.region_id AND m.external_code_system='SGG5'
                          AND m.external_region_code=%s AND m.mapping_status='exact'
                          AND (m.valid_from IS NULL OR m.valid_from<=%s)
                          AND (m.valid_to IS NULL OR m.valid_to>=%s)) THEN 0
                    WHEN r.scope_type='specific' THEN 1 WHEN r.scope_type='nationwide' THEN 2 END)
                    FROM program_regions r WHERE r.program_version_id=c.program_version_id
                      AND r.scope_purpose='administering_jurisdiction'),3)'''
                region_rank_params=[region_code,as_of,as_of]
            # All matches are counted before pagination, including unknown age/period.
            matched=int(db.execute(CORPUS+' SELECT count(*) AS matched FROM corpus c WHERE '+clause,
                                   [*base,*params]).fetchone()['matched'])
            category_rank_sql='''(SELECT min(array_position(%s::text[],a.category_code))
                FROM program_support_actions a WHERE a.program_version_id=c.program_version_id
                  AND a.rule_version=%s AND a.category_code=ANY(%s::text[])
                  AND a.review_status<>'rejected' '''
            rank_params=[categories,mapping['rule_version'],categories]
            if actions:
                category_rank_sql+=' AND a.service_action=ANY(%s::text[])'
                rank_params.append(actions)
            category_rank_sql+=')'
            rows=db.execute(CORPUS+' SELECT c.*, '+category_rank_sql+' AS support_category_rank, '+
                region_rank_sql+' AS support_region_rank FROM corpus c WHERE '+clause+
                ''' ORDER BY support_category_rank,support_region_rank,
                    CASE c.period_status WHEN 'always' THEN 0 WHEN 'ongoing' THEN 0
                        WHEN 'not_required' THEN 0 WHEN 'ended' THEN 2 ELSE 1 END,
                    c.title,c.program_version_id LIMIT %s OFFSET %s''',
                [*base,*rank_params,*region_rank_params,*params,limit,offset]).fetchall()
            version_ids=[str(row['program_version_id']) for row in rows]
            groups=self._children(db,version_ids)
            services=self._support_links(db,version_ids,mapping['rule_version'])
            programs=[]
            for row in rows:
                key=str(row['program_version_id'])
                links=services.get(key,[])
                program=_normalize_program(row,*(group.get(key,[]) for group in groups),support_links=links)
                program['support_category_rank']=row.get('support_category_rank')
                program['support_region_rank']=row.get('support_region_rank')
                program['search_reasons']=[{'type':'support_service','category_id':link['category_code'],
                    'service_action':link['service_action'],'evidence_id':link['evidence_id'],
                    'rule_version':link['rule_version'],'review_status':link['review_status']}
                    for link in links if link['category_code'] in categories
                    and (not actions or link['service_action'] in actions)]
                programs.append(program)
            coverage=db.execute(CORPUS+''' SELECT count(DISTINCT c.program_version_id) AS mapped
                FROM corpus c JOIN program_support_actions a USING(program_version_id)
                WHERE a.rule_version=%s AND a.review_status<>'rejected' ''',
                [*base,mapping['rule_version']]).fetchone()
        return {**common,'status':'ok','programs':programs,'region_search_order':search_order,
            'next_offset':offset+limit if offset+limit<matched else None,
            'coverage':{'total':int(stats['total']),'matched':matched,'unclassified':int(stats['unclassified']),
                'support_mapped':int(coverage['mapped']),'support_unmapped':int(stats['total'])-int(coverage['mapped']),
                'complete':offset+len(programs)>=matched}}

    @staticmethod
    def _stats(db, base):
        # The application-period expression contains source-text regexes. Keep
        # only these two columns and evaluate it once, rather than once for
        # every status FILTER after the corpus CTE has been inlined.
        return db.execute(CORPUS + ''', status_rows AS MATERIALIZED (
            SELECT program_version_id,period_status FROM corpus
        ) SELECT count(*) AS total,
            count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM program_tags t
                WHERE t.program_version_id=c.program_version_id AND t.review_status<>'rejected')) AS unclassified,
            count(*) FILTER (WHERE period_status='ongoing') AS ongoing,
            count(*) FILTER (WHERE period_status='upcoming') AS upcoming,
            count(*) FILTER (WHERE period_status='ended') AS ended,
            count(*) FILTER (WHERE period_status='unknown') AS unknown,
            count(*) FILTER (WHERE period_status='always') AS always,
            count(*) FILTER (WHERE period_status='varies') AS varies,
            count(*) FILTER (WHERE period_status='announced') AS announced,
            count(*) FILTER (WHERE period_status='not_required') AS not_required
            FROM status_rows c''', base).fetchone()

    @staticmethod
    def _allowed_regions(db, code, as_of):
        rows = db.execute('''WITH RECURSIVE ancestors AS (
            SELECT r.region_id,r.parent_region_id,ARRAY[r.region_id] AS visited
            FROM region_code_mappings m JOIN administrative_regions r USING(region_id)
            WHERE m.external_code_system='SGG5' AND m.external_region_code=%s
              AND m.mapping_status='exact'
              AND (m.valid_from IS NULL OR m.valid_from<=%s) AND (m.valid_to IS NULL OR m.valid_to>=%s)
              AND (r.valid_from IS NULL OR r.valid_from<=%s) AND (r.valid_to IS NULL OR r.valid_to>=%s)
            UNION ALL SELECT r.region_id,r.parent_region_id,a.visited || r.region_id
            FROM administrative_regions r JOIN ancestors a ON r.region_id=a.parent_region_id
            WHERE NOT r.region_id=ANY(a.visited) AND cardinality(a.visited)<8
              AND (r.valid_from IS NULL OR r.valid_from<=%s) AND (r.valid_to IS NULL OR r.valid_to>=%s)
        ) SELECT DISTINCT region_id FROM ancestors''', (code, as_of, as_of, as_of, as_of, as_of, as_of)).fetchall()
        return [str(row['region_id']) for row in rows]

    @staticmethod
    def _region_tree(db, as_of):
        return db.execute('''SELECT r.region_id,r.parent_region_id,r.region_code,r.region_name,r.region_level,
            codes.sgg_code,codes.sido_code
            FROM administrative_regions r
            LEFT JOIN LATERAL (
                SELECT CASE WHEN count(DISTINCT m.external_region_code) FILTER (WHERE m.external_code_system='SGG5')=1
                    THEN min(m.external_region_code) FILTER (WHERE m.external_code_system='SGG5') END AS sgg_code,
                  CASE WHEN count(DISTINCT m.external_region_code) FILTER (WHERE m.external_code_system='SIDO2')=1
                    THEN min(m.external_region_code) FILTER (WHERE m.external_code_system='SIDO2') END AS sido_code
                FROM region_code_mappings m WHERE m.region_id=r.region_id AND m.mapping_status='exact'
                  AND m.external_code_system IN ('SGG5','SIDO2')
                  AND (m.valid_from IS NULL OR m.valid_from<=%s) AND (m.valid_to IS NULL OR m.valid_to>=%s)
            ) codes ON true
            WHERE r.code_system='MOIS_ADMIN'
              AND (r.valid_from IS NULL OR r.valid_from<=%s) AND (r.valid_to IS NULL OR r.valid_to>=%s)
            ORDER BY r.region_code,r.region_id''', [as_of] * 4).fetchall()

    @staticmethod
    def _region_facets(db, base, as_of):
        # Explicit valid SGG5 mappings determine the selectable codes. Program
        # jurisdictions match those regions or their stored administrative parents.
        query = CORPUS.replace('WITH corpus', 'WITH RECURSIVE corpus', 1) + ''',
        valid_regions AS (
            SELECT DISTINCT m.external_region_code AS code,r.region_id,r.region_name AS name,r.parent_region_id
            FROM region_code_mappings m JOIN administrative_regions r USING(region_id)
            WHERE m.external_code_system='SGG5' AND m.mapping_status='exact'
              AND (m.valid_from IS NULL OR m.valid_from<=%s) AND (m.valid_to IS NULL OR m.valid_to>=%s)
              AND (r.valid_from IS NULL OR r.valid_from<=%s) AND (r.valid_to IS NULL OR r.valid_to>=%s)
        ), ancestors AS (
            SELECT region_id AS base_id,region_id,parent_region_id,ARRAY[region_id] AS visited FROM valid_regions
            UNION ALL SELECT a.base_id,r.region_id,r.parent_region_id,a.visited || r.region_id
            FROM ancestors a JOIN administrative_regions r ON r.region_id=a.parent_region_id
            WHERE NOT r.region_id=ANY(a.visited) AND cardinality(a.visited)<8
              AND (r.valid_from IS NULL OR r.valid_from<=%s) AND (r.valid_to IS NULL OR r.valid_to>=%s)
        ), region_programs AS (
            SELECT a.base_id,r.program_version_id FROM ancestors a
            JOIN program_regions r ON r.region_id=a.region_id
            JOIN corpus c USING(program_version_id)
            WHERE r.scope_type='specific' AND r.scope_purpose='administering_jurisdiction'
            UNION
            SELECT v.region_id,r.program_version_id FROM valid_regions v
            CROSS JOIN program_regions r JOIN corpus c USING(program_version_id)
            WHERE r.scope_type='nationwide' AND r.scope_purpose='administering_jurisdiction'
        ) SELECT v.region_id,v.code,v.name,count(DISTINCT r.program_version_id) AS count
          FROM valid_regions v LEFT JOIN region_programs r ON r.base_id=v.region_id
          GROUP BY v.region_id,v.code,v.name ORDER BY v.code,v.region_id'''
        return _serializable(db.execute(query, [*base, *([as_of] * 6)]).fetchall())

    @staticmethod
    def _children(db, version_ids):
        if not version_ids:
            return {}, {}, {}, {}, {}
        result = []
        queries = [
            '''SELECT t.program_version_id,t.category_code,k.category_name,k.description,t.assignment_method,
                t.review_status,t.rule_version,t.evidence_id,e.source_quote FROM program_tags t
                JOIN categories k USING(category_code)
                LEFT JOIN evidence e ON e.evidence_id=t.evidence_id
                    AND e.program_version_id=t.program_version_id AND e.evidence_kind='category_basis'
                WHERE t.program_version_id=ANY(%s::uuid[])
                AND t.review_status<>'rejected' ORDER BY k.category_name,t.category_code''',
            '''SELECT p.program_version_id,p.scope_type,p.region_id,p.scope_purpose,p.mapping_status,
                p.original_region_text,r.region_code,r.region_name,r.code_system
                FROM program_regions p LEFT JOIN administrative_regions r USING(region_id)
                WHERE p.program_version_id=ANY(%s::uuid[]) ORDER BY p.scope_type,r.region_code,p.program_region_id''',
            '''SELECT r.program_version_id,r.condition_type,r.operator,r.condition_value,r.logic_group,
                r.review_status,r.evidence_id,e.source_quote FROM eligibility_rules r
                LEFT JOIN evidence e ON e.evidence_id=r.evidence_id
                    AND e.program_version_id=r.program_version_id AND e.evidence_kind='eligibility_basis'
                WHERE r.program_version_id=ANY(%s::uuid[]) ORDER BY r.condition_type,r.rule_id''',
            '''SELECT program_version_id,evidence_id,source_quote FROM evidence
                WHERE program_version_id=ANY(%s::uuid[]) AND evidence_kind='official_link'
                ORDER BY evidence_id''',
            '''SELECT vd.program_version_id,max(d.retrieved_at) AS source_retrieved_at
                FROM program_version_documents vd
                JOIN program_versions v ON v.program_version_id=vd.program_version_id
                JOIN programs p ON p.program_id=v.program_id
                JOIN source_documents d ON d.document_id=vd.document_id AND d.source_id=p.source_id
                WHERE vd.program_version_id=ANY(%s::uuid[]) GROUP BY vd.program_version_id''',
        ]
        for query in queries:
            grouped = defaultdict(list)
            for row in db.execute(query, (version_ids,)).fetchall():
                grouped[str(row['program_version_id'])].append(row)
            result.append(grouped)
        return result

    def catalog(self, *, as_of, q='', category_id=None, domain=None, region_code=None, region_level=None,
                status=None, offset=0, limit=100, view='full'):
        if not isinstance(as_of, date) or isinstance(as_of, datetime):
            raise ValueError('as_of must be a date')
        if not isinstance(q, str) or len(q) > 200 or not 1 <= limit <= 500 or offset < 0:
            raise ValueError('invalid catalog query')
        if domain and domain not in DOMAIN_LABELS or status and status not in PERIOD_LABELS:
            raise ValueError('invalid catalog filter')
        if view not in ('full', 'browser'):
            raise ValueError('invalid catalog view')
        validate_catalog_region(region_level, region_code)
        if view == 'browser':
            return self._browser_catalog(as_of=as_of, q=q, category_id=category_id, domain=domain,
                region_code=region_code, region_level=region_level, status=status, offset=offset, limit=limit)
        with postgres_connection() as db:
            publications = self._publications(db)
            ids = [p['publication_id'] for p in publications]
            base = [as_of, ids]
            mapping = self._support_mapping_status(db, ids)
            namespace = catalog_cache_namespace()
            def cached(kind, loader, *filters):
                # Published membership and date keep counts/labels separate when
                # sources switch publications or the application-status day changes.
                key = (namespace, as_of, tuple(ids), kind, *filters) if namespace else None
                return CATALOG_CACHE.get_or_load(key, loader)

            stats = cached('stats', lambda: self._stats(db, base))
            explicit = region_level is not None
            available_regions = cached('available_regions',
                lambda: catalog_region_facets(self._region_tree(db, as_of)))
            regions = available_regions if explicit else None
            ranked_ids, search_order = catalog_region_ranks(regions, region_level, region_code) if explicit else ([], [])
            rank_sql = '''COALESCE((SELECT min(CASE
                WHEN r.scope_type='specific' AND r.region_id=ANY(%s::uuid[]) THEN 0
                WHEN r.scope_type='specific' AND r.region_id=ANY(%s::uuid[]) THEN 1
                WHEN r.scope_type='nationwide' THEN 2 END)
                FROM program_regions r WHERE r.program_version_id=c.program_version_id
                  AND r.scope_purpose='administering_jurisdiction'),3)'''
            catalog_sql, catalog_base, corpus = CORPUS, base, 'corpus'
            if explicit:
                catalog_sql += ', ranked_corpus AS (SELECT c.*, ' + rank_sql + ' AS region_match_rank FROM corpus c)'
                catalog_base = [*base, [key for key, rank in ranked_ids if rank == 0],
                                [key for key, rank in ranked_ids if rank == 1]]
                corpus = 'ranked_corpus'
            allowed = self._allowed_regions(db, region_code, as_of) if region_code and not explicit else []
            filters, filter_params = _filters(q=q, category_id=category_id, domain=domain,
                allowed_regions=allowed, region_code=region_code if not explicit else None, status=status)
            if explicit:
                filters += (' AND ' if filters else ' WHERE ') + 'c.region_match_rank<3'
            matched = cached('matched', lambda: int(db.execute(
                catalog_sql + f' SELECT count(*) AS matched FROM {corpus} c' + filters,
                [*catalog_base, *filter_params]).fetchone()['matched']),
                q, category_id, domain, region_level, region_code, status)
            rows = db.execute(catalog_sql + f' SELECT c.* FROM {corpus} c' + filters +
                (' ORDER BY c.region_match_rank,c.title,c.program_version_id' if explicit else ' ORDER BY c.title,c.program_version_id') + ' LIMIT %s OFFSET %s',
                [*catalog_base, *filter_params, limit, offset]).fetchall()
            categories = cached('categories', lambda: db.execute(CORPUS + '''
                SELECT t.category_code AS category_id,k.category_name AS label,
                    count(DISTINCT t.program_version_id) AS count
                FROM corpus c JOIN program_tags t USING(program_version_id)
                JOIN categories k USING(category_code) WHERE t.review_status<>'rejected'
                GROUP BY t.category_code,k.category_name ORDER BY t.category_code''', base).fetchall())
            if not explicit:
                regions = cached('regions', lambda: self._region_facets(db, base, as_of))
            tags, scopes, rules, links, documents = self._children(db, [str(row['program_version_id']) for row in rows])
            services = self._support_links(db, [str(row['program_version_id']) for row in rows], mapping['rule_version'])
            programs = [_normalize_program(row, tags.get(str(row['program_version_id']), []),
                scopes.get(str(row['program_version_id']), []), rules.get(str(row['program_version_id']), []),
                links.get(str(row['program_version_id']), []),
                documents.get(str(row['program_version_id']), []),
                services.get(str(row['program_version_id']), [])) for row in rows]
            if explicit:
                for program, row in zip(programs, rows):
                    rank = row['region_match_rank']
                    program['region_match_rank'] = rank
                    program['region_match_label'] = next(item['label'] for item in search_order if item['rank'] == rank)
        return {
            'schema_version': 'welfare_catalog_v1',
            'publication_id': ids[0] if len(ids) == 1 else None,
            'publication_selection': 'explicit' if self.publication_id else 'active_per_source',
            'publication_ids': ids, 'publications': publications,
            'is_synthetic': False, 'data_source': 'supabase', 'as_of': as_of.isoformat(),
            'date_basis': 'application', 'region_basis': 'administering_jurisdiction',
            'classification_basis': 'internal_tags', **mapping,
            'total': int(stats['total']), 'matched': matched, 'offset': offset, 'limit': limit,
            'next_offset': offset + limit if offset + limit < matched else None,
            'programs': programs, 'categories': _serializable(categories),
            'domains': [{'code': key, 'label': value, 'count': 0} for key, value in DOMAIN_LABELS.items()],
            'regions': regions,
            'available_regions': available_regions,
            'region_level': region_level, 'region_code': region_code, 'region_search_order': search_order,
            'statuses': [{'code': key, 'label': value, 'count': int(stats[key])}
                         for key, value in PERIOD_LABELS.items()],
            'coverage': {'total': int(stats['total']), 'matched': matched, 'unclassified': int(stats['unclassified'])},
            'limitations': [mapping['mapping_reason'],
                '지역 필터는 수행기관의 관할을 탐색하며 거주 자격을 판정하지 않습니다.',
                '신청기간 내 표시만으로 현재 접수 중이거나 개인에게 자격이 있다고 확정하지 않습니다.'],
        }

    def _browser_catalog(self, *, as_of, q, category_id, domain, region_code, region_level,
                         status, offset, limit):
        """Read complete page content without unused whole-catalog facet counts.

        This path deliberately does not use application caches. Page counts,
        ordering, bodies and evidence share one read-only snapshot. Region
        selection options load independently for the all/national first page.
        """
        with postgres_connection() as db:
            publications = self._publications(db)
            ids = [publication['publication_id'] for publication in publications]
            mapping = self._support_mapping_status(db, ids)
            # The initial all/national page needs no local rank calculation.
            # Region options are loaded independently by the browser.
            available_regions = (catalog_region_facets(self._region_tree(db, as_of))
                if region_level in ('sido', 'sgg', 'administrative_dong') else [])
            explicit = region_level is not None
            ranked_ids, search_order = catalog_region_ranks(available_regions, region_level, region_code) if explicit else ([], [])
            catalog_sql, catalog_base, corpus = CORPUS, [as_of, ids], 'corpus'
            if explicit:
                catalog_sql += ''', ranked_corpus AS (SELECT c.*, COALESCE((SELECT min(CASE
                    WHEN r.scope_type='specific' AND r.region_id=ANY(%s::uuid[]) THEN 0
                    WHEN r.scope_type='specific' AND r.region_id=ANY(%s::uuid[]) THEN 1
                    WHEN r.scope_type='nationwide' THEN 2 END)
                    FROM program_regions r WHERE r.program_version_id=c.program_version_id
                      AND r.scope_purpose='administering_jurisdiction'),3) AS region_match_rank
                    FROM corpus c)'''
                catalog_base.extend([[key for key, rank in ranked_ids if rank == 0],
                                     [key for key, rank in ranked_ids if rank == 1]])
                corpus = 'ranked_corpus'
            allowed = self._allowed_regions(db, region_code, as_of) if region_code and not explicit else []
            filters, filter_params = _filters(q=q, category_id=category_id, domain=domain,
                allowed_regions=allowed, region_code=region_code if not explicit else None, status=status)
            if explicit:
                filters += (' AND ' if filters else ' WHERE ') + 'c.region_match_rank<3'
            page = db.execute(browser_page_query(catalog_sql, corpus, filters, explicit=explicit),
                [*catalog_base, *filter_params, limit, offset]).fetchone()
            services = self._support_links(db, [str(row['program_version_id']) for row in page['rows']], mapping['rule_version'])
            groups = []
            for kind in ('tags', 'scopes', 'rules', 'links', 'documents'):
                grouped = defaultdict(list)
                for child in page[kind]:
                    grouped[str(child['program_version_id'])].append(child)
                groups.append(grouped)
            programs = []
            for row in page['rows']:
                version_id = str(row['program_version_id'])
                program = _normalize_program(row, *(group.get(version_id, []) for group in groups),
                                             support_links=services.get(version_id, []))
                if explicit:
                    rank = row['region_match_rank']
                    program['region_match_rank'] = rank
                    program['region_match_label'] = next(item['label'] for item in search_order if item['rank'] == rank)
                programs.append(program)
        total, matched = int(page['total']), int(page['matched'])
        return {
            'schema_version': 'welfare_catalog_v1', 'view': 'browser', 'facet_counts_included': False,
            'publication_id': ids[0] if len(ids) == 1 else None,
            'publication_selection': 'explicit' if self.publication_id else 'active_per_source',
            'publication_ids': ids, 'publications': publications,
            'is_synthetic': False, 'data_source': 'supabase', 'as_of': as_of.isoformat(),
            'date_basis': 'application', 'region_basis': 'administering_jurisdiction',
            'classification_basis': 'internal_tags', **mapping,
            'total': total, 'matched': matched, 'offset': offset, 'limit': limit,
            'next_offset': offset + limit if offset + limit < matched else None,
            'programs': programs, 'categories': page['categories'],
            'domains': [{'code': key, 'label': value} for key, value in DOMAIN_LABELS.items()],
            'regions': available_regions, 'available_regions': available_regions,
            'region_level': region_level, 'region_code': region_code, 'region_search_order': search_order,
            'statuses': [{'code': key, 'label': value} for key, value in PERIOD_LABELS.items()],
            'coverage': {'total': total, 'matched': matched},
            'limitations': [mapping['mapping_reason'],
                '지역 필터는 수행기관의 관할을 탐색하며 거주 자격을 판정하지 않습니다.',
                '신청기간 내 표시만으로 현재 접수 중이거나 개인에게 자격이 있다고 확정하지 않습니다.'],
        }

    def catalog_regions(self, *, as_of):
        if not isinstance(as_of, date) or isinstance(as_of, datetime):
            raise ValueError('as_of must be a date')
        with postgres_connection() as db:
            return catalog_region_facets(self._region_tree(db, as_of))

    def published_program(self, program_version_id, *, as_of):
        """Read one exact published version and its evidence in one snapshot."""
        try:
            version_id = str(UUID(str(program_version_id)))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('invalid program version') from None
        if not isinstance(as_of, date) or isinstance(as_of, datetime):
            raise ValueError('as_of must be a date')
        with postgres_connection() as db:
            publications = self._publications(db)
            ids = [publication['publication_id'] for publication in publications]
            mapping = self._support_mapping_status(db, ids)
            row = db.execute(CORPUS + ''' SELECT c.* FROM corpus c
                WHERE c.program_version_id=%s::uuid LIMIT 1''', [as_of, ids, version_id]).fetchone()
            if row is None:
                return None
            groups = self._children(db, [version_id])
            services = self._support_links(db, [version_id], mapping['rule_version'])
            program = _normalize_program(row, *(group.get(version_id, []) for group in groups),
                                         support_links=services.get(version_id, []))
            program['mapping_status'] = mapping['mapping_status']
            program['publication'] = next(publication for publication in publications
                if publication['source_id'] == row['source_id'])
            program['as_of'] = as_of.isoformat()
            return program

    def search(self, *, i5_domains=(), category_ids=(), keyword=None):
        if not set(i5_domains) <= set(DOMAIN_LABELS):
            raise ValueError('invalid I5 domain')
        # This interface feeds the recommendation graph. Listing the catalog is
        # deliberately separate from recommending an arbitrary first page of it.
        if category_ids or keyword:
            raise ValueError('use catalog for explicit internal-category or keyword browsing')
        with postgres_connection() as db:
            publications = self._publications(db)
            ids = [p['publication_id'] for p in publications]
            stats = self._stats(db, [datetime.now(ZoneInfo('Asia/Seoul')).date(), ids])
        return {
            'status': 'mapping_required' if i5_domains else 'ok',
            'publication_id': ids[0] if len(ids) == 1 else None,
            'publication_selection': 'explicit' if self.publication_id else 'active_per_source',
            'publication_ids': ids, 'publications': publications, 'data_source': 'supabase',
            'is_synthetic': False, 'programs': [], 'mapping_status': 'unconfigured',
            'mapping_reason': MAPPING_REASON,
            'coverage': {'total': int(stats['total']), 'matched': 0, 'unclassified': int(stats['unclassified'])},
        }
