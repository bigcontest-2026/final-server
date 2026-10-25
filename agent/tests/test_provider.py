"""Provider contracts and child-process failures; never call a real model in pytest."""
import json
import os
from pathlib import Path
import sys
import time

import pytest

from donghaeng_agent.provider import (CodexSubscriptionProvider, ProviderUnavailable,
    create_provider, normalize_decision, _prompt)


def final(**changes):
    return {"decision": "final", "calls": [], "text": "조건은 확인이 필요합니다.", "evidence_ids": [], **changes}


@pytest.mark.parametrize("value,code", [
    (final(extra=True), "model_output_schema_error"),
    (final(text=""), "model_empty_response"),
    (final(evidence_ids=[1]), "model_output_schema_error"),
    (final(decision="tools"), "model_output_schema_error"),
    (final(decision="tools", calls=[{"name": "shell", "arguments": "{}"}]), "invalid_model_tool"),
    (final(decision="tools", calls=[{"name": "fetch_support_details", "arguments": '{"candidate_id":1}'}]), "invalid_model_tool"),
    (final(decision="tools", calls=[{"name": "inspect_observation_quality", "arguments": '{"extra":1}'}]), "invalid_model_tool"),
])
def test_reject_untrusted_decisions(value, code):
    with pytest.raises(ProviderUnavailable) as error:
        normalize_decision(value)
    assert error.value.code == code


def test_summary_cannot_request_tools():
    with pytest.raises(ProviderUnavailable, match="model_tools_not_allowed"):
        normalize_decision(final(decision="tools", calls=[{"name": "inspect_observation_quality", "arguments": "{}"}]), False)


def test_api_credentials_never_inherited_and_no_fallback(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-test-key")
    monkeypatch.setenv("CODEX_API_KEY", "secret-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://other")
    monkeypatch.delenv("DONGHAENG_PROVIDER", raising=False)
    assert isinstance(create_provider(), CodexSubscriptionProvider)
    env = CodexSubscriptionProvider._environment()
    assert not {"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"} & set(env)


def test_disabled_capabilities_and_fixed_json_contract(tmp_path):
    args = CodexSubscriptionProvider(executable="codex")._argv(tmp_path)
    for flag in ("--ignore-user-config", "--ephemeral", "--output-schema"):
        assert flag in args
    assert 'forced_login_method="chatgpt"' in args
    assert 'web_search="disabled"' in args
    assert "shell_tool" in args and "apps" in args and "plugins" in args
    assert "--dangerously-bypass-approvals-and-sandbox" not in args
    assert "--ignore-rules" not in args


def program(tmp_path, source):
    path = tmp_path / "fake_cli.py"
    path.write_text(source)
    return [sys.executable, str(path)]


def test_complete_jsonl_and_no_diagnostics_retained(tmp_path):
    events = [{"type":"thread.started"}, {"type":"item.completed", "item":{"type":"agent_message", "text":json.dumps(final())}},
              {"type":"turn.completed", "usage":{"input_tokens":12,"output_tokens":7}}]
    args = program(tmp_path, "import json,sys\nsys.stderr.write('secret-auth-diagnostic')\n" + "\n".join(f"print({json.dumps(json.dumps(e))})" for e in events))
    text, usage = CodexSubscriptionProvider(timeout_seconds=3)._run(args, "{}", None)
    assert normalize_decision(json.loads(text))["text"]
    assert usage == {"input_tokens":12,"output_tokens":7}
    assert "secret" not in text


@pytest.mark.parametrize("event,code", [
    ({"type":"turn.failed"}, "codex_turn_failed"),
    ({"type":"item.started","item":{"type":"command_execution"}}, "codex_unexpected_tool_execution"),
    ({"type":"item.completed","item":{"type":"agent_message","text":"fake success"}}, "codex_incomplete_turn"),
])
def test_failed_or_unexpected_events_cannot_succeed(tmp_path, event, code):
    args = program(tmp_path, f"print({json.dumps(json.dumps(event))})")
    with pytest.raises(ProviderUnavailable, match=code):
        CodexSubscriptionProvider(timeout_seconds=3)._run(args, "{}", None)


def test_timeout_kills_and_reaps_process(tmp_path):
    pidfile = tmp_path / "pid"
    args = program(tmp_path, f"import os,time\nopen({str(pidfile)!r},'w').write(str(os.getpid()))\ntime.sleep(30)")
    before = time.monotonic()
    with pytest.raises(ProviderUnavailable, match="model_timeout"):
        CodexSubscriptionProvider(timeout_seconds=1)._run(args, "{}", None)
    assert time.monotonic() - before < 5
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_cancellation_terminates_process(tmp_path):
    def cancel():
        raise RuntimeError("lease_lost")
    args = program(tmp_path, "import time\ntime.sleep(30)")
    with pytest.raises(RuntimeError, match="lease_lost"):
        CodexSubscriptionProvider(timeout_seconds=3)._run(args, "x" * 100000, cancel)


def test_input_bound_before_process():
    with pytest.raises(ProviderUnavailable, match="model_input_too_large"):
        _prompt([{"content":"x"*180000}], False)


def test_model_concurrency_rejects_second_process_slot():
    from donghaeng_agent.provider import _subscription_slot
    with _subscription_slot():
        with pytest.raises(ProviderUnavailable, match="model_concurrency_limit"):
            with _subscription_slot():
                raise AssertionError("a second model was admitted")


def test_model_transport_receives_minimized_public_evidence():
    value = [{"role":"user", "content":json.dumps({"scope":{"purpose":"private prose", "idempotency_key":"secret-command", "region_code":"1168066000"},
        "observations":[{"evidence_id":"ev_1", "value":33.25}], "api_key":"unprefixed-secret"})}]
    wire = _prompt(value, False)
    assert "private prose" not in wire and "secret-command" not in wire and "unprefixed-secret" not in wire
    clean = json.loads(json.loads(wire)["conversation"][0]["content"])
    assert clean["observations"] == [{"evidence_id":"ev_1","value":33.25}]


def test_expected_cli_capability_notices_do_not_mask_unknown_errors(tmp_path):
    notices = ["Code Mode is unavailable because code-mode host is disabled.",
               "Exceeded skills context budget. All skill descriptions were removed"]
    events = [{"type":"item.completed", "item":{"type":"error", "message":text}} for text in notices]
    events += [{"type":"item.completed", "item":{"type":"agent_message", "text":json.dumps(final())}}, {"type":"turn.completed"}]
    argv = program(tmp_path, "\n".join(f"print({json.dumps(json.dumps(event))})" for event in events))
    text, usage = CodexSubscriptionProvider(timeout_seconds=3)._run(argv, "{}", None)
    assert json.loads(text)["decision"] == "final" and usage is None
    argv = program(tmp_path, 'print(\'{"type":"item.completed","item":{"type":"error","message":"arbitrary error"}}\')')
    with pytest.raises(ProviderUnavailable, match="codex_unexpected_error_event"):
        CodexSubscriptionProvider(timeout_seconds=3)._run(argv, "{}", None)
