"""Versioned research search routes and quoted service actions, without an LLM.

Area observations select topics to inspect, never establish a person's need or
eligibility. Recipient descriptions and existing tags are not offered services.
"""
from datetime import date
import hashlib
import math
import re


RULE_VERSION = "welfare-service-actions-v3"
PLAN_RULE_VERSION = "welfare-context-topics-v1"
CATEGORY_LABELS = {
    "social_connection": "사회적 관계·참여", "isolation_outreach": "고립·은둔 발굴·상담",
    "income_support": "소득·생활 안정", "daily_living": "일상생활·돌봄",
    "employment": "취업·경제활동", "mental_health": "정신건강·정서 지원",
    "health_care": "건강·의료", "housing": "주거 지원", "family_support": "가족·보호자 지원",
}
# This historical metadata belongs to the registered v3 extraction definition.
# Keep it unchanged: its fingerprint validates existing stored service links.
# It is not the active topic-selection policy below.
_V3_PLAN_METADATA = {
    "social_connection": ("priority", ("P", "C", "U"), "사회활동·교류 지원 내용 확인"),
    "isolation_outreach": ("priority", ("C", "U", "O"), "안부·방문 접근 지원 내용 확인"),
    "income_support": ("explore", ("E",), "경제적 지원 필요와 사업 조건 확인"),
    "daily_living": ("explore", ("O", "U"), "생활·가사 지원 필요 확인"),
    "employment": ("conditional", ("E",), "취업·구직 또는 직업훈련 수요 확인"),
    "mental_health": ("conditional", ("U",), "심리·정신건강 상담 수요 확인"),
    "health_care": ("conditional", ("O", "U"), "의료·건강관리 수요 확인"),
    "housing": ("conditional", (), "주거 지원 수요 확인"),
    "family_support": ("conditional", ("C", "U"), "양육·가족돌봄·가족상담 수요 확인"),
}
_PLAN = {
    category: ("conditional" if tier == "conditional" else "explore", domains, reason)
    for category, (tier, domains, reason) in _V3_PLAN_METADATA.items()
}
# Each pattern describes a service actually offered. In particular, recipient
# words such as 수급자/저소득/취약계층 never create an income-support action.
_OFFER = r"(?:지원|제공|지급|운영|실시|연계|알선|진행|공급|감면|면제|보조|대납|융자)"
_BRIDGE = r"[^\r\n.!?;。]{0,32}?"
_PATTERNS = {
    "social_connection": [
        rf"(?:사회\s*관계|사회\s*참여|친목|동아리|자조\s*모임|교류|문화\s*체험|(?<![가-힣])여가|문화[\s·ㆍ∙‧]*여가|취미|스포츠){_BRIDGE}{_OFFER}",
        r"(?:관계\s*형성|사회\s*참여|공동체|교류)\s*(?:활동|프로그램|모임)",
    ],
    "isolation_outreach": [
        r"(?:안부|안전)\s*(?:를\s*)?확인",
        r"(?:고립|위기)\s*(?:가구|대상자)\s*(?:를\s*)?발굴",
        rf"(?:가정\s*방문|방문\s*상담|찾아가는\s*상담){_BRIDGE}{_OFFER}",
    ],
    "income_support": [
        rf"(?:생계비|생활비|긴급\s*생계|주거급여|생계급여|양육비|양육수당|교육비|의료비|난방비|연료비|전기요금|통신요금|교통비|등록금|장학금|공과금|임차료|월세|현금|보조금|지원금|수당|바우처|이용권|보험료){_BRIDGE}{_OFFER}",
        r"(?:급여|지원금|수당)\s*(?:를\s*)?(?:지급|감면|보조)",
    ],
    "daily_living": [
        rf"(?:돌봄|가사(?!업)|식사(?!업)|반찬|도시락|급식|청소(?!년)|세탁|장보기|일상\s*생활){_BRIDGE}{_OFFER}",
        r"(?:이동|활동|가사)\s*지원\s*(?:서비스|사업)",
    ],
    "employment": [
        r"(?:취업|구직)\s*(?:상담|알선|훈련|교육|지원)",
        rf"(?:직업\s*훈련|일자리|일\s*경험|고용(?!\s*보험)){_BRIDGE}{_OFFER}",
    ],
    "mental_health": [
        r"(?:정신\s*건강|심리|우울|자살\s*예방)\s*(?:상담|치료|검사|지원)",
    ],
    "health_care": [
        rf"(?:진료|건강\s*검진|방문\s*간호|재활\s*치료|의료비|예방\s*접종){_BRIDGE}{_OFFER}",
        r"(?:방문\s*건강\s*관리|방문\s*간호|건강\s*검진)\s*(?:서비스|사업)",
    ],
    "housing": [
        rf"(?:임대\s*주택|주택\s*개보수|집\s*수리|주거\s*환경\s*개선|주거\s*이전|임차료|월세){_BRIDGE}{_OFFER}",
    ],
    "family_support": [
        rf"(?:가족\s*상담|부모\s*교육|양육\s*상담|아이\s*돌봄|아동\s*돌봄|양육비|양육수당){_BRIDGE}{_OFFER}",
        r"(?:가족\s*관계|부모\s*자녀\s*관계)\s*(?:개선|회복)\s*(?:프로그램|지원)",
    ],
}
RULE_DEFINITION = {
    "rule_version": RULE_VERSION,
    "purpose": "research_candidate_search_only",
    "source_fields": ["support_content"],
    "excluded_fields": ["target_text", "selection_text", "source_categories"],
    "category_plan": {key: {"tier": row[0], "domains": list(row[1]), "confirmation": row[2]}
                      for key, row in _V3_PLAN_METADATA.items()},
    "service_patterns": _PATTERNS,
    "recipient_markers": ["수급자", "수급권자", "대상자", "이용자", "수혜자", "참여자", "신청자"],
    "staff_support_markers": ["종사자", "인건비", "처우", "제공기관", "수행기관", "운영기관"],
    "negated_service_rule": "skip_negated_clause",
    "target_clause_rule": "skip_target_heading_or_recipient_modifier",
    "generic_visit_context": "same_quote_requires_isolation_or_outreach_context",
    "leisure_cash_rule": "leisure_cost_alone_not_social_connection",
    "leisure_token_rule": "standalone_leisure_or_explicit_culture_leisure_not_benefit_particle",
    "employed_recipient_rule": "employed_worker_description_not_employment_service",
    "manual_approval": False,
    "individual_eligibility_confirmed": False,
}


def category_plan(domains, confirmed_needs=()):
    """Explore equally ranked topics; only confirmed needs take precedence.

    The separate plan version must not invalidate v3 service extraction links.
    Age alone is neither an input nor confirmation of a support need.
    """
    domains, confirmed = set(domains), set(confirmed_needs)
    if not domains <= set("EPOCU"):
        raise ValueError("invalid I5 domain")
    if not confirmed <= set(_PLAN):
        raise ValueError("confirmed_needs must contain known category codes")
    selected, pending = [], []
    for category, (tier, relevant, reason) in _PLAN.items():
        matched = sorted(domains.intersection(relevant))
        item = {"category_code": category, "category_id": category,
                "label": CATEGORY_LABELS[category], "category_name": CATEGORY_LABELS[category],
                "matched_domains": matched, "reason": reason, "rule_version": RULE_VERSION,
                "plan_rule_version": PLAN_RULE_VERSION}
        if category in confirmed:
            selected.append({**item, "tier": "confirmed_need", "need_confirmed": True,
                             "selection_basis": "confirmed_need"})
        elif tier != "conditional" and matched:
            selected.append({**item, "tier": "explore", "need_confirmed": False,
                             "selection_basis": "observation_domain_topic"})
        elif tier == "conditional":
            pending.append({**item, "required_confirmation": reason,
                            "need_confirmed": False, "selection_basis": "requires_confirmation"})
    # Directly confirmed demand takes precedence over observation-led browsing.
    # Alphabetical order within a tier is only a stable display order.
    tier_order = {"confirmed_need": 0, "explore": 1}
    selected.sort(key=lambda item: (tier_order[item["tier"]], item["category_code"]))
    pending.sort(key=lambda item: item["category_code"])
    return {"rule_version": RULE_VERSION, "plan_rule_version": PLAN_RULE_VERSION,
            "selection_basis": "confirmed_needs_then_observation_topics",
            "within_tier_order": "category_code_ascending", "within_tier_order_is_priority": False,
            "ordering_note": "같은 탐색 단계 안의 분야 표시 순서는 우선순위가 아닙니다.",
            "status": "selected" if selected else "held",
            "selected_categories": [item["category_code"] for item in selected],
            "category_tiers": {item["category_code"]: item["tier"] for item in selected},
            "categories": selected, "pending_categories": [item["category_code"] for item in pending],
            "pending": pending, "quality_status": "hold", "operational_alert_allowed": False}


def _sentences(text):
    for match in re.finditer(r"[^\r\n;。!?]+", text):
        # Decimal amounts remain intact; punctuation alone is never evidence.
        raw = match.group()
        leading = len(raw) - len(raw.lstrip())
        quote = raw.strip()
        if quote:
            start = match.start() + leading
            yield quote, start, start + len(quote)


def extract_program_services(program):
    """Return one reproducible offered-action quote per category, never targets.

    The title may name the program but cannot replace a missing support body.
    Offsets and SHA256 always refer to the original support_content string.
    """
    content = program.get("support_content")
    if not isinstance(content, str) or not content.strip():
        return []
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    actions = []
    for category, patterns in _PATTERNS.items():
        found = None
        for quote, start, end in _sentences(content):
            # Explicit exclusions do not prove a service is supplied. A whole
            # clause is held conservatively when its negation is ambiguous.
            if re.search(r"미지원|지원\s*제외|제외\s*대상|하지\s*않|않습니다|제공\s*없|지원\s*없|지급\s*없", quote):
                continue
            if re.match(r"(?:지원\s*대상|대상자|신청\s*자격)\s*[:：]", quote):
                continue
            for index, pattern in enumerate(patterns):
                matched = re.search(pattern, quote)
                if matched is None:
                    continue
                action = matched.group()
                # '수당 수급자에게 돌봄 제공' describes a recipient, not a
                # financial benefit. Do not cross a recipient clause.
                if re.search(r"(?:수급자|수급권자|대상자|이용자|수혜자|참여자|신청자)", action):
                    continue
                if re.match(r"\s*(?:사업\s*)?(?:대상|수급자|이용자|참여자|신청자)", quote[matched.end():]):
                    continue
                if category == "employment" and re.search(r"근로자|노동자|고용\s*[·ㆍ]?\s*파견된", action):
                    continue
                if category == "social_connection" and re.search(r"(?:여가|문화|체험)(?:활동|체험)?비", action):
                    continue
                if category == "isolation_outreach" and "안부" not in action:
                    if not re.search(r"고립|고독|독거|은둔|위기\s*(?:가구|대상자)|관계\s*단절", quote):
                        continue
                if category != "income_support":
                    # Funding staff, providers, or wages is not the resident
                    # care service whose name occurs in the recipient phrase.
                    if re.search(r"종사자|인건비|처우|제공\s*기관|수행\s*기관|운영\s*기관", action):
                        continue
                    if category == "daily_living" and "수당" in action:
                        continue
                    if re.match(r"\s*(?:(?:제공\s*|수행\s*|운영\s*)기관|종사자|제공자|인력|인건비|처우)", quote[matched.end():]):
                        continue
                found = {"category_code": category, "category_id": category,
                         "label": CATEGORY_LABELS[category],
                         "service_action": action, "source_field": "support_content",
                         "source_quote": quote, "start": start, "end": end,
                         "quote_start": start, "quote_end": end, "content_hash": digest,
                         "reason": "지원내용 원문에 명시된 제공 행위로 탐색 분야를 연결합니다.",
                         "rule_id": f"{category}:{index + 1}", "rule_version": RULE_VERSION,
                         "review_status": "rule_extracted",
                         "evidence": {"field": "support_content", "quote": quote,
                                      "start": start, "end": end}}
                break
            if found:
                break
        if found:
            actions.append(found)
    return actions


def _verified_links(program):
    """Use stored links only when their quote still matches the current body."""
    content = program.get("support_content") or ""
    stored = program.get("support_links")
    if stored is None:
        return extract_program_services(program)
    verified = []
    if not isinstance(stored, list) or not isinstance(content, str):
        return verified
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    for link in stored:
        if not isinstance(link, dict):
            continue
        category = link.get("category_code", link.get("category_id"))
        quote = link.get("source_quote")
        start = link.get("quote_start", link.get("start"))
        end = link.get("quote_end", link.get("end"))
        if (category not in _PLAN or link.get("source_field") != "support_content"
                or link.get("rule_version") != RULE_VERSION
                or link.get("review_status") not in {"auto_extracted", "rule_extracted", "reviewed", "approved"}
                or not isinstance(quote, str) or not quote
                or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(content) or content[start:end] != quote
                or link.get("content_hash", link.get("source_text_sha256")) != digest
                or not isinstance(link.get("service_action"), str)
                or not link["service_action"] or link["service_action"] not in quote):
            continue
        verified.append(dict(link))
    return verified


def review_support_candidates(programs, mapping, *, region_context=None,
                              age_range=None, sex=None, as_of):
    """Review search candidates without granting eligibility or source approval."""
    day = date.fromisoformat(as_of)
    if age_range is not None and (len(age_range) != 2 or age_range[0] > age_range[1]):
        raise ValueError("invalid requested age range")
    selected = set(mapping["selected_categories"])
    if not selected <= set(_PLAN):
        raise ValueError("invalid mapping categories")
    output = []
    for program in programs:
        links = _verified_links(program)
        matching = [link for link in links
                    if link.get("category_code", link.get("category_id")) in selected]
        unknown = ["개인별 최종 자격 및 현재 접수 여부", "실제 지원 필요 및 활동 감소 원인"]
        excluded = []
        if not matching and links:
            excluded.append("확인된 제공서비스가 선택한 탐색 분야와 일치하지 않음")
        if not matching:
            unknown.append("선택한 분야의 제공서비스 원문 근거 확인")
        content = program.get("support_content")
        source_available = bool(isinstance(content, str) and content.strip() and program.get("source_url"))
        if not source_available:
            unknown.append("공식 출처 링크 및 지원내용 원문 확인")
        if program.get("source_verified") is not True:
            unknown.append("자동 추출한 지원내용의 담당자 검토")

        # An administering jurisdiction is not a residence eligibility rule.
        unknown.append("사업 적용 지역 및 거주 요건")
        lower, upper = program.get("min_age"), program.get("max_age")
        age_status = program.get("age_status")
        def valid_bound(value):
            return (value is None or isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value))
        structured_age = (age_status in {"bounded", "explicit", "parsed", "verified", "known", "specified"}
                          and (lower is not None or upper is not None)
                          and valid_bound(lower) and valid_bound(upper)
                          and (lower is None or upper is None or lower <= upper))
        if age_range is not None and structured_age:
            low = lower if lower is not None else -math.inf
            high = upper if upper is not None else math.inf
            if age_range[1] < low or age_range[0] > high:
                excluded.append("연령 구간 불일치")
            elif not low <= age_range[0] <= age_range[1] <= high:
                unknown.append("연령 구간 일부만 해당")
            else:
                unknown.append("원문 연령 조건의 개별 적용 확인")
        else:
            unknown.append("사업 연령 조건")
        gender = program.get("gender_scope")
        known_gender = {"male": "MAN", "female": "WMAN", "MAN": "MAN", "WMAN": "WMAN"}
        if sex in {"MAN", "WMAN"} and gender in known_gender and known_gender[gender] != sex:
            excluded.append("명시된 성별 조건 불일치")
        else:
            unknown.append("성별 및 기타 개인·가구 조건")
        boundaries = {}
        for field, direction in (("application_start", "before"), ("application_end", "after")):
            value = program.get(field) or program.get("start_date" if direction == "before" else "end_date")
            if not value:
                unknown.append("신청 기간 원문 확인")
                continue
            try:
                boundary = date.fromisoformat(str(value)[:10])
            except ValueError:
                unknown.append("신청 기간 원문 확인")
                continue
            boundaries[direction] = boundary
        invalid_window = ("before" in boundaries and "after" in boundaries
                          and boundaries["before"] > boundaries["after"])
        if invalid_window:
            unknown.append("신청 기간 시작·종료일 모순 확인")
        else:
            for direction, boundary in boundaries.items():
                if (direction == "before" and day < boundary) or (direction == "after" and day > boundary):
                    excluded.append("신청 기간 불일치")
        if program.get("period_status") == "ended" or program.get("application_status") == "closed":
            excluded.append("신청 기간 종료")
        reasons = [{"category_code": link.get("category_code", link.get("category_id")),
                    "label": CATEGORY_LABELS[link.get("category_code", link.get("category_id"))],
                    "tier": mapping["category_tiers"].get(link.get("category_code", link.get("category_id"))),
                    "service_action": link["service_action"], "service_evidence": link,
                    "reason": f"{CATEGORY_LABELS[link.get('category_code', link.get('category_id'))]}: 사업 원문에 ‘{link['service_action']}’ 제공 내용이 명시되어 있습니다.",
                    "mapping_rule_version": mapping["rule_version"],
                    "plan_rule_version": mapping.get("plan_rule_version"),
                    "selection_basis": next((item.get("selection_basis") for item in mapping.get("categories", [])
                        if item["category_code"] == link.get("category_code", link.get("category_id"))), None)}
                   for link in matching]
        status = "excluded" if excluded else "source_hold" if not source_available or not matching else "needs_confirmation"
        output.append({**program, "status": status, "service_links": links,
                       "recommendation_reasons": reasons,
                       "unconfirmed_conditions": list(dict.fromkeys(unknown)),
                       "exclusion_reasons": list(dict.fromkeys(excluded)),
                       "mapping_rule_version": RULE_VERSION,
                       "plan_rule_version": mapping.get("plan_rule_version"),
                       "quality_status": "hold", "operational_alert_allowed": False})
    return output
