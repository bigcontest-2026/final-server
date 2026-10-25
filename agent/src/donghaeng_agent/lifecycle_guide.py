"""Versioned literature-based questions; never a risk or eligibility classifier.

The reviewed report supplies concepts and age bands. Age-specific questions below
are service interpretation hypotheses, not observed regional facts or the paper's
age-to-cause assignments. No local PDF path or full source text is transmitted.
"""
from copy import deepcopy
import hashlib
import json

from .signal_rules import previous_month


LIFECYCLE_GUIDE_VERSION = "lifecycle-guide-kihasa-2025-07-v1"
SOURCE = {
    "source_id": "literature:kihasa:2025-07",
    "title": "생애주기별 사회적 고립 및 외로움 실태와 정책 과제",
    "authors": ["김성아", "강예은"], "publisher": "한국보건사회연구원",
    "year": 2025, "publication_month": "2025-12", "report_number": "연구보고서(수시) 2025-07",
    "doi": "https://doi.org/10.23060/kihasa.b.2025.07",
    "pages": [19, 20, 21, 22, 23, 24, 30, 39, 40, 52, 57, 58, 59, 60, 61, 62],
    "page_numbering": "printed", "pdf_page_offset": 8,
    "content_sha256": "54e127dd559bbe08562ea0c528f0cceb12e5b77361ca5b9936806f38037d2e5c",
    "evidence_role": "interpretive_literature", "regional_confirmation": False,
    "review_basis": "reviewed_source_pdf",
}
AGE_GUIDES = {
    "20G": {"label": "20대", "source_range": "19~29세", "relation": "subset_excluding_age_19",
        "hypothesis": "교육 이수·경제적 독립·새로운 친밀 관계와 사회 역할의 변화가 활동 변화와 함께 있었을 가능성을 확인합니다.",
        "questions": ["같은 지역·기간에 교육·고용·독립과 관련된 변화가 있었는지 확인할 공개 집계 근거가 있습니까?",
                      "관계 접점이나 도움받을 관계의 변화에 관한 별도 지역 자료가 있으며 관측 활동과 같은 집단을 다룹니까?"],
        "pages": [19, 20, 21, 24, 30, 57],
        "age_boundary": "문헌의 첫 실증 구간은 19~29세이고 현재 활동 구간은 20~29세입니다. 19세를 포함한 동일 집단으로 간주하지 않습니다."},
    "30G": {"label": "30대", "source_range": "30~39세", "relation": "same_age_band_different_population",
        "hypothesis": "고용·주거·가족 역할 변화가 생활권이나 관계망 변화와 함께 있었는지 확인할 수 있습니다.",
        "questions": ["고용·주거·가구구성 변화와 관측 기간을 연결할 지역 자료가 있습니까?",
                      "관계 교류와 도움받을 지지체계 중 무엇이 실제로 관측되었고 무엇이 미확인입니까?"],
        "pages": [20, 21, 22, 23, 24, 30, 57, 58],
        "age_boundary": "연령대가 같아도 문헌의 설문 응답자와 현재 장소 유동·가맹점 거래 집계는 다른 모집단입니다. 주거·가족 역할은 서비스용 확인 가설입니다."},
    "40G": {"label": "40대", "source_range": "40~49세", "relation": "same_age_band_different_population",
        "hypothesis": "고용·부양·돌봄 역할이나 교류·지원 관계의 변화가 있었는지 확인할 수 있습니다.",
        "questions": ["고용·부양·돌봄 역할의 변화가 있었다는 같은 지역·기간의 근거가 있습니까?",
                      "활동 감소와 관계 교류·지지체계의 변화를 구별해서 확인할 수 있습니까?"],
        "pages": [20, 21, 22, 23, 24, 30, 52, 57, 58],
        "age_boundary": "40대라는 이유만으로 부양·돌봄 부담을 단정하지 않습니다. 이는 문헌의 연령별 확정 원인표가 아닌 서비스용 확인 가설입니다."},
    "50G": {"label": "50대", "source_range": "50~59세", "relation": "same_age_band_different_population",
        "hypothesis": "일·가족 역할 변화와 도움받을 관계의 공백이 있었는지 확인할 수 있습니다.",
        "questions": ["고용 형태·은퇴 전후 역할·가족 역할의 변화를 뒷받침할 집계 근거가 있습니까?",
                      "관련 지원이 검색되지 않은 이유가 미수록·조건 차이·지역 제공 여부 미확인 중 무엇입니까?"],
        "pages": [19, 20, 21, 22, 24, 30, 52, 57, 58, 61],
        "age_boundary": "50대 전체를 퇴직자나 지원 공백 집단으로 간주하지 않습니다. 퇴직·역할 변화는 서비스용 확인 가설입니다."},
    "60GU": {"label": "60세 이상", "source_range": "60세 이상", "relation": "same_age_band_different_population",
        "hypothesis": "은퇴 전후 역할 변화·주변인 상실·건강·돌봄 맥락 중 확인 가능한 요인이 있는지 검토합니다.",
        "questions": ["은퇴·관계 상실·건강·돌봄 맥락을 확인할 비식별 지역 자료가 있으며 관측 기간과 맞습니까?",
                      "사업의 실제 연령 조건이 60세 이상인지 65세 이상인지 원문에서 확인했습니까?"],
        "pages": [19, 20, 21, 22, 23, 24, 30, 39, 40, 52, 57, 58],
        "age_boundary": "60세 이상에는 60~64세가 포함됩니다. 65세 이상 사업 대상에 자동 대응하거나 65세 이상 인원·비율을 추정하지 않습니다."},
}
COMPARISON_CHECKS = [
    "다른 성·연령 집단에서도 같은 변화가 관측되는지 확인하되 자료가 없으면 비교 완료로 쓰지 않습니다.",
    "달력·휴일·행사·기상 등 같은 지역·관측 기간의 대안 설명을 확인합니다.",
    "통신 경계·관측 범위와 카드 집계·누락 규칙의 변경을 생애사건 가설과 별도로 검토합니다.",
    "가설을 지지하는 자료와 반박하거나 약화하는 자료를 함께 확인하고 시간·지역·집단 불일치를 남깁니다.",
]
BOUNDARIES = [
    "활동 변화는 사회적 교류 부재·지지체계 부재·주관적 외로움의 직접 측정값이 아닙니다.",
    "혼자 사는 상태·사회적 고립·외로움은 서로 일치하지 않습니다. 고립·외로움·지원 필요를 확정하지 않습니다.",
    "생애주기 질문으로 기존 10개 활동 집단이나 5% 범주를 바꾸지 않으며 연령별 추가 점수·복지 분야 순위를 만들지 않습니다.",
    "문헌의 정책 비교는 당시 검토 범위입니다. 현재 지역의 사업 공백이나 개인 자격을 입증하지 않습니다.",
]
GUIDE_SHA256 = hashlib.sha256(json.dumps({"version": LIFECYCLE_GUIDE_VERSION, "source": SOURCE,
    "ages": AGE_GUIDES, "comparison_checks": COMPARISON_CHECKS, "boundaries": BOUNDARIES},
    ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _scope(scope):
    if not isinstance(scope, dict):
        raise ValueError("lifecycle_scope_invalid")
    key = {name: scope.get(name) for name in ("region_code", "month", "sex_code", "age_code")}
    region = key["region_code"]
    if (not isinstance(region, str) or len(region) != 5 or not region.isascii() or not region.isdigit()
            or previous_month(key["month"]) is None or key["sex_code"] not in (None, "MAN", "WMAN")
            or key["age_code"] not in (None, *AGE_GUIDES)):
        raise ValueError("lifecycle_scope_invalid")
    return key


def _group_matches(parent, child):
    return (child["region_code"] == parent["region_code"] and child["month"] == parent["month"]
            and child["sex_code"] in ("MAN", "WMAN") and child["age_code"] in AGE_GUIDES
            and all(parent[name] is None or parent[name] == child[name] for name in ("sex_code", "age_code")))


def validate_lifecycle_scope(interpretation, scope):
    """A frozen guide may be older, but cannot describe another observation scope."""
    parent = _scope(scope)
    if not isinstance(interpretation, dict) or interpretation.get("scope") != parent:
        raise ValueError("lifecycle_scope_mismatch")
    if (not isinstance(interpretation.get("guide_version"), str) or not interpretation["guide_version"]
            or not isinstance(interpretation.get("guide_sha256"), str)
            or len(interpretation["guide_sha256"]) != 64
            or not isinstance(interpretation.get("groups"), list)):
        raise ValueError("lifecycle_guide_invalid")
    seen = set()
    for group in interpretation["groups"]:
        if not isinstance(group, dict):
            raise ValueError("lifecycle_guide_invalid")
        child = _scope(group.get("scope"))
        identity = child["sex_code"], child["age_code"]
        if not _group_matches(parent, child) or identity in seen:
            raise ValueError("lifecycle_scope_mismatch")
        if group.get("interpretation_status") != "questions_only" or group.get("support_need_status") != "unconfirmed":
            raise ValueError("lifecycle_status_invalid")
        hypotheses = group.get("hypotheses")
        if (not isinstance(hypotheses, list) or not hypotheses
                or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
                       or item.get("status") != "unverified" for item in hypotheses)
                or len({item["id"] for item in hypotheses}) != len(hypotheses)):
            raise ValueError("lifecycle_guide_invalid")
        seen.add(identity)
    expected = {(sex, age) for sex in ([parent["sex_code"]] if parent["sex_code"] else ("MAN", "WMAN"))
                for age in ([parent["age_code"]] if parent["age_code"] else AGE_GUIDES)}
    if seen != expected:
        raise ValueError("lifecycle_scope_mismatch")
    if (interpretation.get("interpretation_status") != "questions_only"
            or interpretation.get("support_need_status") != "unconfirmed"
            or interpretation.get("changes_activity_classification") is not False
            or interpretation.get("automatic_welfare_mapping") is not False):
        raise ValueError("lifecycle_status_invalid")


def build_lifecycle_interpretation(scope, assessments):
    parent, indexed = _scope(scope), {}
    for item in assessments:
        child = _scope(item.get("scope"))
        identity = child["sex_code"], child["age_code"]
        if not _group_matches(parent, child) or identity in indexed:
            raise ValueError("lifecycle_scope_mismatch")
        indexed[identity] = item
    groups = []
    for sex in ([parent["sex_code"]] if parent["sex_code"] else ("MAN", "WMAN")):
        for age in ([parent["age_code"]] if parent["age_code"] else AGE_GUIDES):
            guide, observed = AGE_GUIDES[age], indexed.get((sex, age)) or {}
            observation_status = "available" if observed.get("observation") else "unavailable"
            groups.append({"scope": {**parent, "sex_code": sex, "age_code": age}, "age_label": guide["label"],
                "source_age_range": guide["source_range"], "age_scope_relation": guide["relation"],
                "observation_status": observation_status,
                "hypotheses": [{"id": f"lifecycle:{age}:context", "text": guide["hypothesis"],
                    "basis": "service_interpretation", "status": "unverified", "source_pages": list(guide["pages"])}],
                "questions": list(guide["questions"]), "comparison_checks": list(COMPARISON_CHECKS),
                "interpretation_status": "questions_only", "support_need_status": "unconfirmed",
                "boundaries": [guide["age_boundary"], *BOUNDARIES],
                "observation_evidence_ids": list(observed.get("evidence_ids", []))})
    result = {"guide_version": LIFECYCLE_GUIDE_VERSION, "guide_sha256": GUIDE_SHA256,
              "source": deepcopy(SOURCE), "scope": parent, "groups": groups,
              "comparison_checks": list(COMPARISON_CHECKS), "boundaries": list(BOUNDARIES),
              "interpretation_status": "questions_only", "support_need_status": "unconfirmed",
              "changes_activity_classification": False, "automatic_welfare_mapping": False}
    validate_lifecycle_scope(result, scope)
    return result


def guide_provenance(interpretation):
    return {"run_id": interpretation["guide_version"], "manifest_sha256": interpretation["guide_sha256"],
            "source_content_sha256": interpretation["source"]["content_sha256"],
            "role": "interpretive_literature_not_regional_confirmation"}
