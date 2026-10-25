"""Save an explicitly supplied Codex review; this script does not call a judge.

The interactive development assistant supplies a decision and short reason
code after reviewing an answer. This is an auxiliary LLM annotation, never a
human review, an independent benchmark or an automatic release gate.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from phoenix_feedback import publish_feedback

CRITERIA = ("no_personal_data_request", "grounded_explanation", "scope_limitations_clear")
RUBRIC_VERSION = "hermes-auxiliary-semantic-review-1.0"


def make_review(case: dict, *, criterion: str, score: int, reason_code: str,
                execution_id: str | None = None) -> dict:
    if criterion not in CRITERIA or score not in (0, 1) or isinstance(score, bool):
        raise ValueError("invalid_semantic_review_criterion_or_score")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", reason_code):
        raise ValueError("short_reason_code_required")
    briefs = case.get("briefs") or []
    execution_id = execution_id or (briefs[-1].get("execution_id") if briefs else None)
    execution = next((e for e in case.get("executions", []) if e.get("execution_id") == execution_id), {})
    brief = next((b for b in briefs if b.get("execution_id") == execution_id), {})
    metrics = execution.get("metrics") or {}
    span_id = metrics.get("span_id")
    if not brief or not isinstance(span_id, str) or not re.fullmatch(r"[0-9a-fA-F]{16}", span_id):
        raise ValueError("persisted_brief_and_target_span_required")
    metadata = {"rubric_version": RUBRIC_VERSION, "evaluator": "codex_assistant_review",
        "human_validated": False, "independent_evaluation": False, "release_gate": False,
        "separate_judge_call": False, "case_id": case.get("case_id"), "execution_id": execution_id,
        "model_prompt_versions": sorted({m["prompt_version"] for m in metrics.get("model_metrics", []) if m.get("prompt_version")}),
        "reason_code": reason_code, "review_scope": "development_assistant_auxiliary_review"}
    row = {"span_id": span_id, "annotation_name": "hermes.semantic." + criterion,
        "annotator_kind": "LLM", "score": score, "label": "pass" if score else "fail",
        "explanation": reason_code, "metadata": metadata}
    return {"span_id": span_id, "checks": [row], "metadata": metadata,
            "limitations": ["Supplied by the development assistant; not independently validated.",
                "Do not aggregate this auxiliary opinion with deterministic CODE contract scores.",
                "No human reviewer, external outcome, or separate model-judge call is represented."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--execution-id")
    parser.add_argument("--criterion", choices=CRITERIA, required=True)
    parser.add_argument("--score", type=int, choices=(0, 1), required=True)
    parser.add_argument("--reason-code", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:6006")
    args = parser.parse_args()
    report = make_review(json.loads(args.case.read_text()), criterion=args.criterion, score=args.score,
                         reason_code=args.reason_code, execution_id=args.execution_id)
    if args.publish:
        report["submission"] = publish_feedback(report, base_url=args.base_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"span_id": report["span_id"], "annotator_kind": "LLM", "criterion": args.criterion,
                      "score": args.score, "human_validated": False, "release_gate": False}))


if __name__ == "__main__":
    main()
