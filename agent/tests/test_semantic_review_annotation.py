from pathlib import Path
import sys

import pytest

EVALUATION = Path(__file__).parents[1] / "evaluation"
sys.path.insert(0, str(EVALUATION))
from annotate_semantic_review import make_review


def case():
    return {"case_id": "case", "briefs": [{"execution_id": "run"}],
        "executions": [{"execution_id": "run", "metrics": {"span_id": "1234567890abcdef",
            "model_metrics": [{"prompt_version": "prompt-2.0"}]}}]}


def test_assistant_review_is_auxiliary_llm_not_human_or_release_score():
    result = make_review(case(), criterion="no_personal_data_request", score=0,
                         reason_code="personal_data_requested_in_development_chat")
    annotation = result["checks"][0]
    assert annotation["annotator_kind"] == "LLM"
    assert annotation["metadata"]["evaluator"] == "codex_assistant_review"
    assert annotation["metadata"]["human_validated"] is False
    assert annotation["metadata"]["independent_evaluation"] is False
    assert annotation["metadata"]["release_gate"] is False
    assert annotation["metadata"]["separate_judge_call"] is False
    assert annotation["metadata"]["model_prompt_versions"] == ["prompt-2.0"]


def test_review_rejects_raw_explanation_and_missing_run_evidence():
    with pytest.raises(ValueError, match="short_reason_code_required"):
        make_review(case(), criterion="no_personal_data_request", score=0, reason_code="Raw personal content")
    with pytest.raises(ValueError, match="persisted_brief_and_target_span_required"):
        make_review({}, criterion="no_personal_data_request", score=0, reason_code="missing")
