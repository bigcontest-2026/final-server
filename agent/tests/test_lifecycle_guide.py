"""Literature questions stay separate from observations, scores and eligibility."""
from copy import deepcopy
import json

import pytest

from donghaeng_agent.analysis_report_store import AnalysisReportStore
from donghaeng_agent.analysis_reports import build_analysis_report, render_analysis_report, attach_context_investigation
from donghaeng_agent.lifecycle_guide import (
    AGE_GUIDES, SOURCE, GUIDE_SHA256, LIFECYCLE_GUIDE_VERSION,
    build_lifecycle_interpretation, validate_lifecycle_scope,
)


SCOPE = {"region_code": "51110", "month": "202510", "sex_code": None, "age_code": None}


def base_report(scope=None):
    return build_analysis_report(scope=scope or SCOPE, current_rows=[], previous_rows=[],
        i5=None, mois=None, provenance={"activity": {"run_id": "test", "manifest_sha256": "a" * 64}})


@pytest.mark.parametrize("age", list(AGE_GUIDES))
def test_five_age_bands_supply_only_unconfirmed_questions(age):
    scope = {**SCOPE, "sex_code": "MAN", "age_code": age}
    observation = {"scope": scope, "observation": {"region_name": "춘천시"}, "evidence_ids": ["evidence:test"]}
    before = deepcopy(observation)
    guide = build_lifecycle_interpretation(scope, [observation])
    assert observation == before
    assert len(guide["groups"]) == 1
    group = guide["groups"][0]
    assert group["scope"] == scope and group["age_label"] == AGE_GUIDES[age]["label"]
    assert group["observation_status"] == "available"
    assert group["interpretation_status"] == "questions_only"
    assert group["support_need_status"] == "unconfirmed"
    assert group["hypotheses"][0]["basis"] == "service_interpretation"
    assert group["hypotheses"][0]["status"] == "unverified"
    assert group["questions"] and group["comparison_checks"] and group["boundaries"]
    assert group["observation_evidence_ids"] == ["evidence:test"]
    assert guide["changes_activity_classification"] is False
    assert guide["automatic_welfare_mapping"] is False
    assert not {"risk_score", "domain_rank", "recommendations", "eligibility"} & group.keys()


def test_missing_observations_keep_ten_questions_without_observed_claims():
    guide = build_lifecycle_interpretation(SCOPE, [])
    assert len(guide["groups"]) == 10
    assert {(g["scope"]["sex_code"], g["scope"]["age_code"]) for g in guide["groups"]} == {
        (sex, age) for sex in ("MAN", "WMAN") for age in AGE_GUIDES}
    assert all(g["observation_status"] == "unavailable" and not g["observation_evidence_ids"] for g in guide["groups"])
    assert guide["source"]["regional_confirmation"] is False
    assert guide["source"]["evidence_role"] == "interpretive_literature"


def test_original_age_ranges_and_reviewed_source_are_explicit():
    guide = build_lifecycle_interpretation(SCOPE, [])
    young, older = guide["groups"][0], guide["groups"][4]
    assert young["source_age_range"] == "19~29세"
    assert young["age_scope_relation"] == "subset_excluding_age_19"
    assert "19세" in young["boundaries"][0]
    assert "60~64세" in older["boundaries"][0] and "65세 이상" in older["boundaries"][0]
    assert guide["source"]["authors"] == ["김성아", "강예은"]
    assert guide["source"]["doi"] == "https://doi.org/10.23060/kihasa.b.2025.07"
    assert {19, 24, 30, 39, 40, 52, 57, 62} <= set(guide["source"]["pages"])
    assert guide["source"]["content_sha256"] == "54e127dd559bbe08562ea0c528f0cceb12e5b77361ca5b9936806f38037d2e5c"
    serialized = json.dumps(guide, ensure_ascii=False)
    assert "/Users/" not in serialized and "local_path" not in serialized and "full_text" not in serialized
    assert guide["guide_version"] == LIFECYCLE_GUIDE_VERSION and guide["guide_sha256"] == GUIDE_SHA256
    guide["source"]["title"] = "changed"
    assert SOURCE["title"] == "생애주기별 사회적 고립 및 외로움 실태와 정책 과제"


@pytest.mark.parametrize("changes", [{"region_code": "11680"}, {"month": "202509"}, {"sex_code": "WMAN"}, {"age_code": "30G"}])
def test_assessment_cannot_cross_report_scope(changes):
    scope = {**SCOPE, "sex_code": "MAN", "age_code": "20G"}
    with pytest.raises(ValueError, match="lifecycle_scope_mismatch"):
        build_lifecycle_interpretation(scope, [{"scope": {**scope, **changes}}])


@pytest.mark.parametrize("mutate", [
    lambda guide: guide["scope"].update(month="202509"),
    lambda guide: guide["groups"][0]["scope"].update(region_code="11680"),
    lambda guide: guide["groups"].pop(),
    lambda guide: guide["groups"].append(deepcopy(guide["groups"][0])),
    lambda guide: guide["groups"][0].update(support_need_status="confirmed"),
    lambda guide: guide.update(changes_activity_classification=True),
])
def test_frozen_scope_and_unconfirmed_status_are_validated(mutate):
    guide = build_lifecycle_interpretation(SCOPE, [])
    mutate(guide)
    with pytest.raises(ValueError):
        validate_lifecycle_scope(guide, SCOPE)


def test_report_markdown_distinguishes_literature_and_missing_observations():
    report = base_report()
    assert report["summary"]["total"] == report["summary"]["assessment_hold"] == 10
    assert report["threshold_percent"] == 5
    assert report["recommendation_status"] == "not_requested"
    assert report["provenance"]["lifecycle_guide"]["role"] == "interpretive_literature_not_regional_confirmation"
    for text in ("생애주기별 살펴볼 사항", "19~29세", "60~64세", "달력", "집계", "DOI", "39, 40, 52"):
        assert text in report["markdown"]
    assert "관측 자료: 없음" in report["markdown"]
    assert report["lifecycle_interpretation"]["support_need_status"] == "unconfirmed"
    assert "지원 필요: 미확인" not in report["markdown"]


def test_new_guide_and_schema_preserve_old_frozen_reports_across_restart(tmp_path):
    path = tmp_path / "analysis.sqlite3"
    report = base_report()
    old = deepcopy(report)
    old["schema_version"] = "analysis-report-1.0"
    old.pop("lifecycle_interpretation")
    old["provenance"].pop("lifecycle_guide")
    old["markdown"] = render_analysis_report(old)
    store = AnalysisReportStore(path)
    saved_old, saved_new = store.save(old), store.save(report)
    assert saved_old["report_id"] != saved_new["report_id"]
    assert store.save(report)["report_id"] == saved_new["report_id"]
    changed = deepcopy(report)
    changed["lifecycle_interpretation"]["guide_version"] += "-new"
    changed["lifecycle_interpretation"]["guide_sha256"] = "b" * 64
    saved_changed = store.save(changed)
    assert saved_changed["report_id"] != saved_new["report_id"]
    restarted = AnalysisReportStore(path)
    assert restarted.get(saved_old["report_id"]) == {k: v for k, v in saved_old.items() if k != "reused"}
    assert restarted.get(saved_new["report_id"])["lifecycle_interpretation"] == report["lifecycle_interpretation"]
    assert "lifecycle_interpretation" not in restarted.get(saved_old["report_id"])


def test_investigation_preserves_guide_and_cannot_confirm_need(tmp_path):
    saved = AnalysisReportStore(tmp_path / "analysis.sqlite3").save(base_report())
    before = deepcopy(saved)
    result = attach_context_investigation(saved, {"status": "no_evidence", "summary": "원문 근거 미확보", "sources": [],
        "verification_questions": ["별도 지지체계 자료가 있는가?"], "limitations": ["원인 미확인"]})
    assert saved == before and result["lifecycle_interpretation"] == saved["lifecycle_interpretation"]
    assert result["lifecycle_interpretation"]["support_need_status"] == "unconfirmed"
    assert result["assessments"] == saved["assessments"]
