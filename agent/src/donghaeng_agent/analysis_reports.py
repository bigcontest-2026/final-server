"""Deterministic report ① from verified aggregates; no LLM or outside actions."""
from final_model.src.i5_display import calculate_i5_display_score, REFERENCE_SCOPE

from collections import Counter
from copy import deepcopy
import math
import re
from urllib.parse import urlsplit

from .signal_rules import CATEGORY_LABELS, SIGNAL_RULE_VERSION, THRESHOLD_PERCENT, classify_activity, previous_month
from .lifecycle_guide import build_lifecycle_interpretation, guide_provenance


REPORT_SCHEMA_VERSION = "analysis-report-1.2"
SEX_LABELS = {"MAN": "남성", "WMAN": "여성"}
AGE_LABELS = {"20G": "20대", "30G": "30대", "40G": "40대", "50G": "50대", "60GU": "60세 이상"}
DOMAIN_LABELS = {"E": "경제생활의 어려움", "P": "모임·단체 참여 부족", "O": "드문 외출",
                 "C": "가족·주변 사람과 교류 부족", "U": "도움을 부탁할 관계 부족"}
QUALITY_LABELS = {
    "SKT_ORIGINAL_BOUNDARY_CRS_UNVERIFIED": "통신 원본 공간 경계와 좌표 기준 미확인",
    "SKT_MONTHLY_COVERAGE_UNVERIFIED": "통신 월별 관측 범위 미확인",
    "SKT_PRODUCTION_RULES_UNVERIFIED": "통신 자료 생산 규칙 미확인",
    "CARD_MISSINGNESS_RULE_UNVERIFIED": "카드 누락 자료 처리 규칙 미확인",
    "CARD_INDUSTRY_SUBTYPE_UNVERIFIED": "카드 업종 세부 분류 미확인",
    "SKT_SEP_OCT_BOUNDARY_VERSION_CHANGED": "통신 9월·10월 공간 경계 버전이 달라 변화율 해석에 추가 검토 필요",
    "OBSERVATION_UNAVAILABLE": "선택 범위의 활동 자료 없음",
}



def i5_display_record(record):
    """Add the model projection to a copy; retain every original evidence field."""
    if record is None:
        return None
    result = deepcopy(record)
    result.update(calculate_i5_display_score(result.get("i5_reference")))
    return result


def i5_display_line(record):
    projected = i5_display_record(record)
    value = projected.get("i5_display_score") if projected else None
    score = "자료 없음" if value is None else f"{value:.2f}/10"
    return f"I5 표시점수: {score} · {REFERENCE_SCOPE} · 5점은 기준 평균"


def present_analysis_report(report):
    """Read-time display projection for saved reports, without rewriting SQLite."""
    result = deepcopy(report)
    context = result.get("context") or {}
    i5 = (context.get("i5") or {}).get("record")
    if i5:
        context["i5"]["record"] = i5_display_record(i5)
        # Only replace the server-generated numerical line. Keep authored research,
        # citations, timestamps and all other historical content as saved.
        result["markdown"] = re.sub(
            r"^I5: [^\n]*?/100 · 지역 전체 인구구성 기준$",
            lambda _: i5_display_line(i5), result.get("markdown", ""), flags=re.MULTILINE)
        result["markdown"] = result["markdown"].replace("영역 참고값: ", "영역 참고값(0~100점): ")
    for compared in (result.get("comparison_context") or {}).get("reports", []):
        evidence = ((compared.get("context") or {}).get("i5") or {}).get("record")
        if evidence:
            compared["context"]["i5"]["record"] = i5_display_record(evidence)
    return result


def _number(value):
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _formatted(value, digits=2, suffix="", signed=False):
    value = _number(value)
    return "자료 없음" if value is None else f"{value:+,.{digits}f}{suffix}" if signed else f"{value:,.{digits}f}{suffix}"


def _flag(value):
    return "확인됨" if value is True else "해당 없음" if value is False else "미확인"


def _safe(value):
    return str(value).replace("|", "／").replace("\n", " ").replace("\r", " ")


def _historical_context(row):
    if row is None:
        return {"status": "unavailable", "record": None,
                "reason": "이 관측에 정확히 연결된 C3 과거 참고가 없습니다."}
    allowed = ("record_id", "experiment_id", "month", "region", "sex", "age", "view", "calendar_type",
               "dataset", "industry", "time_band", "residence_sido",
               "baseline_method", "reference_start", "reference_end", "count", "amount", "daily_support",
               "signal_type", "review_flag", "threshold_pct", "quality_review_required", "limitations")
    return {"status": "available", "record": {key: deepcopy(row[key]) for key in allowed if key in row},
            "independent_evidence": False,
            "reason": "카드와 같은 출처의 과거 참고이며 독립적인 추가 신호로 세지 않습니다."}


def report_region_name(report):
    """Name the selected area while keeping an activity observation's SGG distinct."""
    if report.get("region_name"):
        return report["region_name"]
    i5 = (report.get("context", {}).get("i5", {}).get("record") or {})
    if report.get("scope", {}).get("dong_code"):
        return " ".join(dict.fromkeys(value for value in (i5.get("region_name"), i5.get("area_name")) if value)) or report["scope"]["dong_code"]
    return i5.get("region_name") or next((item.get("observation", {}).get("region_name")
        for item in report.get("assessments", []) if item.get("observation", {}).get("region_name")), report.get("scope", {}).get("region_code", "지역"))


def build_analysis_report(*, scope, current_rows, previous_rows, i5, mois, provenance):
    """Inputs come only from the server's validated bundle adapter."""
    previous = {tuple(row["key"][key] for key in ("region_code", "month", "sex_code", "age_code")): row
                for row in previous_rows}
    current = {tuple(item["behavior"]["key"][key] for key in ("sex_code", "age_code")): item for item in current_rows}
    sexes = [scope["sex_code"]] if scope.get("sex_code") else list(SEX_LABELS)
    ages = [scope["age_code"]] if scope.get("age_code") else list(AGE_LABELS)
    assessments, missing_groups = [], []
    for sex in sexes:
        for age in ages:
            key = {"region_code": scope["region_code"], "month": scope["month"], "sex_code": sex, "age_code": age}
            item = current.get((sex, age))
            if item is None:
                if scope.get("dong_code"):
                    missing_groups.append({"sex_code": sex, "age_code": age})
                    continue
                assessment = {"rule_version": SIGNAL_RULE_VERSION, "threshold_percent": THRESHOLD_PERCENT,
                    "category": "assessment_hold", "category_label": CATEGORY_LABELS["assessment_hold"], "scope": key,
                    "sources": {source: {"change_percent": None, "threshold_met": None,
                        "previous_change_percent": None, "previous_threshold_met": None} for source in ("skt", "card_a")},
                    "signals": {"joint_decline": None, "two_month_streak": None, "robust_candidate": None},
                    "reason_codes": ["OBSERVATION_UNAVAILABLE"], "reasons": ["선택한 지역·월·성별·연령의 활동 자료가 없습니다."],
                    "quality_status": "hold", "quality_reasons": ["OBSERVATION_UNAVAILABLE"],
                    "operational_alert_allowed": False, "evidence_ids": []}
                assessment["historical_reference"] = _historical_context(None)
            else:
                behavior = item["behavior"]
                prev = previous.get((scope["region_code"], previous_month(scope["month"]), sex, age))
                assessment = classify_activity(behavior, previous_behavior=prev)
                assessment["historical_reference"] = _historical_context(item.get("historical_reference"))
                assessment["observation"] = {"record_id": behavior["record_id"], "region_name": behavior.get("region_name"),
                    "sources": {source: {name: behavior["sources"][source].get(name)
                        for name in ("activity_daily", "previous_activity_daily", "unit", "calculation_status")}
                        for source in ("skt", "card_a")}}
            assessments.append(assessment)
    counts = Counter(item["category"] for item in assessments)
    summary = {"total": len(assessments), **{category: counts[category] for category in CATEGORY_LABELS}}
    context = {"i5": {"status": "available" if i5 else "unavailable", "record": i5_display_record(i5),
                      "role": "regional_population_composition_reference", "changes_category": False},
               "mois": {"status": "available" if mois else "unavailable", "record": deepcopy(mois),
                        "role": "population_and_household_context", "changes_category": False,
                        "automatic_10_to_6_group_join": False}}
    if scope.get("dong_code"):
        parent_name = i5.get("region_name") if i5 else next((item.get("observation", {}).get("region_name")
            for item in assessments if item.get("observation", {}).get("region_name")), scope["region_code"])
        context["activity_scope"] = {"spatial_unit": "sgg", "region_code": scope["region_code"],
            "region_name": parent_name, "role": "parent_sgg_reference", "is_dong_observation": False}
        context["mois"]["spatial_unit"] = "sgg"
        context["mois"]["region_code"] = scope["region_code"]
    limitations = [
        "5% 감소 기준은 검토용 초기 규칙이며 고립 위험을 검증한 임계값이 아닙니다.",
        "원자료의 품질 보류와 운영 경보 금지를 유지합니다. 범주는 자료 검토의 순서를 정하며 개인 고립·현재 위험을 판정하지 않습니다.",
        "활동은 시군구별 장소 유동·가맹점 거래 집계입니다. 거주자 개인이나 행정동 관측으로 옮기지 않습니다.",
        "I5는 전국 설문과 지역 인구구성의 참고값이며 고립률이 아닙니다. I5·MOIS·C3를 더해 위험점수를 만들지 않습니다.",
        "외부 원인 조사는 수행하지 않았습니다. 달력·기상·행사·관측 범위 변화는 확인할 가설입니다.",
        "대응안 추천 및 대응 보고서는 생성하지 않았습니다. 이 보고서는 현황 분석 첫 번째 산출물입니다.",
    ]
    if scope.get("dong_code"):
        limitations.insert(0, "선택 행정동의 I5를 표시하며, 활동 분류·C3·MOIS는 상위 시군구의 참고 자료입니다. 행정동별 활동 감소나 고립 상태로 해석하지 않습니다.")
    questions = ["휴일·행사·날씨 등 같은 지역·관측 기간의 맥락 자료가 있습니까?",
                 "통신 관측 범위와 카드 집계·누락 규칙이 전월과 동일합니까?",
                 "감소한 집단이 이용하는 지역 프로그램의 참여 현황과 이용 여건은 어떻습니까?"]
    report = {"schema_version": REPORT_SCHEMA_VERSION, "report_type": "observation_analysis",
              "scope": deepcopy(scope), "rule_version": SIGNAL_RULE_VERSION, "threshold_percent": THRESHOLD_PERCENT,
              "assessments": assessments, "summary": summary, "context": context,
              "quality_status": "hold", "operational_alert_allowed": False,
              "context_investigation_status": "not_performed", "recommendation_status": "not_requested",
              "limitations": limitations, "open_questions": questions, "provenance": deepcopy(provenance)}
    report["region_name"] = report_region_name(report)
    if scope.get("dong_code"):
        availability = "unavailable" if not assessments else "partial" if missing_groups else "available"
        report["activity_availability"] = {"status": availability, "spatial_unit": "sgg",
            "region_code": scope["region_code"], "requested_group_count": len(sexes) * len(ages),
            "observed_group_count": len(assessments), "missing_groups": missing_groups}
        report["report_status"] = "partial" if missing_groups else "complete"
        if not assessments:
            report["context_investigation_status"] = "not_applicable"
            report["limitations"].insert(0, "상위 시군구의 선택 월·집단 활동 자료가 없어 활동 범주와 원인을 판정하지 않았습니다. 선택 행정동의 I5를 담은 부분 보고서입니다.")
    report["lifecycle_interpretation"] = build_lifecycle_interpretation(scope, assessments)
    report["provenance"]["lifecycle_guide"] = guide_provenance(report["lifecycle_interpretation"])
    report["markdown"] = render_analysis_report(report)
    return report


def render_analysis_report(report):
    scope, context = report["scope"], report["context"]
    i5, mois = context["i5"]["record"], context["mois"]["record"]
    region_name = report_region_name(report)
    investigated = report.get("context_investigation_status") in ("completed", "no_evidence")
    title = "# 현황·원인 분석 보고서" if investigated else "# 현황·변화 신호 분석 보고서"
    lines = [title, "", f"지역: {_safe(region_name or scope['region_code'])} ({scope.get('dong_code') or scope['region_code']}) · 관측월: {scope['month']}",
             f"집단: {SEX_LABELS.get(scope.get('sex_code'), '전체 성별')} · {AGE_LABELS.get(scope.get('age_code'), '전체 연령')}",
             f"분류 규칙: {report['rule_version']} · 출처별 전월 대비 5% 이상 감소를 검토 기준으로 사용", ""]
    if scope.get("dong_code"):
        parent = context.get("activity_scope", {})
        lines.extend([f"선택 행정동: {_safe(region_name)} ({scope['dong_code']}). I5는 해당 행정동의 인구구성 참고값입니다.",
            f"활동 집계 지역: {_safe(parent.get('region_name') or scope['region_code'])} ({scope['region_code']}) · 통신·카드·C3·MOIS", ""])
    if investigated:
        lines.extend(_research_markdown(report["context_investigation"]))
    if report.get("lifecycle_interpretation"):
        lines.extend(_lifecycle_markdown(report["lifecycle_interpretation"]))
    lines.extend(["## 상위 시군구 활동 변화 참고" if scope.get("dong_code") else "## 활동 변화 범주", ""])
    if report["assessments"]:
        lines.extend(["| 집단 | 통신 변화 | 카드 변화 | 범주 | 판단 이유 |", "| --- | ---: | ---: | --- | --- |"])
    else:
        lines.append("상위 시군구의 선택 월·집단 활동 자료가 없어 활동 범주를 판정하지 않았습니다. 행정동 I5를 담은 부분 보고서입니다.")
    for assessment in report["assessments"]:
        key, sources = assessment["scope"], assessment["sources"]
        signals = assessment["signals"]
        details = f"2개월 연속 5% 동시 감소: {_flag(signals['two_month_streak'])}; 상대적 이례성: {_flag(signals['robust_candidate'])}"
        lines.append(f"| {SEX_LABELS[key['sex_code']]} {AGE_LABELS[key['age_code']]} | {_formatted(sources['skt'].get('change_percent'), suffix='%', signed=True)} | {_formatted(sources['card_a'].get('change_percent'), suffix='%', signed=True)} | {_safe(assessment['category_label'])} | {_safe(' / '.join(assessment['reasons']))} {_safe(details)} |")
    lines.extend(["", "### 연속성·이례성의 수치 근거", ""])
    for assessment in report["assessments"]:
        key, sources = assessment["scope"], assessment["sources"]
        robust = assessment.get("robust_evidence", {})
        diagnostics = robust.get("sources", {})
        lines.append(f"- {SEX_LABELS[key['sex_code']]} {AGE_LABELS[key['age_code']]}: 직전 월({previous_month(scope['month'])}) 변화율은 통신 {_formatted(sources['skt'].get('previous_change_percent'), suffix='%', signed=True)}, 카드 {_formatted(sources['card_a'].get('previous_change_percent'), suffix='%', signed=True)}. 모델 상대 이례성은 통신 z {_formatted(diagnostics.get('skt', {}).get('z'))}, 카드 z {_formatted(diagnostics.get('card_a', {}).get('z'))}, 모델 기준 {_formatted(robust.get('cutoff'))}.")
    lines.extend(["", "## 지역 배경 참고", ""])
    if i5:
        lines.extend([i5_display_line(i5),
                      "영역 참고값(0~100점): " + " · ".join(f"{key} {DOMAIN_LABELS.get(key, key)} {_formatted(value)}" for key, value in i5.get("domain_scores", {}).items()),
                      f"I5 근거: {_safe(i5['record_id'])}"])
    else:
        lines.append("동일 지역·월의 I5 자료가 없습니다.")
    if mois:
        share = _number(mois.get("one_person_household_share"))
        lines.append(f"{'상위 시군구 ' if scope.get('dong_code') else ''}MOIS 주민등록 인구 {_formatted(mois.get('population_total'), 0)}명 · 20세 이상 {_formatted(mois.get('population_20plus'), 0)}명 · 1인 세대 {_formatted(mois.get('one_person_households'), 0)} / 전체 세대 {_formatted(mois.get('households_total'), 0)} · 1인 세대 비중 {_formatted(None if share is None else share * 100, suffix='%')}.")
    else:
        lines.append("동일 상위 시군구·월의 MOIS 배경 자료가 없습니다." if scope.get("dong_code") else "동일 지역·월의 MOIS 배경 자료가 없습니다.")
    lines.extend(["", "## C3 과거 카드 참고", ""])
    for assessment in report["assessments"]:
        row = assessment["historical_reference"]["record"]
        key = assessment["scope"]
        group = f"{SEX_LABELS[key['sex_code']]} {AGE_LABELS[key['age_code']]}"
        if row:
            count, amount = row.get("count", {}), row.get("amount", {})
            method = "기준기간의 같은 요일별 일 중앙값과 비교" if row.get("baseline_method") == "fixed_reference_same_weekday_daily_median" else _safe(row.get("baseline_method", "기준 방법 미확인"))
            lines.append(f"- {group}: 거래 건수 변화 {_formatted(count.get('change_pct'), suffix='%', signed=True)}, 금액 변화 {_formatted(amount.get('change_pct'), suffix='%', signed=True)}. 기준기간 {_safe(row.get('reference_start', '미확인'))}~{_safe(row.get('reference_end', '미확인'))}, {method}. 원본 {_formatted(row.get('threshold_pct'), 0, '%')} 감소 기준: 건수 {_flag(count.get('decline_flag'))}, 금액 {_flag(amount.get('decline_flag'))}. 근거 {_safe(row['record_id'])}.")
        else:
            lines.append(f"- {group}: 정확히 연결된 과거 참고 자료 없음.")
    lines.append("C3의 과거 기준기간·동일 요일 비교와 원본 감소 기준은 위 활동 분류의 전월 대비 5% 기준과 별개입니다.")
    lines.extend(["", "## 살펴볼 사항", "", *["- " + _research_text(item) for item in report["open_questions"]],
                  "", "## 분석 자료 상세", ""])
    guide = report.get("lifecycle_interpretation")
    if guide:
        lines.extend(["### 문헌과 대상 범위", ""])
        lines.extend("- " + _research_text(item) for item in guide["boundaries"])
        for group in guide["groups"]:
            lines.append("- " + _research_text(group["boundaries"][0]))
        lines.append("")
    quality_reasons = sorted({reason for item in report["assessments"] for reason in item["quality_reasons"]})
    if quality_reasons:
        lines.extend(["### 자료 처리 정보", "", *["- " + QUALITY_LABELS.get(code, "자료 처리 정보") + f" ({_safe(code)})" for code in quality_reasons], ""])
    for family, source in report["provenance"].items():
        lines.append(f"- {family}: {_safe(source.get('run_id', '없음'))} · manifest SHA-256 {_safe(source.get('manifest_sha256', '없음'))}")
    for assessment in report["assessments"]:
        if assessment["evidence_ids"]:
            lines.append("- 활동 근거: " + ", ".join(_safe(value) for value in assessment["evidence_ids"]))
    return "\n".join(lines) + "\n"


def attach_context_investigation(base_report, investigation):
    """Preserve the frozen measurement report and create a separately saved derivative."""
    if investigation.get("status") not in ("completed", "no_evidence"):
        raise ValueError("analysis_research_incomplete")
    report = deepcopy(base_report)
    report["parent_report_id"] = base_report["report_id"]
    for field in ("report_id", "created_at", "storage_status", "reused"):
        report.pop(field, None)
    report["report_type"] = "context_cause_analysis"
    report["context_investigation_status"] = investigation["status"]
    report["context_investigation"] = deepcopy(investigation)
    report["comparison_context"] = deepcopy(investigation.get("comparison_context", {}))
    report["limitations"] = [item for item in report["limitations"]
                             if not item.startswith("외부 원인 조사는 수행하지 않았습니다.")]
    report["limitations"].append("외부 자료는 지역 맥락과 가능한 설명을 검토하는 근거입니다. 관측 변화의 원인이나 개인의 고립 상태를 확정하지 않습니다.")
    report["open_questions"] = list(dict.fromkeys([
        *report["open_questions"], *investigation.get("verification_questions", [])]))
    report["markdown"] = render_analysis_report(report)
    return report


def _research_text(value):
    # Model/source prose is data. Only the server's verified source list creates links.
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", text)


def _lifecycle_markdown(guide):
    source = guide["source"]
    lines = ["## 생애주기별 살펴볼 사항", "",
             f"문헌: {_research_text('·'.join(source['authors']))} ({source['year']}), 『{_research_text(source['title'])}』, {_research_text(source['publisher'])}.",
             "본문 참고 쪽: " + ", ".join(str(page) for page in source["pages"]),
             f"문헌 DOI: {_research_text(source['doi'])}",
             f"가이드 버전: {_research_text(guide['guide_version'])}", ""]
    for group in guide["groups"]:
        scope = group["scope"]
        lines.extend([f"### {SEX_LABELS[scope['sex_code']]} {_research_text(group['age_label'])}",
                      "관측 자료: " + ("있음" if group["observation_status"] == "available" else "없음")])
        lines.extend("- 확인 가설: " + _research_text(item["text"]) for item in group["hypotheses"])
        lines.extend("- 확인 질문: " + _research_text(item) for item in group["questions"])
        lines.append("")
    lines.extend(["함께 비교할 사항:", *["- " + _research_text(item) for item in guide["comparison_checks"]],
                  ""])
    return lines


def _research_markdown(research):
    refs = lambda values: " · ".join(_research_text(value) for value in values)
    lines = ["## 지역 맥락 조사 요약", "", _research_text(research.get("summary")), ""]
    comparison = research.get("comparison_context") or {}
    if comparison.get("reports"):
        lines.extend(["### 비교에 사용한 저장 관측", "", "같은 월·집단·집계 단위의 활동을 나란히 비교합니다.", ""])
        for item in comparison["reports"]:
            scope = item["scope"]
            group = f"{SEX_LABELS.get(scope.get('sex_code'), '전체 성별')} {AGE_LABELS.get(scope.get('age_code'), '전체 연령')}"
            lines.append(f"- {_research_text(item['region_name'])} · {_research_text(scope['month'])} · {_research_text(group)} · {'행정동' if scope.get('dong_code') else '시군구'}")
            lines.append(f"  - 분류 규칙: {_research_text(item['rule_version'])} · 저장 시각: {_research_text(item['created_at'])}")
            for family, version in item["provenance"].items():
                lines.append(f"  - {_research_text(family)}: {_research_text(version.get('run_id', '없음'))} · manifest SHA-256 {_research_text(version.get('manifest_sha256', '없음'))}")
        lines.append("")
    if research["status"] == "no_evidence":
        lines.extend(["해당 지역·기간의 추가 자료가 없습니다.", ""])
    lines.extend(["### 조사로 확인한 지역 맥락", ""])
    for item in research.get("observed_context", []):
        lines.append(f"- {_research_text(item['text'])} — 근거: {refs(item['source_ids'])}")
    if not research.get("observed_context"):
        lines.append("- 확인한 외부 지역 맥락 자료가 없습니다.")
    lines.extend(["", "### 가능한 원인 · 확인할 가설", ""])
    for item in research.get("possible_causes", []):
        lines.extend([f"- **{_research_text(item['title'])}**: {_research_text(item['explanation'])}",
                      f"  - 관련 자료: {refs(item['source_ids'])}"])
    if not research.get("possible_causes"):
        lines.append("- 추가로 살펴볼 배경은 아직 정리되지 않았습니다.")
    if research.get("lifecycle_checks"):
        labels = {"supported_context": "관련 지역 맥락 확인", "counter_context": "반박·약화하는 지역 맥락", "unverified": "미확인"}
        lines.extend(["", "### 생애주기별 지역 맥락", ""])
        for item in research["lifecycle_checks"]:
            scope = item["scope"]
            lines.extend([f"- {SEX_LABELS[scope['sex_code']]} {AGE_LABELS[scope['age_code']]} · {_research_text(item['hypothesis_id'])}: {labels[item['status']]}",
                          "  - " + _research_text(item["text"]),
                          "  - 지역 근거: " + (refs(item["source_ids"]) if item["source_ids"] else "확보하지 못함")])
    lines.extend(["", "### 외부 근거·출처", ""])
    for source in research.get("sources", []):
        url = source.get("url", "")
        parsed = urlsplit(url)
        safe_url = (parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
                    and not any(character in url for character in ('<', '>', '\\', '\n', '\r', '"')))
        title = _research_text(source.get("title") or source["source_id"])
        link = f"[{title}](<{url}>)" if safe_url else title
        lines.append(f"- **{_research_text(source['source_id'])}** · {link}")
        lines.append(f"  - 발행기관: {_research_text(source.get('publisher') or '미확인')} · 발행일: {_research_text(source.get('published_at') or '미확인')} · 자료 대상 기간: {_research_text(source.get('reference_period') or '미확인')} · 조회 시각: {_research_text(source.get('accessed_at') or '미확인')}")
        excerpt = source.get("supporting_excerpt") or source.get("excerpt")
        if excerpt:
            lines.append(f"  - 확인한 원문 발췌: {_research_text(excerpt)}")
        if source.get("geography_relation") or source.get("period_relation"):
            geography = {"same_region": "동일 지역", "broader_region": "더 넓은 지역", "other_region": "다른 지역", "unverified": "지역 일치 미확인"}
            period = {"same_period": "동일 기간", "broader_or_other_period": "더 넓거나 다른 기간", "unverified": "기간 일치 미확인"}
            lines.append(f"  - 분석 범위와의 관계: {geography.get(source.get('geography_relation'), '지역 관계 미확인')} · {period.get(source.get('period_relation'), '기간 관계 미확인')}")
    if not research.get("sources"):
        lines.append("- 확인한 외부 출처가 없습니다.")
    if research.get("search_queries"):
        lines.append("- 사용한 검색어: " + refs(research["search_queries"]))
    lines.append("")
    return lines
