"""Deterministic, versioned feedback for saved investigation runs.

This checks machine-verifiable contracts. It does not grade prose quality,
clinical validity, institutional readiness or actual service outcomes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from donghaeng_agent.observability import local_endpoint, sanitize

RUBRIC_VERSION = "hermes-contract-evaluation-1.1"
ORIGINS = ("actual_model", "rule_baseline", "mock", "official_capture_replay")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _refs(value):
    if isinstance(value, dict):
        for key in ("evidence_ids", "model_evidence_ids"):
            if isinstance(value.get(key), list):
                yield from value[key]
        if "evidence_id" in value:
            yield value["evidence_id"]
        for item in value.values():
            yield from _refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _refs(item)


def evaluate_case(case: dict, *, execution_origin: str, execution_id: str | None = None,
                  span_id: str | None = None) -> dict:
    """Evaluate a saved repository.get() response, keeping synthetic origin explicit."""
    if execution_origin not in ORIGINS:
        raise ValueError("execution_origin_required")
    if span_id is not None and not re.fullmatch(r"[a-fA-F0-9]{16}", span_id):
        raise ValueError("span_id_must_be_16_hex_characters")
    briefs = case.get("briefs") or []
    if execution_id is None:
        execution_id = briefs[-1].get("execution_id") if briefs else None
    brief = next((b for b in reversed(briefs) if b.get("execution_id") == execution_id), {})
    execution = next((e for e in case.get("executions", []) if e.get("execution_id") == execution_id), {})
    # Repository.get() includes authoritative SQL ownership fields. A case can
    # have several executions, so case-wide ID membership is insufficient.
    # Missing ownership in older exports must fail closed; re-export the saved
    # case from the repository instead of inferring ownership from ID strings.
    evidence_ids = {e.get("evidence_id") for e in case.get("evidence", [])
                    if e.get("evidence_id") and execution_id is not None and
                    e.get("execution_id") == execution_id and
                    case.get("case_id") is not None and e.get("case_id") == case["case_id"]}
    refs = list(_refs(brief))
    candidates = brief.get("support_candidates") or []
    model_metrics = (execution.get("metrics") or {}).get("model_metrics") or []
    successful = [m for m in model_metrics if m.get("model_call_succeeded") or m.get("actual_api_success")]
    metadata = {"rubric_version": RUBRIC_VERSION, "execution_origin": execution_origin,
                "case_id": case.get("case_id"), "execution_id": execution_id,
                "input_sha256": _hash(case), "locked_evaluation": False,
                "synthetic": execution_origin == "mock"}
    checks = []

    def check(name, passed, explanation, remediation):
        checks.append({"span_id": span_id, "annotation_name": "hermes." + name,
            "annotator_kind": "CODE", "score": int(bool(passed)),
            "label": "pass" if passed else "fail", "explanation": explanation if passed else remediation,
            "metadata": metadata})

    check("execution_completed", bool(brief) and execution.get("status") in ("interrupted", "succeeded"),
          "A persisted brief reached review or completed review.",
          "Inspect the failed/missing execution before claiming an end-to-end success.")
    check("evidence_integrity", bool(brief) and bool(refs) and all(isinstance(r, str) and r in evidence_ids for r in refs)
          and all(c.get("evidence_ids") for c in brief.get("claims", [])),
          "All declared references resolve to evidence owned by the selected case and execution.",
          "Reject missing, foreign-execution, or unowned evidence; re-export older cases with SQL ownership fields.")
    request, scope = case.get("request", {}), brief.get("scope", {})
    scope_fields = ("region_code", "city", "age_group", "measurement_as_of", "support_as_of")
    check("scope_integrity", bool(brief) and all(k in request and request[k] == scope.get(k) for k in scope_fields),
          "The report preserves the requested population, location and separate observation/support dates.",
          "Preserve the exact request scope and decline unsupported disaggregation or time substitution.")
    unsafe = [c for c in candidates if c.get("eligibility_status") in ("eligible", "confirmed", "excluded")
              or any(x.get("status") == "no_match" for x in c.get("conditions", []))
              or (any(x.get("status") == "unknown" for x in c.get("conditions", []))
                  and c.get("currently_accepting_confirmed") is True)]
    check("support_condition_safety", bool(brief) and not unsafe,
          "No included candidate contradicts a known condition or converts unknown eligibility to confirmation.",
          "Exclude expired/out-of-scope candidates and preserve unknown personal eligibility and acceptance.")
    observations = brief.get("observations", [])
    check("interpretation_boundary", bool(brief) and all(
        o.get("operational_alert_allowed") is not True and
        (o.get("kind") not in ("i5", "survey_i5_population_reference") or
         o.get("quality_status") == "hold") for o in observations),
        "Structured observations do not authorize an operational alert; I5 remains on hold.",
        "Restore hold and operational_alert_allowed=false; these figures are not individual risk diagnoses.")
    model_claimed = brief.get("model_called") is True
    origin_consistent = not (execution_origin == "rule_baseline" and (model_claimed or successful))
    real_success = model_claimed and bool(successful) and execution_origin in ("actual_model", "official_capture_replay")
    check("model_provenance", bool(brief) and origin_consistent and
          (bool(successful) if model_claimed else not successful) and
          (real_success if execution_origin == "actual_model" else True),
          "The model-call claim agrees with successful saved call metrics and the declared execution origin.",
          "Record successful provider metrics; never promote a mock, baseline or failed call to actual-model evidence.")
    subscription = [m for m in model_metrics if m.get("billing_mode") in
                    ("subscription", "subscription_quota", "chatgpt_subscription")]
    check("subscription_billing", all(m.get("cost_usd") is None for m in subscription),
          "Subscription calls retain unknown/non-API dollar cost as null.",
          "Do not convert subscription tokens into API charges; set cost_usd=null and record billing mode.")
    check("followup_boundary", all(f.get("status") not in ("connection_confirmed", "participation_confirmed")
          or bool(f.get("evidence_ids")) for f in case.get("followups", [])),
          "No confirmed service outcome lacks declared follow-up evidence.",
          "Require outcome evidence; an accepted review or referral is not completed support.")
    return {**metadata, "span_id": span_id, "checks": sanitize(checks),
            "checks_passed": sum(x["score"] for x in checks), "checks_total": len(checks),
            "passed": all(x["score"] for x in checks), "actual_model_evidence": real_success,
            "limitations": ["Deterministic contracts only; prose quality and unsupported natural-language claims need human review.",
                "Development evaluation, not a locked benchmark or enterprise acceptance test.",
                "Trace availability and real-world service outcomes require separate verification."]}


def publish_feedback(report: dict, *, base_url: str = "http://127.0.0.1:6006", client=None) -> dict:
    """Attach CODE scores to a real span; imports the optional client only on publish."""
    local_endpoint(base_url)
    span_id = report.get("span_id")
    if not isinstance(span_id, str) or not re.fullmatch(r"[a-fA-F0-9]{16}", span_id):
        raise ValueError("real_target_span_id_required")
    import pandas as pd
    if client is None:
        client = _local_client(base_url)
    rows = sanitize(report["checks"])
    client.spans.log_span_annotations_dataframe(dataframe=pd.DataFrame(rows), sync=True)
    return {"status": "submitted", "span_id": span_id, "annotations": len(rows),
            "note": "Submission is not read-back verification; query Phoenix before claiming persistence."}


def _local_client(base_url):
    local_endpoint(base_url)
    os.environ.setdefault("PHOENIX_WORKING_DIR", str(Path(__file__).resolve().parents[1] / "runtime/phoenix"))
    os.environ.setdefault("PHOENIX_ALLOW_EXTERNAL_RESOURCES", "false")
    from phoenix.client import Client
    import httpx
    return Client(base_url=base_url, http_client=httpx.Client(base_url=base_url,
                  trust_env=False, follow_redirects=False, timeout=10.0))


def load_regression_dataset() -> dict:
    return json.loads((Path(__file__).parent / "phoenix_regression_cases_v1_1.json").read_text())


def run_regression_dataset() -> dict:
    dataset = load_regression_dataset()
    checks = []
    for example in dataset["examples"]:
        report = evaluate_case(example["input"], execution_origin="mock")
        failed = sorted(c["annotation_name"] for c in report["checks"] if c["score"] == 0)
        checks.append({"id": example["id"], "passed": failed == sorted(example["expected_failed_checks"]),
                       "expected_failed_checks": example["expected_failed_checks"], "actual_failed_checks": failed})
    return {"dataset_version": dataset["dataset_version"], "synthetic": True,
            "passed": all(c["passed"] for c in checks), "checks": checks}


def publish_regression_dataset(*, base_url="http://127.0.0.1:6006", client=None):
    """Upload immutable synthetic fixtures, clearly distinct from live model scores."""
    local_endpoint(base_url)
    dataset, examples = prepare_regression_dataset()
    if client is None:
        client = _local_client(base_url)
    return client.datasets.create_dataset(
        name=dataset["dataset_version"],
        inputs=[e["input"] for e in examples],
        outputs=[e["output"] for e in examples],
        metadata=[e["metadata"] for e in examples],
        dataset_description=dataset["description"],
    )


def prepare_regression_dataset(dataset=None):
    """Validate the exact sanitized export inputs before any network operation."""
    dataset = dataset or load_regression_dataset()
    examples = []
    for example in dataset["examples"]:
        # Full fixture contracts need deeper nesting than a short span summary.
        # Redaction, item/character budgets still apply, and a changed assertion
        # must fail closed instead of uploading a subtly different test.
        exported_input = sanitize(example["input"], max_depth=12)
        report = evaluate_case(exported_input, execution_origin="mock")
        observed = sorted(r["annotation_name"] for r in report["checks"] if r["score"] == 0)
        if observed != sorted(example["expected_failed_checks"]):
            raise ValueError("dataset_sanitization_changed_contract:" + example["id"])
        examples.append({"input": exported_input,
            "output": {"expected_failed_checks": example["expected_failed_checks"]},
            "metadata": {"fixture_id": example["id"], "execution_origin": "mock", "synthetic": True,
                "dataset_version": dataset["dataset_version"], "rubric_version": RUBRIC_VERSION,
                "input_sha256": _hash(exported_input), "source_input_sha256": _hash(example["input"]),
                "sanitization_preserved_contract": True}})
    return dataset, examples


def feedback_actions(reports: list[dict]) -> list[dict]:
    """Rank reproducible contract failures for the next implementation iteration."""
    grouped = {}
    for report in reports:
        for row in report.get("checks", []):
            if row.get("score") == 0:
                entry = grouped.setdefault(row["annotation_name"], {"check": row["annotation_name"],
                    "failed_executions": [], "remediation": row["explanation"]})
                entry["failed_executions"].append(report.get("execution_id"))
    return sorted(grouped.values(), key=lambda x: (-len(x["failed_executions"]), x["check"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--case", type=Path)
    source.add_argument("--regression-dataset", action="store_true")
    parser.add_argument("--origin", choices=ORIGINS)
    parser.add_argument("--execution-id")
    parser.add_argument("--span-id")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:6006")
    args = parser.parse_args()
    if args.regression_dataset:
        dataset, examples = prepare_regression_dataset()
        result = run_regression_dataset()
        result.update(export_examples=len(examples), exported_inputs_verified=True,
                      example_hashes=[e["metadata"]["input_sha256"] for e in examples])
        if args.publish:
            published = publish_regression_dataset(base_url=args.base_url)
            result["phoenix_dataset"] = {"id": published.id, "version_id": published.version_id,
                                          "name": dataset["dataset_version"], "status": "submitted"}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({k: result[k] for k in ("dataset_version", "synthetic", "passed", "export_examples", "exported_inputs_verified")}, ensure_ascii=False))
        return 0 if result["passed"] else 1
    if args.origin is None:
        parser.error("--origin is required with --case")
    case = json.loads(args.case.read_text())
    report = evaluate_case(case, execution_origin=args.origin, execution_id=args.execution_id, span_id=args.span_id)
    if args.publish:
        report["phoenix_submission"] = publish_feedback(report, base_url=args.base_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("passed", "checks_passed", "checks_total", "actual_model_evidence")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
