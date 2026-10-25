"""Synthetic negative cases for model transport and business orchestration.

No CLI login or external model request is made. These are safety-contract tests,
not evidence of model answer quality or institution deployment readiness.
"""
import json
import os
from pathlib import Path
import sys
import time

import pytest

from donghaeng_agent.provider import CodexSubscriptionProvider, ProviderUnavailable, model_configuration, normalize_decision
from donghaeng_agent.repository import InvestigationRepository, RepositoryError
from donghaeng_agent.worker import InvestigationWorker
import donghaeng_agent.graph as graph_module


def final(refs=None):
    return {"decision": "final", "calls": [], "text": "Synthetic fixture: 확인 필요.", "evidence_ids": refs or []}


def calls(*items):
    return {"decision": "tools", "calls": [{"name": name, "arguments": json.dumps(arguments)} for name, arguments in items],
            "text": "", "evidence_ids": []}


def program(tmp_path, source):
    path = tmp_path / "synthetic_transport.py"
    path.write_text(source)
    return [sys.executable, str(path)]


@pytest.mark.parametrize("event", [[], None, {"type": []}, {"type": "item.completed", "item": None}])
def test_non_object_or_malformed_events_fail_with_structured_error(tmp_path, event):
    argv = program(tmp_path, f"print({json.dumps(json.dumps(event))})")
    with pytest.raises(ProviderUnavailable, match="model_invalid_event_stream"):
        CodexSubscriptionProvider(timeout_seconds=3)._run(argv, "{}", None)


def test_incomplete_trailing_event_cannot_hide_after_turn_completed(tmp_path):
    events = [{"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(final())}}, {"type": "turn.completed"}]
    source = "import sys\n" + "\n".join(f"print({json.dumps(json.dumps(event))}, flush=True)" for event in events)
    source += "\nsys.stdout.write('{\\\"type\\\":\\\"item.started\\\",')\nsys.stdout.flush()"
    argv = program(tmp_path, source)
    with pytest.raises(ProviderUnavailable, match="model_invalid_event_stream"):
        CodexSubscriptionProvider(timeout_seconds=3)._run(argv, "{}", None)


def test_process_that_closes_pipes_still_has_model_deadline(tmp_path):
    argv = program(tmp_path, "import os,time\nos.close(1)\nos.close(2)\ntime.sleep(30)")
    before = time.monotonic()
    with pytest.raises(ProviderUnavailable, match="model_timeout"):
        CodexSubscriptionProvider(timeout_seconds=1)._run(argv, "{}", None)
    assert time.monotonic() - before < 4


def test_output_limit_applies_to_stderr_without_retaining_secret(tmp_path):
    argv = program(tmp_path, "import sys,time\nsys.stderr.write('sensitive-diagnostic' * 120000)\nsys.stderr.flush()\ntime.sleep(30)")
    with pytest.raises(ProviderUnavailable) as error:
        CodexSubscriptionProvider(timeout_seconds=3)._run(argv, "{}", None)
    assert error.value.code == "model_output_too_large"
    assert "sensitive" not in str(error.value)


class ScriptedProvider(CodexSubscriptionProvider):
    def __init__(self, responses):
        self.model = "synthetic-controlled-model"
        self.responses = iter(responses)
        self.call_count = 0

    def decide(self, messages, allow_tools=True, cancel_check=None):
        self.call_count += 1
        if cancel_check:
            cancel_check()
        response = next(self.responses)
        value = response(messages, cancel_check) if callable(response) else response
        result = normalize_decision(value, allow_tools)
        result["metrics"] = {"model_call_succeeded": True, "provider": "synthetic_test", "billing_mode": "test", "synthetic": True}
        return result


class SyntheticTools:
    """Infrastructure-only fixture; no fabricated record enters application data."""
    def __init__(self, failed_detail=False):
        self.failed_detail = failed_detail
        self.searches = self.detail_calls = self.condition_calls = 0

    def versions(self):
        return {"snapshot_sha256": "synthetic-stable"}

    def measurements(self, scope, execution_id):
        return {"scope": scope, "snapshot_version": "synthetic", "sk_mode": "excluded", "measurement_status": "held",
                "evidence": [{"evidence_id": "ev_public_fixture", "source_id": "synthetic", "version_hash": "fixture"}],
                "observations": [{"kind": "survey_i5_population_reference", "status": "held", "data": None, "evidence_id": "ev_public_fixture"}]}

    def quality(self, measurements):
        return {"actions": [], "questions": []}

    def search(self, scope, support_type="general", exclude_ids=None):
        self.searches += 1
        return {"candidates": [{"id": "fixture_source"}]}

    def details(self, candidate_id):
        self.detail_calls += 1
        if self.failed_detail:
            return {"status": "failed", "error": {"code": "capture_hash_mismatch"}}
        return {"status": "ok", "content_hash": "fixture-content", "text_hash": "fixture-text", "metadata_hash": "fixture-metadata",
                "source": {"id": candidate_id, "title": "Controlled synthetic source", "url": "https://example.invalid/fixture",
                           "retrieved_at": "2026-01-01", "capture_path": "synthetic", "conditions": []}}

    def check_conditions(self, scope, details):
        self.condition_calls += 1
        return {"conditions": [], "eligibility_status": "needs_confirmation", "currently_accepting_confirmed": False,
                "excluded": False, "reasons": []}


def execute(tmp_path, monkeypatch, responses, *, strategy="agent", frozen_change=None, tools=None, timeout=480):
    provider = ScriptedProvider(responses)
    monkeypatch.setattr(graph_module, "create_provider", lambda: provider)
    tools = tools or SyntheticTools()
    repository = InvestigationRepository(tmp_path / "business.sqlite3")
    case = repository.create({"idempotency_key": "create-synthetic", "sk_mode": "excluded", "measurement_as_of": "2025-12", "region_code": "1168066000"})
    configuration = model_configuration(provider)
    if frozen_change:
        configuration.update(frozen_change)
    repository.enqueue(case["case_id"], {"idempotency_key": "run-synthetic", "expected_case_version": 1, "strategy": strategy},
                       {**tools.versions(), "model_config": configuration})
    worker = InvestigationWorker(repository, tools, tmp_path / "checkpoints.sqlite3", execution_timeout_seconds=timeout)
    try:
        assert worker.drain_once()
    finally:
        worker.close()
    return repository.get(case["case_id"]), provider, tools, repository


@pytest.mark.parametrize("strategy", ["agent", "single_search"])
def test_unknown_model_evidence_reference_blocks_brief(tmp_path, monkeypatch, strategy):
    case, provider, _, _ = execute(tmp_path, monkeypatch, [final(["invented_source"])], strategy=strategy)
    assert provider.call_count == 1
    assert not case["briefs"]
    assert case["executions"][0]["error"]["code"] == "model_unknown_evidence_reference"


@pytest.mark.parametrize("field", ["provider", "model", "prompt_version", "decision_schema_sha256", "tool_schema_sha256"])
def test_frozen_model_contract_change_fails_before_call(tmp_path, monkeypatch, field):
    case, provider, _, repo = execute(tmp_path, monkeypatch, [final()], frozen_change={field: "changed"})
    assert provider.call_count == 0
    assert case["executions"][0]["error"]["code"] == "model_configuration_changed_before_execution"
    with pytest.raises(RepositoryError) as error:
        repo.retry(case["case_id"], {"execution_id": case["executions"][0]["execution_id"],
                                  "idempotency_key": "invalid-retry", "expected_case_version": case["case_version"]})
    assert error.value.code == "new_execution_required"


def test_valid_model_reference_is_preserved_separately_from_server_claims(tmp_path, monkeypatch):
    case, _, _, _ = execute(tmp_path, monkeypatch, [final(["ev_public_fixture"])], strategy="single_search")
    brief = case["briefs"][0]
    assert brief["model_evidence_ids"] == ["ev_public_fixture"]
    assert brief["claims"][0]["text"] == "인구구성 가중 I5 참고지수 확인"
    assert brief["quality_status"] == "hold" and brief["operational_alert_allowed"] is False
    assert brief["verification"]["model_reference_status"] == "known_evidence_only"
    assert brief["verification"]["model_text_status"].startswith("unverified_advisory")


def test_retrieved_but_unchecked_detail_can_be_cited_without_becoming_candidate(tmp_path, monkeypatch):
    def cite_detail(messages, _):
        detail = next(message["content"] for message in reversed(messages) if message.get("name") == "fetch_support_details")
        return final([detail["evidence_id"]])
    responses = [calls(("search_official_support", {"support_type": "general", "exclude_ids": []})),
                 calls(("fetch_support_details", {"candidate_id": "fixture_source"})), cite_detail]
    case, _, _, _ = execute(tmp_path, monkeypatch, responses)
    brief = case["briefs"][0]
    assert not brief["support_candidates"]
    assert brief["excluded_candidates"][0]["reason"] == "conditions_not_checked"
    assert set(brief["model_evidence_ids"]) <= {item["evidence_id"] for item in case["evidence"]}


def test_conditions_cannot_run_before_detail_even_for_listed_candidate(tmp_path, monkeypatch):
    responses = [calls(("search_official_support", {"support_type": "general", "exclude_ids": []})),
                 calls(("check_support_conditions", {"candidate_id": "fixture_source"}))]
    case, _, tools, _ = execute(tmp_path, monkeypatch, responses)
    assert tools.condition_calls == 0
    assert case["executions"][0]["error"]["code"] == "support_details_required_before_conditions"


def test_duplicate_read_calls_are_still_charged_against_tool_budget(tmp_path, monkeypatch):
    responses = [calls(*[("inspect_observation_quality", {}) for _ in range(12)]), calls(("inspect_observation_quality", {}))]
    case, provider, _, _ = execute(tmp_path, monkeypatch, responses)
    assert provider.call_count == 2
    assert case["executions"][0]["error"]["code"] == "tool_budget_exhausted"
    assert not case["briefs"]


def test_failed_detail_never_becomes_candidate_or_fake_success(tmp_path, monkeypatch):
    case, _, tools, _ = execute(tmp_path, monkeypatch, [final(["ev_public_fixture"])], strategy="single_search", tools=SyntheticTools(failed_detail=True))
    brief = case["briefs"][0]
    assert not brief["support_candidates"]
    assert brief["excluded_candidates"][0]["reason"] == "capture_hash_mismatch"
    assert not any(item.get("kind") == "official_support" for item in case["evidence"])


def test_whole_job_deadline_interrupts_during_model_callback(tmp_path, monkeypatch):
    def delayed(_messages, cancellation):
        time.sleep(0.04)
        cancellation()
        return final()
    case, _, _, _ = execute(tmp_path, monkeypatch, [delayed], timeout=0.03)
    assert case["executions"][0]["error"]["code"] == "agent_wall_time_exhausted"
    assert not case["briefs"]


@pytest.mark.parametrize("field", ["tool_version", "graph_version", "prompt_version", "signal_rule_version"])
def test_changed_execution_component_version_is_blocked(tmp_path, monkeypatch, field):
    class ChangingVersions(SyntheticTools):
        def __init__(self):
            super().__init__()
            self.reads = 0
        def versions(self):
            self.reads += 1
            return {**super().versions(), field: "before" if self.reads == 1 else "after"}
    case, provider, _, _ = execute(tmp_path, monkeypatch, [final()], tools=ChangingVersions())
    assert provider.call_count == 0
    assert case["executions"][0]["error"]["code"] == "source_version_changed_before_execution"
