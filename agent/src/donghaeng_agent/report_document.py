"""Frozen, plain-text document shared by screen, Markdown, PDF and HWPX."""
import re
from datetime import datetime, timedelta, timezone
from .planning_details import PLANNING_FIELDS, applicable_fields

VERSION = 'welfare-plan-2.0'
STATUS = {'unconfirmed':'미확인', 'confirmed':'사용자 확인 기록', 'not_applicable':'해당 없음으로 기록'}
KINDS = {'existing_program':'A · 기존 서비스 활용·연계', 'program_improvement':'B · 기존 사업 보완·확대', 'new_program':'B · 신규 사업 설계'}
REVIEWS = {'eligibility':'지원 대상·조건', 'support_content':'지원 내용', 'delivery':'전달기관·이용 경로', 'finance_and_cost':'재원·본인부담·이용 비용'}


def display_created_at(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return value
        return parsed.astimezone(timezone(timedelta(hours=9))).strftime('%Y년 %m월 %d일 %H:%M (한국시간)')
    except (TypeError, ValueError, AttributeError):
        return str(value or '미확인')


def entry_text(label, entry):
    entry = entry or {}
    parts = [f"{label} · {STATUS.get(entry.get('status', 'unconfirmed'), '미확인')}: {entry.get('text') or '확인할 내용 미기재'}"]
    if entry.get('evidence'): parts.append(f"근거: {entry['evidence']}")
    parts.append(f"확인 역할: {entry.get('owner') or '미지정'} / 다음 조치: {entry.get('next_action') or '확인 계획 수립 필요'}")
    return '\n'.join(parts)


def authored(entry):
    return bool(entry and (entry.get('status', 'unconfirmed') != 'unconfirmed' or any(str(entry.get(key) or '').strip() for key in ('text', 'evidence', 'owner', 'next_action'))))


def scope_label(scope):
    sex = {'MAN':'남성','WMAN':'여성'}.get(scope.get('sex_code'), '전체 성별')
    age = {'20G':'20대','30G':'30대','40G':'40대','50G':'50대','60GU':'60세 이상'}.get(scope.get('age_code'), '전체 연령')
    return f"{sex} · {age}"


def month_label(value):
    if value and len(value) == 6 and value.isdigit():
        return f"{value[:4]}년 {int(value[4:])}월"
    return value or '미확인'


def summary_entry(label, entry):
    entry = entry or {}
    return f"{label} {STATUS.get(entry.get('status'), '미확인')}" + (f": {entry['text']}" if entry.get('text') else '')


def build_response_document(report):
    analysis = report.get('analysis_snapshot') or {}
    scope = report.get('scope') or analysis.get('scope') or {}
    plans = report.get('selected_plans') or []
    kind_set = {p['kind'] for p in plans}
    doc_type = 'A+B' if 'existing_program' in kind_set and len(kind_set) > 1 else ('A' if kind_set == {'existing_program'} else 'B')
    observations = []
    for item in analysis.get('assessments', []):
        bits = []
        for source, value in item.get('sources', {}).items():
            if value and value.get('change_percent') is not None:
                name = {'skt':'통신 활동', 'card_a':'카드 이용'}.get(source, source)
                bits.append(f"{name} 전월 대비 {value['change_percent']:.2f}%")
        observations.append(f"{scope_label(item.get('scope') or scope)} — {item.get('category_label', '활동 관측')}: " + (' · '.join(bits) or '변화량 미확인'))
    summary_observation = (' / '.join(observations) if len(observations) <= 3 else f"{len(observations)}개 집단의 활동 변화를 각각 관측했습니다. 집단별 수치와 관측 범주는 ‘지역·집단·자료 범위’에서 확인할 수 있습니다.") if observations else '분석 범위에서 활동 변화 자료를 확인할 수 없습니다.'
    summary = ['관측 결과: ' + summary_observation,
        '실무상 의미: 활동 변화는 해당 지역·집단의 참여 여건과 지원 필요를 추가 확인하는 출발점입니다.',
        '판단 한계: 집계 활동 자료만으로 고립 위험의 증가 여부를 판단할 수 없습니다. 개인의 고립·지원 자격·이용 의사는 별도 확인 사항입니다.']
    for plan in plans:
        details = plan.get('planning_details') or {}
        summary.append(f"제안 방향: {plan['title']} / 선택 이유: {plan.get('rationale') or '추가 확인 필요'}")
        supply = summary_entry('운영·공급', details.get('availability')) if plan['kind'] == 'existing_program' else ' / '.join([summary_entry('재원', details.get('funding')), summary_entry('실행 조건', details.get('execution'))])
        summary.append(f"비용·공급 확인: {plan['title']} / {summary_entry('비용', details.get('budget'))} / {supply}")
        summary.append(f"이번 결정 요청: {(details.get('decisions') or {}).get('text') or plan.get('checks') or '확인·결정할 사항을 담당자가 지정해야 합니다.'}")
    sections = [{'id':'summary','title':'핵심 요약·결정 요청','paragraphs':summary}]
    analysis_paragraphs = [f"지역: {report.get('region_name', analysis.get('region_name', '미확인'))}",
        f"관측 기준: {month_label(scope.get('month'))} / {scope_label(scope)}",
        f"지역 코드: {scope.get('dong_code') or scope.get('region_code', '미확인')}"]
    if scope.get('dong_code'):
        analysis_paragraphs.append(f"선택 행정동의 I5 · 활동 집계 지역: 상위 시군구({scope.get('region_code')})")
    analysis_paragraphs.extend(observations)
    investigation = analysis.get('context_investigation') or {}
    analysis_paragraphs.append(investigation.get('summary') or '지역 맥락 조사는 아직 진행하지 않았습니다.')
    lifecycle = analysis.get('lifecycle_interpretation') or {}
    if lifecycle:
        source = lifecycle.get('source') or {}
        pages = source.get('pages') or []
        pages_text = '·'.join(map(str, pages)) if isinstance(pages, list) else str(pages)
        analysis_paragraphs.append(f"문헌: {source.get('title') or '출처 미확인'} / {source.get('year') or '기준연도 미확인'} / 참고 본문: {pages_text or '원본 분석 출처 확인'}쪽 / 해석 가이드: {lifecycle.get('guide_version', '미확인')}")
        for group in lifecycle.get('groups') or []:
            for question in group.get('questions') or []:
                analysis_paragraphs.append(f"생애주기별 확인 질문(가설): {question}")
    sections.append({'id':'analysis','title':'지역·집단·자료 범위','paragraphs':analysis_paragraphs})
    unresolved, references, execution, decisions = [], [], [], []
    for index, plan in enumerate(plans, 1):
        details = plan.get('planning_details') or {}
        paragraphs = [f"문서 유형: {KINDS[plan['kind']]} / 저장된 {plan['version']}차 수정본",
            f"대상과 범위: {plan.get('target') or '미확인'}", f"분석과 연결한 이유: {plan.get('rationale') or '추가 확인 필요'}",
            f"실행 내용: {plan.get('content') or '미확인'}", f"실행 전 확인할 사항: {plan.get('checks') or '추가 확인 필요'}"]
        for key in applicable_fields(plan['kind']):
            if key in {'execution','evaluation','decisions','sources'}: continue
            entry = details.get(key)
            if entry and entry.get('status') == 'not_applicable':
                paragraphs.append(entry_text(f"적용·생략 기록 — {PLANNING_FIELDS[key]}", entry))
            elif authored(entry):
                paragraphs.append(entry_text(PLANNING_FIELDS[key], entry))
        # Previously authored fields remain visible after a type change.
        for key in details.keys() - set(applicable_fields(plan['kind'])):
            if authored(details[key]):
                paragraphs.append(entry_text(f"이전 유형에서 작성한 {PLANNING_FIELDS[key]}(적용 재검토)", details[key]))
        paragraphs.append('담당자가 입력한 확인 내용입니다. 시스템의 자동 검증이 아닌 사용자 확인 기록입니다.')
        for key, label in REVIEWS.items():
            item = (plan.get('policy_review') or {}).get(key) or {}
            paragraphs.append(f"사업 적용 검토 — {label}: {STATUS.get(item.get('status'), '미확인')} / 내용: {item.get('text') or '미확인'} / 사용자 입력 근거: {item.get('evidence') or '미기재'}")
        if not plan.get('support_needs'):
            paragraphs.append('지원 필요 확인 기록이 아직 없습니다.')
        for need in plan.get('support_needs') or []:
            from .welfare_support_mapping import CATEGORY_LABELS
            paragraphs.append(f"지원 필요 확인 기록 — {CATEGORY_LABELS.get(need['category_code'], need['category_code'])}: {STATUS.get(need.get('status'), '미확인')} / {need.get('text') or '미확인'} / 근거: {need.get('evidence') or '미기재'}")
        if plan.get('feedback'): paragraphs.append(f"반영할 검토 의견: {plan['feedback']}")
        reference = plan.get('program_reference')
        if reference:
            paragraphs.append(f"사용자가 연결한 참고 사업: {reference['title']} / 발행기관: {reference.get('publisher') or '미확인'} / 사업 버전: {reference.get('program_version_id', '미확인')} / {reference.get('url') or '원문 링크 미확인'}")
        sections.append({'id':f"plan-{index}", 'title':f"{index}. {plan['title']}", 'paragraphs':paragraphs})
        for key in ('execution','evaluation'):
            if authored(details.get(key)):
                execution.append(f"{plan['title']} — {entry_text(PLANNING_FIELDS[key], details[key])}")
        missing = []
        for key in applicable_fields(plan['kind']):
            entry = details.get(key) or {}
            if not authored(entry):
                missing.append(PLANNING_FIELDS[key])
            elif entry.get('status') not in ('confirmed','not_applicable'):
                unresolved.append(f"{plan['title']} — {PLANNING_FIELDS[key]}: 미확인 / 확인 역할: {entry.get('owner') or '미지정'} / 다음 조치: {entry.get('next_action') or '확인 계획 수립 필요'} (작성 내용은 해당 본문 절 참조)")
        if missing:
            unresolved.append(f"{plan['title']} — 적용 항목 중 아직 미확인·미기재: {' · '.join(missing)}. 확인 역할: 미지정 / 다음 조치: 담당 역할을 지정하고 필요한 자료·기관 회신 확인 계획을 작성합니다.")
        if authored(details.get('decisions')):
            decisions.append(f"{plan['title']} — {entry_text('확인·결정 기록', details['decisions'])}")
        if authored(details.get('sources')): references.append(f"{plan['title']} — {entry_text('작성 근거·갱신', details['sources'])}")
    sections.extend([
        {'id':'execution','title':'실행·인계·성과 점검 계획','paragraphs':execution or ['단계별 실행·성과 점검 계획은 아직 미확인입니다. 확인 역할과 다음 조치는 미확인 사항 절에 연결했습니다.']},
        {'id':'decisions','title':'확인·결정 요청과 사용자 기록','paragraphs':decisions or ['확인·결정 기록이 아직 없습니다. 이번에 요청할 결정은 핵심 요약과 미확인 사항을 참고해 담당자가 지정해야 합니다.']},
        {'id':'unresolved','title':'미확인 사항·확인 역할·다음 조치','paragraphs':unresolved or ['추가 미확인 기록이 없습니다. 사용자 확인 기록은 기관 승인과 구별합니다.']},
    ])
    references += [f"원본 현황 분석 보고서: {report['analysis_report_id']}", f"관측 규칙: {analysis.get('rule_version') or '미확인'}"]
    for source in investigation.get('sources') or []:
        references.append(f"{source.get('source_id')}: {source.get('title') or '제목 미확인'} / {source.get('url') or '링크 미확인'} / 자료 대상 기간: {source.get('reference_period') or '미확인'}")
    for family, provenance in (analysis.get('provenance') or {}).items():
        references.append(f"{family}: {provenance.get('run_id')} / manifest SHA-256 {provenance.get('manifest_sha256')}")
    references.extend(f"작성 한계: {item}" for item in report.get('limitations') or [])
    sections.append({'id':'sources','title':'출처·원본 연결·작성 정보','paragraphs':references})
    return {'version':VERSION,'title':report['title'],'subtitle':'담당자 검토안 · 시행 전 검토 문서',
        'metadata':[{'label':'문서 유형','value':doc_type},{'label':'검토 지역','value':report.get('region_name') or '미확인'}, {'label':'작성 시각','value':display_created_at(report['created_at'])},{'label':'문서 상태','value':'사용자 작성·선택 검토안 / 기관 협의·사업 승인·재원 확보는 별도 확인'}], 'sections':sections}


def render_document_markdown(document):
    def safe(value):
        return re.sub(r'([\\`*_{}\[\]<>#!|])', r'\\\1', str(value or ''))
    lines = [f"# {safe(document['title'])}", '', safe(document.get('subtitle', '')), '']
    for item in document.get('metadata', []): lines.extend([f"{safe(item['label'])}: {safe(item['value'])}", ''])
    for section in document['sections']:
        lines.extend([f"## {safe(section['title'])}", ''])
        for paragraph in section.get('paragraphs', []): lines.extend([safe(paragraph), ''])
    return '\n'.join(lines)
