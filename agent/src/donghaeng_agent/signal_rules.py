"""Deterministic, research-only classification of observed activity changes.

The model's positive-Q flags describe any decline. This layer independently
applies the agreed 5% threshold, including to the preceding month's evidence.
"""
from collections.abc import Mapping
import math


SIGNAL_RULE_VERSION = "activity-review-5pct-v1"
THRESHOLD_PERCENT = 5
SOURCE_NAMES = ("skt", "card_a")
CATEGORY_LABELS = {
    "no_threshold_decline": "기준 이상 감소 미포착",
    "single_source_decline": "단일 감소 관찰",
    "joint_decline": "동시 감소 관찰",
    "priority_review": "우선 검토 후보",
    "assessment_hold": "판정 보류",
}


def previous_month(month):
    """Return the exact preceding calendar month, or None for invalid input."""
    if not isinstance(month, str) or len(month) != 6 or not month.isascii() or not month.isdigit():
        return None
    year, number = int(month[:4]), int(month[4:])
    if not 1 <= year <= 9999 or not 1 <= number <= 12 or (year == 1 and number == 1):
        return None
    return f"{year - (number == 1):04d}{12 if number == 1 else number - 1:02d}"


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _valid_scope(key):
    region = key.get("region_code")
    return (
        isinstance(region, str) and len(region) == 5 and region.isascii() and region.isdigit()
        and previous_month(key.get("month")) is not None
        and key.get("sex_code") in ("MAN", "WMAN")
        and key.get("age_code") in ("20G", "30G", "40G", "50G", "60GU")
    )


def _source_change(record, source, *, comparable):
    data = _mapping(_mapping(record.get("sources")).get(source))
    change = _finite_number(data.get("change_percent"))
    threshold_met = (
        change <= -THRESHOLD_PERCENT
        if comparable and data.get("calculation_status") == "computed" and change is not None
        else None
    )
    return change, threshold_met


def _both(values):
    if any(value is False for value in values):
        return False
    return True if all(value is True for value in values) else None


def classify_activity(behavior, previous_behavior=None):
    """Classify one SGG/month/sex/age observation without enabling alerts.

Missing data stays unknown, and an invalid preceding observation cannot supply
continuity or an evidence ID. I5, MOIS and C3 do not add votes to this rule.
"""
    current = _mapping(behavior)
    prior = _mapping(previous_behavior)
    key = dict(_mapping(current.get("key")))
    quality = _mapping(current.get("quality"))
    quality_status = quality.get("status", current.get("quality_status", "unknown" if current else "unavailable"))
    quality_status = quality_status if isinstance(quality_status, str) else "unknown"
    quality_reasons = quality.get("reasons", [])
    quality_reasons = [reason for reason in quality_reasons if isinstance(reason, str)] if isinstance(quality_reasons, list) else []
    codes, reasons = [], []
    valid_scope = _valid_scope(key) and current.get("spatial_unit") == "sgg"
    # The model marks the overall comparison unavailable when just one source
    # is missing. Preserve the other source's explicitly computed observation.
    comparable = valid_scope and current.get("comparison_status") in ("computed", "unavailable")
    prior_key = _mapping(prior.get("key"))
    prior_matches = bool(
        valid_scope and prior and prior.get("spatial_unit") == "sgg"
        and _valid_scope(prior_key)
        and prior_key.get("month") == previous_month(key["month"])
        and all(prior_key.get(field) == key.get(field) for field in ("region_code", "sex_code", "age_code"))
    )
    prior_comparable = prior_matches and prior.get("comparison_status") == "computed"
    sources = {}
    for source in SOURCE_NAMES:
        change, met = _source_change(current, source, comparable=comparable)
        prior_change, prior_met = _source_change(prior, source, comparable=prior_comparable) if prior_matches else (None, None)
        sources[source] = {
            "change_percent": change,
            "threshold_met": met,
            "previous_change_percent": prior_change,
            "previous_threshold_met": prior_met,
        }
    flags = [sources[name]["threshold_met"] for name in SOURCE_NAMES]
    previous_flags = [sources[name]["previous_threshold_met"] for name in SOURCE_NAMES]
    joint = _both(flags)
    streak = bool(joint and _both(previous_flags)) if prior_comparable and all(flag is not None for flag in flags + previous_flags) else None
    robust_value = _mapping(current.get("robust")).get("candidate")
    robust = robust_value if type(robust_value) is bool and comparable else None
    robust_record = _mapping(current.get("robust"))
    robust_evidence = {"status": robust_record.get("status", "unavailable"),
                       "cutoff": _finite_number(robust_record.get("cutoff")), "sources": {}}
    for source in SOURCE_NAMES:
        diagnostic = _mapping(_mapping(_mapping(current.get("sources")).get(source)).get("robust"))
        robust_evidence["sources"][source] = {
            "status": diagnostic.get("status", "unavailable"),
            "z": _finite_number(diagnostic.get("z")),
            "mad": _finite_number(diagnostic.get("mad")),
            "candidate": diagnostic.get("candidate") if type(diagnostic.get("candidate")) is bool else None,
        }

    if joint and (streak is True or robust is True):
        category = "priority_review"
        codes.append("JOINT_5PCT_DECLINE_WITH_SUPPORT")
        reasons.append("통신·카드가 각각 5% 이상 감소했고 아래 보조 조건이 확인되어 우선 검토 후보로 분류합니다.")
    elif joint:
        category = "joint_decline"
        codes.append("BOTH_SOURCES_DECLINE_AT_LEAST_5PCT")
        reasons.append("통신·카드가 각각 전월보다 5% 이상 감소했습니다.")
    elif any(flag is True for flag in flags):
        category = "single_source_decline"
        codes.append("ONE_SOURCE_DECLINES_AT_LEAST_5PCT")
        reasons.append("한 활동 원천에서 전월 대비 5% 이상 감소가 확인되었습니다.")
    elif any(flag is None for flag in flags):
        category = "assessment_hold"
        codes.append("ACTIVITY_COMPARISON_UNAVAILABLE")
        reasons.append("같은 지역·월·성·연령의 두 활동 원천을 비교할 수 없어 판단을 보류합니다.")
    else:
        category = "no_threshold_decline"
        codes.append("NO_SOURCE_DECLINES_AT_LEAST_5PCT")
        reasons.append("통신·카드 모두 전월 대비 5% 이상 감소 기준에 해당하지 않습니다.")
    if category == "single_source_decline" and any(flag is None for flag in flags):
        codes.append("OTHER_SOURCE_UNAVAILABLE")
        reasons.append("다른 활동 원천은 비교할 수 없어 동시 감소 여부는 미확인입니다.")
    if not current:
        codes.append("ACTIVITY_EVIDENCE_MISSING")
    elif not valid_scope:
        codes.append("ACTIVITY_SCOPE_INVALID")
    if prior and not prior_matches:
        codes.append("PREVIOUS_OBSERVATION_SCOPE_MISMATCH")
        reasons.append("이전 자료의 지역·성·연령 또는 비교월이 일치하지 않아 연속성을 판단하지 않습니다.")
    elif not prior_comparable or any(flag is None for flag in previous_flags):
        codes.append("PREVIOUS_OBSERVATION_UNAVAILABLE")
        reasons.append("직전 월의 비교 가능한 두 활동 원천이 없어 2개월 연속 여부는 미확인입니다.")
    if streak is True:
        codes.append("TWO_MONTH_5PCT_JOINT_DECLINE")
        reasons.append("이번 달과 직전 달 모두 통신·카드가 각각 5% 이상 감소해 2개월 연속 기준을 충족했습니다.")
    if robust is True:
        codes.append("MODEL_ROBUST_CANDIDATE")
        reasons.append("현재 모델에서 같은 지역의 다른 성·연령 집단 대비 상대적 이례성 후보가 확인되었습니다.")
    if quality_status == "hold":
        codes.append("SOURCE_QUALITY_HOLD")
        reasons.append("원자료 품질 보류 상태를 유지하며 이 분류는 연구용 검토에만 사용합니다.")
    evidence_ids = []
    for record in (current, prior if prior_matches else {}):
        record_id = record.get("record_id")
        if isinstance(record_id, str) and record_id and record_id not in evidence_ids:
            evidence_ids.append(record_id)
    return {
        "rule_version": SIGNAL_RULE_VERSION,
        "threshold_percent": THRESHOLD_PERCENT,
        "category": category,
        "category_label": CATEGORY_LABELS[category],
        "scope": key,
        "sources": sources,
        "signals": {"joint_decline": joint, "two_month_streak": streak, "robust_candidate": robust},
        "robust_evidence": robust_evidence,
        "reason_codes": codes,
        "reasons": reasons,
        "quality_status": quality_status,
        "quality_reasons": quality_reasons,
        "operational_alert_allowed": False,
        "evidence_ids": evidence_ids,
    }
