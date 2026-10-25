"""Read persisted local Phoenix evidence without modifying its database.

This fallback is deliberately SQLite/development-only. It does not replace
Phoenix RBAC/API access for deployed environments. It only emits summaries for
the explicitly selected project, never full prompts or CLI output.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def read_persisted_feedback(database: Path, project_name: str) -> dict:
    con = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("""SELECT s.id, s.span_id, s.parent_id, s.name, s.span_kind,
            s.status_code, s.status_message, s.llm_token_count_prompt, s.llm_token_count_completion,
            json(s.attributes) AS attributes, json(s.events) AS events, t.trace_id
            FROM spans s JOIN traces t ON s.trace_rowid=t.id
            JOIN projects p ON t.project_rowid=p.id WHERE p.name=? ORDER BY s.id""", (project_name,)).fetchall()
        annotations = con.execute("""SELECT s.span_id,t.trace_id,a.name,a.score,a.label,a.annotator_kind,
            json(a.metadata) AS metadata FROM span_annotations a JOIN spans s ON a.span_rowid=s.id
            JOIN traces t ON s.trace_rowid=t.id JOIN projects p ON t.project_rowid=p.id
            WHERE p.name=? ORDER BY a.id""", (project_name,)).fetchall()
        costs = con.execute("""SELECT s.span_id,c.total_cost FROM span_costs c
            JOIN spans s ON c.span_rowid=s.id JOIN traces t ON s.trace_rowid=t.id
            JOIN projects p ON t.project_rowid=p.id WHERE p.name=?""", (project_name,)).fetchall()
    finally:
        con.close()
    groups = {}
    for row in rows:
        groups.setdefault(row["trace_id"], []).append(dict(row))
    traces = []
    for trace_id, spans in groups.items():
        ids = {s["span_id"] for s in spans}
        roots = [s for s in spans if not s["parent_id"]]
        missing = [s["span_id"] for s in spans if s["parent_id"] and s["parent_id"] not in ids]
        model = []
        for span in spans:
            attrs = json.loads(span["attributes"] or "{}")
            metadata = attrs.get("metadata") or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            if span["span_kind"] == "LLM":
                usage = metadata.get("usage")
                output = attrs.get("output", {}).get("value")
                if isinstance(output, str):
                    try:
                        output = json.loads(output)
                    except ValueError:
                        output = None
                model.append({"span_id": span["span_id"], "status": span["status_code"],
                    "provider": metadata.get("provider"), "auth_mode": metadata.get("auth_mode"),
                    "billing_mode": metadata.get("billing_mode"), "cost_usd": metadata.get("cost_usd"),
                    "usage": usage, "token_usage_status": metadata.get("token_usage_status", "reported" if usage else "not_reported"),
                    "phoenix_token_columns": {"input_tokens": span["llm_token_count_prompt"],
                                              "output_tokens": span["llm_token_count_completion"]},
                    "structured_output_persisted": isinstance(output, dict),
                    "output_contract_fields": sorted(k for k in ("text", "tool_names", "evidence_ids")
                        if isinstance(output, dict) and k in output),
                    "output_text_characters": len(output.get("text", "")) if isinstance(output, dict) else None,
                    "output_tool_count": len(output.get("tool_names", [])) if isinstance(output, dict) else None,
                    "output_evidence_count": len(output.get("evidence_ids", [])) if isinstance(output, dict) else None,
                    "model_call_succeeded": metadata.get("model_call_succeeded") is True})
        trace_annotations = [{k: a[k] for k in ("span_id", "name", "score", "label", "annotator_kind")}
                             for a in annotations if a["trace_id"] == trace_id]
        semantic_reviews = []
        for annotation in annotations:
            if annotation["trace_id"] != trace_id or annotation["annotator_kind"] != "LLM":
                continue
            review_metadata = json.loads(annotation["metadata"] or "{}")
            semantic_reviews.append({"name": annotation["name"], "score": annotation["score"],
                **{k: review_metadata.get(k) for k in ("evaluator", "human_validated", "independent_evaluation",
                    "release_gate", "separate_judge_call", "reason_code")}})
        subscription = [m for m in model if m["billing_mode"] == "subscription"]
        cost_ids = {c["span_id"] for c in costs}
        traces.append({"trace_id": trace_id, "spans": len(spans),
            "roots": [{"span_id": s["span_id"], "status": s["status_code"]} for s in roots],
            "topology": [{k: s[k] for k in ("span_id", "parent_id", "name", "span_kind", "status_code")} for s in spans],
            "checks": {
                "single_persisted_root": len(roots) == 1,
                "all_parents_persisted": not missing,
                "subscription_identity_consistent": all(m["provider"] == "codex_cli" and
                    m["auth_mode"] == "chatgpt_subscription" for m in subscription),
                "subscription_cost_null": all(m["cost_usd"] is None for m in subscription),
                "no_subscription_api_cost_rows": not any(m["span_id"] in cost_ids for m in subscription),
                "no_raw_exception_events": not any(json.loads(s["events"] or "[]") for s in spans),
            },
            "missing_parent_span_ids": missing, "model_spans": model,
            "error_codes": sorted({s["status_message"] for s in spans if s["status_code"] == "ERROR" and s["status_message"]}),
            "annotations": trace_annotations,
            "auxiliary_semantic_reviews": semantic_reviews,
            "code_checks_passed": sum(a["score"] == 1 for a in trace_annotations if a["annotator_kind"] == "CODE"),
            "code_checks_total": sum(a["annotator_kind"] == "CODE" for a in trace_annotations),
            "actual_model_calls": sum(m["model_call_succeeded"] for m in model),
        })
    return {"read_at": datetime.now(timezone.utc).isoformat(), "method": "sqlite_read_only",
            "project_name": project_name, "traces": traces, "trace_count": len(traces),
            "limitations": ["A persisted span verifies observability, not model correctness or enterprise readiness.",
                "Phoenix token columns may default to zero when usage is unknown; use application metadata.",
                "An in-progress trace can temporarily have missing parent/root spans."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path(__file__).parents[1] / "runtime/phoenix/phoenix.sqlite")
    parser.add_argument("--project", default="hermes-enterprise-development")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = read_persisted_feedback(args.database, args.project)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"project": args.project, "trace_count": result["trace_count"],
        "summaries": [{"trace_id": t["trace_id"], "spans": t["spans"], "checks": t["checks"],
            "code_checks_passed": t["code_checks_passed"], "code_checks_total": t["code_checks_total"],
            "actual_model_calls": t["actual_model_calls"], "error_codes": t["error_codes"]} for t in result["traces"]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
