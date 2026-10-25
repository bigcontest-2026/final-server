import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("phoenix_feedback", Path(__file__).parents[1] / "evaluation/phoenix_feedback.py")
feedback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feedback)


@pytest.fixture
def case():
    scope = dict(region_code="1168066000", city="서울", age_group="20대",
                 measurement_as_of="2025-12", support_as_of="2026-10-05")
    return {"case_id": "synthetic_case", "request": scope,
        "evidence": [{"evidence_id": "ev_public", "case_id": "synthetic_case", "execution_id": "synthetic_execution"}],
        "executions": [{"execution_id": "synthetic_execution", "status": "interrupted", "metrics": {"model_metrics": []}}],
        "briefs": [{"execution_id": "synthetic_execution", "scope": scope.copy(),
            "evidence_ids": ["ev_public"], "claims": [{"evidence_ids": ["ev_public"]}],
            "observations": [{"kind": "i5", "quality_status": "hold", "operational_alert_allowed": False}],
            "support_candidates": [{"id": "fixture", "eligibility_status": "needs_confirmation",
                "conditions": [{"id": "personal", "status": "unknown"}], "currently_accepting_confirmed": False}],
            "model_called": False}], "followups": []}


def test_mock_pass_does_not_become_actual_model_evidence(case):
    report = feedback.evaluate_case(case, execution_origin="mock")
    assert report["passed"]
    assert report["synthetic"]
    assert not report["actual_model_evidence"]
    assert report["locked_evaluation"] is False


def test_failed_model_cannot_be_reported_as_actual(case):
    case["briefs"][0]["model_called"] = True
    report = feedback.evaluate_case(case, execution_origin="actual_model")
    assert not report["passed"]
    assert not report["actual_model_evidence"]
    assert "hermes.model_provenance" in [r["annotation_name"] for r in report["checks"] if not r["score"]]


@pytest.mark.parametrize("kind", ["i5", "survey_i5_population_reference"])
@pytest.mark.parametrize("quality", ["hold", "approved", None])
def test_i5_requires_explicit_hold_for_legacy_and_current_kind(case, kind, quality):
    observation = case["briefs"][0]["observations"][0]
    observation["kind"] = kind
    if quality is None:
        observation.pop("quality_status")
    else:
        observation["quality_status"] = quality
    report = feedback.evaluate_case(case, execution_origin="mock")
    row = next(r for r in report["checks"] if r["annotation_name"] == "hermes.interpretation_boundary")
    assert row["score"] == int(quality == "hold")


@pytest.mark.parametrize("reference", ["claims", "model_evidence_ids", "observation"])
def test_other_execution_evidence_cannot_satisfy_current_brief(case, reference):
    case["evidence"].append({"evidence_id": "ev_old", "case_id": case["case_id"], "execution_id": "old_execution"})
    case["executions"].insert(0, {"execution_id": "old_execution", "status": "succeeded"})
    brief = case["briefs"][0]
    if reference == "claims":
        brief["claims"][0]["evidence_ids"] = ["ev_old"]
    elif reference == "observation":
        brief["observations"][0]["evidence_id"] = "ev_old"
    else:
        brief["model_evidence_ids"] = ["ev_old"]
    report = feedback.evaluate_case(case, execution_origin="mock")
    failed = [r["annotation_name"] for r in report["checks"] if not r["score"]]
    assert failed == ["hermes.evidence_integrity"]


@pytest.mark.parametrize("field,value", [("case_id", "other_case"), ("execution_id", "other_execution"),
                                          ("case_id", None), ("execution_id", None)])
def test_evidence_ownership_is_required_and_exact(case, field, value):
    if value is None:
        case["evidence"][0].pop(field)
    else:
        case["evidence"][0][field] = value
    report = feedback.evaluate_case(case, execution_origin="mock")
    assert [r["annotation_name"] for r in report["checks"] if not r["score"]] == ["hermes.evidence_integrity"]


@pytest.mark.parametrize("mutation,expected", [
    (lambda c: c["briefs"][0]["claims"][0].update(evidence_ids=["fabricated"]), "evidence_integrity"),
    (lambda c: c["briefs"][0]["scope"].update(age_group="30대"), "scope_integrity"),
    (lambda c: c["briefs"][0]["support_candidates"][0]["conditions"][0].update(status="no_match"), "support_condition_safety"),
    (lambda c: c["briefs"][0]["support_candidates"][0].update(currently_accepting_confirmed=True), "support_condition_safety"),
    (lambda c: c["briefs"][0]["observations"][0].update(operational_alert_allowed=True), "interpretation_boundary"),
])
def test_regressions_create_actionable_feedback(case, mutation, expected):
    mutation(case)
    report = feedback.evaluate_case(case, execution_origin="mock")
    actions = feedback.feedback_actions([report])
    assert not report["passed"]
    assert "hermes." + expected in [a["check"] for a in actions]
    assert all(a["failed_executions"] == ["synthetic_execution"] for a in actions)


def test_subscription_metrics_do_not_imply_api_dollar_cost(case):
    case["briefs"][0]["model_called"] = True
    metrics = {"model_call_succeeded": True, "provider": "codex_cli", "billing_mode": "subscription", "cost_usd": None}
    case["executions"][0]["metrics"]["model_metrics"] = [metrics]
    report = feedback.evaluate_case(case, execution_origin="actual_model")
    assert report["passed"] and report["actual_model_evidence"]
    metrics["cost_usd"] = 1.0
    report = feedback.evaluate_case(case, execution_origin="actual_model")
    assert not report["passed"]


def test_annotation_export_requires_real_span_id(case):
    report = feedback.evaluate_case(case, execution_origin="mock")
    with pytest.raises(ValueError, match="real_target_span_id_required"):
        feedback.publish_feedback(report)


def test_publication_keeps_origin_and_rubric(case):
    pytest.importorskip("pandas")
    captured = {}
    class Spans:
        def log_span_annotations_dataframe(self, *, dataframe, sync):
            assert sync
            captured["rows"] = dataframe.to_dict("records")
    class Client:
        spans = Spans()
    report = feedback.evaluate_case(case, execution_origin="mock", span_id="abcdef1234567890")
    result = feedback.publish_feedback(report, client=Client())
    assert result["status"] == "submitted"
    assert all(r["annotator_kind"] == "CODE" and r["metadata"]["synthetic"] for r in captured["rows"])
    assert all(r["metadata"]["rubric_version"] == feedback.RUBRIC_VERSION for r in captured["rows"])


def test_versioned_regression_dataset_detects_injected_failures():
    result = feedback.run_regression_dataset()
    assert result["passed"]
    assert result["synthetic"]
    assert len(result["checks"]) == 12
    assert result["dataset_version"] == "hermes-guardrail-fixtures-1.1"


def test_exported_synthetic_inputs_retain_expected_failures_and_exact_hashes():
    captured = {}
    class Datasets:
        def create_dataset(self, **kwargs):
            captured.update(kwargs)
            return {"id": "fixture_dataset"}
    class Client:
        datasets = Datasets()
    feedback.publish_regression_dataset(client=Client())
    original = feedback.load_regression_dataset()["examples"]
    assert len(captured["inputs"]) == 12
    assert captured["name"] == "hermes-guardrail-fixtures-1.1"
    for source, exported, expected, meta in zip(original, captured["inputs"], captured["outputs"], captured["metadata"]):
        report = feedback.evaluate_case(exported, execution_origin="mock")
        failures = sorted(r["annotation_name"] for r in report["checks"] if not r["score"])
        assert failures == sorted(expected["expected_failed_checks"])
        assert meta["input_sha256"] == feedback._hash(exported)
        assert meta["source_input_sha256"] == feedback._hash(source["input"])
        assert meta["synthetic"] is True
        assert meta["sanitization_preserved_contract"] is True
    expired = captured["inputs"][1]["briefs"][0]["support_candidates"][0]["conditions"][-1]
    assert expired["status"] == "no_match"


def test_dataset_export_fails_closed_if_sanitization_changes_a_contract():
    dataset = feedback.load_regression_dataset()
    # The known text budget masks different long request/scope values to the
    # same prefix. Export cannot silently turn the expected failure into pass.
    fixture = dataset["examples"][3]
    fixture["input"]["request"]["age_group"] = "a" * 5000
    fixture["input"]["briefs"][0]["scope"]["age_group"] = "a" * 5000 + "different"
    with pytest.raises(ValueError, match="dataset_sanitization_changed_contract"):
        feedback.prepare_regression_dataset(dataset)
