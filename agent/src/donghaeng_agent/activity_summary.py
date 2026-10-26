"""Plain-language activity interpretation with server-owned numbers and scope."""
import hashlib
import json
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .analysis_report_store import encoded
from .analysis_reports import report_region_name
from .model_input import ModelInputRejected, prepare_model_input
from .provider import ProviderUnavailable, create_provider, model_configuration
from .signal_rules import CATEGORY_LABELS

PROMPT_VERSION = "activity-summary-2.1"
INSTRUCTIONS = """지역 활동 변화를 읽는 담당자에게 한국어로 간결한 해석을 작성하세요.
analysis.facts는 서버가 수치·분류·범위를 검증한 관측입니다. selected_fact_ids에 핵심 id를 1~3개 넣으세요.
집단이 여러 개면 overview를 먼저 선택하고 변화가 두드러지는 집단을 고르세요. 같은 id는 중복하지 마세요.
서버가 선택한 관측의 수치와 집단, 활동 지역명을 표시하므로 interpretation에는 숫자·지역명·집단명을 반복하지 마세요.
interpretation.meaning에는 통신·카드 변화가 이동·방문과 소비 활동에서 어떤 의미인지 설명하세요.
통신은 장소 유동, 카드는 지역 가맹점 거래이며 거주자 개인의 행동과 동일시하지 마세요.
analysis.directions는 서버가 계산한 출처별 증감 방향입니다. 이를 따라 두 출처가 모두 감소할 때, 한쪽만 감소할 때, 증가할 때의 차이를 구별하세요.
전체 집단을 일부 집단의 변화로 일반화하지 말고, 자료 없는 집단의 변화는 만들어 쓰지 마세요.
interpretation.possible_background에는 계절·휴일·생활 일정 등 가능한 배경을 짧게 제시하세요.
지역 조사 결과가 입력에 없으므로 특정 행사·기상·사건이 실제 있었다고 쓰지 마세요.
추정은 '영향이 있을 수 있습니다'처럼 자연스럽게 표현하고 인과관계를 확정하지 마세요.
interpretation.suggested_review에는 다음 달 흐름이나 지역 프로그램의 참여 여건 등 살펴볼 대응을 제안하세요.
감소가 없으면 감소 대응을 권하지 말고 현재 흐름 유지·변화 추이를 살펴보는 방향으로 쓰세요.
근거·검증·품질 보류·운영 경보·해석 한계에 대한 안내나 면책 문장은 작성하지 마세요.
외부 검색, 도구 호출, 새 수치·사업·출처, 개인 고립 진단·위험 확정은 생성하지 마세요.
output_contract 모양의 JSON을 응답 text에 담고 evidence_ids에는 report_id만 넣으세요.
scope와 quality_status, operational_alert_allowed는 내부 JSON 값에만 입력과 똑같이 반환하세요."""


class Interpretation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    meaning: str = Field(min_length=1, max_length=600)
    possible_background: str = Field(min_length=1, max_length=600)
    suggested_review: str = Field(min_length=1, max_length=600)

    @field_validator("meaning", "possible_background", "suggested_review")
    @classmethod
    def narrative_only(cls, value):
        # All numeric observations are rendered from server-authored facts.
        if not value.strip() or re.search(r"\d|https?://|quality_status|operational_alert_allowed", value):
            raise ValueError("invalid_interpretation")
        return value.strip()


SEX_LABELS = {"MAN": "남성", "WMAN": "여성", None: "전체 성별"}
AGE_LABELS = {"20G": "20대", "30G": "30대", "40G": "40대", "50G": "50대", "60GU": "60세 이상", None: "전체 연령"}


class SummaryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    report_id: str
    scope: dict
    selected_fact_ids: list[str] = Field(min_length=1, max_length=3)
    interpretation: Interpretation
    quality_status: str
    operational_alert_allowed: bool


def public_analysis(report):
    month = report["scope"]["month"]
    period = f"{month[:4]}년 {int(month[4:])}월"
    facts = []
    directions = {}
    if len(report["assessments"]) > 1:
        counts = "·".join(f"{label} {report['summary'].get(key, 0)}개" for key, label in CATEGORY_LABELS.items()
                          if report["summary"].get(key, 0))
        facts.append({"id": "overview", "text": f"{period} 분석 대상 {report['summary']['total']}개 집단의 분류는 {counts}입니다."})
    for index, assessment in enumerate(report["assessments"]):
        group = assessment["scope"]
        values = []
        for key, label in (("skt", "통신"), ("card_a", "카드")):
            value = assessment.get("sources", {}).get(key, {}).get("change_percent")
            values.append(f"{label} {value:+.2f}%" if value is not None else f"{label} 비교 자료 없음")
        directions[f"observation_{index}"] = {
            source: ("unavailable" if assessment.get("sources", {}).get(source, {}).get("change_percent") is None
                     else "decrease" if assessment["sources"][source]["change_percent"] < 0
                     else "increase" if assessment["sources"][source]["change_percent"] > 0 else "stable")
            for source in ("skt", "card_a")}
        group_label = f"{AGE_LABELS.get(group.get('age_code'), '연령 미확인')} {SEX_LABELS.get(group.get('sex_code'), '성별 미확인')}"
        facts.append({"id": f"observation_{index}", "text": f"{period} {group_label}의 전월 대비 변화율은 {', '.join(values)}입니다."})
    if not facts:
        facts.append({"id": "unavailable", "text": f"{period} 선택한 범위의 활동 비교 자료가 없습니다."})
    return {"scope": report["scope"], "region_name": report_region_name(report), "facts": facts, "directions": directions,
            "activity_region_name": (report.get("context", {}).get("activity_scope", {}).get("region_name")
                or next((item.get("observation", {}).get("region_name") for item in report["assessments"]
                         if item.get("observation", {}).get("region_name")), report_region_name(report))),
            "quality_status": report["quality_status"], "operational_alert_allowed": report["operational_alert_allowed"]}


def summary_key(report, provider):
    value = {"report": report, "facts": public_analysis(report), "model": model_configuration(provider),
             "prompt_version": PROMPT_VERSION, "instructions": INSTRUCTIONS}
    # Transport-only reuse metadata is not part of the immutable report snapshot.
    value["report"] = {key: item for key, item in report.items() if key != "reused"}
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _check_interpretation(draft, analysis):
    """Reject obvious scope, event and direction contradictions in free prose.

    These conservative checks complement the grounded prompt; they are not a
    general factual/semantic verifier. Numbers and observation scope are always
    rendered separately from immutable facts.
    """
    prose = " ".join(draft.interpretation.model_dump().values())
    if re.search(r"경보|고립\s*(?:위험|확률|상태|진단|확정|률)|개인.{0,12}(?:위험|진단)|남성|여성|청년|고령|노인|서울|부산|대구|인천|광주|대전|울산|세종|제주|강원|경기|충청|전라|경상", prose):
        raise ValueError("unsupported_interpretation_claim")
    # Scope names belong exclusively to the server-authored preamble/facts.
    for token in re.findall(r"[가-힣]+", prose):
        name = re.sub(r"(?:에서는|에서|으로|의|은|는|이|가|을|를|과|와)$", "", token)
        if len(name) >= 3 and name.endswith(("시", "군", "구", "동")) and not name.endswith(("활동", "인구", "가구", "욕구", "요구", "도구", "연구")) and name != "필요시":
            raise ValueError("interpretation_scope_name")
    background = draft.interpretation.possible_background
    if not re.search(r"수(?:도)?\s*있|가능|살펴|비교|점검|고려", background):
        raise ValueError("asserted_background")
    if re.search(r"(?:행사|축제|사업|정책).{0,25}(?:취소|개최|시행|종료|시작)(?:됐|되었|했|했기|되었기)", background):
        raise ValueError("asserted_local_event")
    selected = [analysis["directions"][key] for key in draft.selected_fact_ids if key in analysis["directions"]]
    if not selected:
        selected = list(analysis["directions"].values())
    meaning = draft.interpretation.meaning
    down, up = r"감소|줄|위축|감퇴", r"증가|늘|확대|활발"

    def affirmative_matches(pattern):
        for match in re.finditer(pattern, meaning):
            tail = re.split(r"[,。.!?]", meaning[match.end():], maxsplit=1)[0]
            if not re.search(r"지\s*않|아닙|아니", tail):
                yield match

    for direction, words in (("decrease", down), ("increase", up)):
        for match in affirmative_matches(rf"(?:함께|모두|동반)[^,.!?]{{0,25}}?(?:{words})"):
            # An overview may contrast a selected declining group with other
            # groups. Check that sentence against those groups, not the focus.
            sentence_start = max(meaning.rfind(mark, 0, match.start()) for mark in ".!?") + 1
            prefix = meaning[sentence_start:match.start()]
            if re.search(r"다른 집단|일부 집단|집단에 따라", prefix):
                candidates = list(analysis["directions"].values())
                valid = any(all(row[source] == direction for source in ("skt", "card_a")) for row in candidates)
            else:
                valid = all(row[source] == direction for row in selected for source in ("skt", "card_a"))
            if not valid:
                raise ValueError("joint_direction_mismatch")
        for source, label, other in (("skt", "통신|장소 유동", "카드|소비|가맹점"), ("card_a", "카드|가맹점 거래", "통신|유동")):
            for match in affirmative_matches(rf"(?:{label})(?:(?!(?:{other}))[^,.!?]){{0,30}}?(?:{words})"):
                if not any(row[source] == direction for row in selected):
                    raise ValueError("source_direction_mismatch")



def summarize_activity(report, provider):
    analysis = public_analysis(report)
    if not any(item.get("sources", {}).get(source, {}).get("change_percent") is not None
               for item in report["assessments"] for source in ("skt", "card_a")):
        month = report["scope"]["month"]
        return f"{month[:4]}년 {int(month[4:])}월 선택한 범위의 활동 비교 자료가 없습니다. 다른 월이나 집단을 선택해 주세요."
    payload = {"task": INSTRUCTIONS, "report_id": report["report_id"], "analysis": analysis,
               "allowed_evidence_ids": [report["report_id"]],
               "output_contract": {"report_id": report["report_id"], "scope": report["scope"],
                   "selected_fact_ids": [analysis["facts"][0]["id"]],
                   "interpretation": {"meaning": "변화의 생활상 의미", "possible_background": "가능한 배경",
                                      "suggested_review": "살펴볼 대응"},
                   "quality_status": report["quality_status"], "operational_alert_allowed": False}}
    try:
        prepared = prepare_model_input(payload)
    except ModelInputRejected as exc:
        raise ProviderUnavailable(exc.code) from None
    response = provider.decide([{"role": "user", "content": json.dumps(prepared, ensure_ascii=False)}], allow_tools=False)
    try:
        if (not isinstance(response, dict) or response.get("calls") or
                response.get("evidence_ids") != [report["report_id"]] or
                not isinstance(response.get("text"), str) or len(response["text"]) > 8000):
            raise ValueError("invalid_envelope")
        draft = SummaryDraft.model_validate(json.loads(response["text"], object_pairs_hook=_unique))
        facts = {fact["id"]: fact["text"] for fact in analysis["facts"]}
        ids = draft.selected_fact_ids
        if (draft.report_id != report["report_id"] or draft.scope != report["scope"] or
                draft.quality_status != report["quality_status"] or draft.operational_alert_allowed is not False or
                len(ids) != len(set(ids)) or any(key not in facts for key in ids) or
                ("overview" in facts and ids[0] != "overview")):
            raise ValueError("invalid_selection")
        _check_interpretation(draft, analysis)
        return " ".join([f"{analysis['activity_region_name']}의 활동을 보면,",
                         *(facts[key] for key in ids), *draft.interpretation.model_dump().values()])
    except (ValidationError, ValueError, TypeError, KeyError):
        raise ProviderUnavailable("activity_summary_output_invalid") from None
