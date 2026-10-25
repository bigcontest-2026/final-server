import importlib.util
import json
from pathlib import Path
import sqlite3

spec = importlib.util.spec_from_file_location("read_phoenix_feedback", Path(__file__).parents[1] / "evaluation/read_phoenix_feedback.py")
readback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readback)


def test_readback_is_project_scoped_and_does_not_treat_unknown_tokens_as_zero(tmp_path):
    path = tmp_path / "phoenix.sqlite"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE projects(id INTEGER,name TEXT);
        CREATE TABLE traces(id INTEGER,project_rowid INTEGER,trace_id TEXT);
        CREATE TABLE spans(id INTEGER,trace_rowid INTEGER,span_id TEXT,parent_id TEXT,name TEXT,span_kind TEXT,
            status_code TEXT,status_message TEXT,llm_token_count_prompt INTEGER,llm_token_count_completion INTEGER,
            attributes TEXT,events TEXT);
        CREATE TABLE span_annotations(id INTEGER,span_rowid INTEGER,name TEXT,score REAL,label TEXT,annotator_kind TEXT,metadata TEXT);
        CREATE TABLE span_costs(span_rowid INTEGER,total_cost REAL);
        INSERT INTO projects VALUES(1,'selected'),(2,'unrelated');
        INSERT INTO traces VALUES(1,1,'target_trace'),(2,2,'unrelated_trace');
    """)
    attrs = json.dumps({"metadata": {"provider": "codex_cli", "billing_mode": "subscription",
        "auth_mode": "chatgpt_subscription", "cost_usd": None, "usage": None},
        "output": {"value": json.dumps({"text": "private answer marker", "tool_names": ["allowed_tool"],
            "evidence_ids": ["ev_public"]})}})
    con.execute("INSERT INTO spans VALUES(1,1,'model_span','missing_root','model.call','LLM','ERROR','model_failed',0,0,?,'[]')", (attrs,))
    con.execute("INSERT INTO spans VALUES(2,2,'secret_span',NULL,'private','AGENT','UNSET',NULL,NULL,NULL,'{}','[]')")
    con.execute("INSERT INTO span_annotations VALUES(1,1,'hermes.model_provenance',0,'fail','CODE','{}')")
    con.commit()
    con.close()
    before = path.read_bytes()
    result = readback.read_persisted_feedback(path, "selected")
    assert path.read_bytes() == before
    assert result["trace_count"] == 1
    trace = result["traces"][0]
    assert not trace["checks"]["all_parents_persisted"]
    assert trace["missing_parent_span_ids"] == ["model_span"]
    assert trace["model_spans"][0]["token_usage_status"] == "not_reported"
    assert trace["model_spans"][0]["usage"] is None
    assert trace["model_spans"][0]["output_contract_fields"] == ["evidence_ids", "text", "tool_names"]
    assert trace["model_spans"][0]["output_tool_count"] == 1
    assert trace["model_spans"][0]["output_evidence_count"] == 1
    assert trace["model_spans"][0]["output_text_characters"] == len("private answer marker")
    assert trace["code_checks_total"] == 1
    assert "private" not in json.dumps(result)
