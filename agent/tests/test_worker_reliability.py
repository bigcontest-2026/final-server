"""Fault and concurrency tests for durable ownership, restart and cancellation.

Synthetic fixtures validate infrastructure only; they are not policy-quality or
production-load evidence and do not call an external model.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from donghaeng_agent.repository import InvestigationRepository, RepositoryError, SCHEMA
from donghaeng_agent.worker import InvestigationWorker
import donghaeng_agent.worker as worker_module


@pytest.fixture
def repo(tmp_path):
    return InvestigationRepository(tmp_path / "business.sqlite3")


def queued(repo, key="case"):
    case = repo.create({"idempotency_key": key, "purpose": "Controlled infrastructure fixture"})
    execution = repo.enqueue(case["case_id"], {
        "expected_case_version": case["case_version"], "idempotency_key": "run", "strategy": "rule_baseline",
    }, {})
    return case["case_id"], execution["execution_id"]


def expire(repo, job):
    with repo.connection(write=True) as db:
        db.execute("UPDATE jobs SET lease_until=0 WHERE job_id=?", (job["job_id"],))


def test_existing_schema_migration_is_additive_and_repeatable(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO cases(case_id,version,request,status,created_at,updated_at) VALUES('old',7,'{}','draft','old','old')")
    for _ in range(2):
        repository = InvestigationRepository(path)
        assert repository.get("old")["case_version"] == 7
        with repository.connection() as db:
            assert {"lease_token", "lease_owner"} <= {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            assert [row[0] for row in db.execute("SELECT version FROM schema_versions ORDER BY version")] == [1, 2]


def test_separate_repository_instances_cannot_claim_same_job(repo):
    queued(repo)
    other = InvestigationRepository(repo.path)
    barrier = threading.Barrier(2)
    def claim(repository):
        barrier.wait()
        return repository.claim(owner="different-worker")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, (repo, other)))
    claimed = [result for result in results if result]
    assert len(claimed) == 1
    assert claimed[0]["status"] == "running" and claimed[0]["attempts"] == 1
    assert claimed[0]["lease_token"] and claimed[0]["lease_owner"]


def test_concurrent_idempotent_create_has_one_case(repo):
    other = InvestigationRepository(repo.path)
    barrier = threading.Barrier(2)
    def create(repository):
        barrier.wait()
        return repository.create({"idempotency_key": "same-input"})
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, (repo, other)))
    assert results[0] == results[1]
    assert len(repo.list()) == 1


def test_stale_owner_cannot_heartbeat_complete_or_release_reclaimed_job(repo):
    case_id, execution_id = queued(repo)
    first = repo.claim(owner="old")
    expire(repo, first)
    assert not repo.heartbeat(first)
    second = InvestigationRepository(repo.path).claim(owner="new")
    assert second["attempts"] == 2 and second["lease_token"] != first["lease_token"]
    assert not repo.finish_job(first, "failed", {"code": "late_result"})
    assert not repo.release_job(first)
    assert repo.execution(execution_id)["status"] == "running"
    assert repo.finish_job(second, "interrupted")
    after = repo.get(case_id)
    assert not repo.finish_job(second, "failed")
    assert repo.get(case_id) == after
    assert len([event for event in after["events"] if event["type"] == "execution_interrupted"]) == 1


def test_expired_owner_cannot_write_even_before_reclaim(repo):
    case_id, execution_id = queued(repo)
    job = repo.claim()
    expire(repo, job)
    with pytest.raises(RepositoryError, match="작업 소유권") as error:
        with repo.job_scope(job):
            repo.event(case_id, execution_id, "late_write", {}, "late")
    assert error.value.code == "job_lease_lost"
    assert all(event["type"] != "late_write" for event in repo.get(case_id)["events"])


def test_tool_late_result_is_discarded_after_takeover(repo):
    _, execution_id = queued(repo)
    job = repo.claim()
    def delayed_tool():
        # A process can lose its lease while waiting for a remote response.
        with repo.connection() as db:
            db.execute("UPDATE jobs SET lease_until=0 WHERE job_id=?", (job["job_id"],))
        assert InvestigationRepository(repo.path).claim(owner="replacement")
        return {"sensitive": "must not become committed output"}
    with pytest.raises(RepositoryError) as error, repo.job_scope(job):
        repo.tool_result(execution_id, "step", "read", {}, delayed_tool)
    assert error.value.code == "job_lease_lost"
    assert repo.execution_metrics(execution_id)["tool_calls"] == 0


def test_cached_step_rejects_different_tool_identity(repo):
    _, execution_id = queued(repo)
    assert repo.tool_result(execution_id, "step", "tool_a", {}, lambda: {"ok": True}) == {"ok": True}
    with pytest.raises(RepositoryError) as error:
        repo.tool_result(execution_id, "step", "tool_b", {}, lambda: {"ok": False})
    assert error.value.code == "tool_cache_conflict"


def test_subscription_metrics_are_not_mislabeled_as_api_dollars(repo):
    _, execution_id = queued(repo)
    repo.tool_result(execution_id, "model", "model_summary", {}, lambda: {"metrics": {
        "model_call_succeeded": True, "provider": "codex_cli", "auth_mode": "chatgpt",
        "billing_mode": "chatgpt_subscription", "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }})
    metrics = repo.execution_metrics(execution_id)
    assert metrics["actual_model_calls"] == 1
    assert metrics["token_usage"] == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert metrics["cost_usd"] is None and "subscription_quota_not_api_dollars" in metrics["cost_status"]
    repo.tool_result(execution_id, "legacy_model", "model_decide", {}, lambda: {"metrics": {
        "actual_api_success": True, "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
    }})
    metrics = repo.execution_metrics(execution_id)
    assert metrics["actual_model_calls"] == 2
    assert metrics["token_usage"]["total_tokens"] == 21
    assert metrics["billing_modes"] == ["api_tokens", "chatgpt_subscription"]
    assert metrics["cost_usd"] is None


class DummyGraph:
    def __init__(self, invoke):
        self._invoke = invoke

    def get_state(self, config):
        return SimpleNamespace(values={}, tasks=())

    def invoke(self, payload, config):
        return self._invoke(payload, config)


def dummy_worker(monkeypatch, repo, tmp_path, invoke, **kwargs):
    def build_dummy(*args, welfare_request=None):
        assert welfare_request is None
        return DummyGraph(invoke)
    monkeypatch.setattr(worker_module, "build_investigation_graph", build_dummy)
    return InvestigationWorker(repo, None, tmp_path / "checkpoint.sqlite3", **kwargs)


def test_heartbeat_prevents_reclaim_during_call_longer_than_initial_lease(monkeypatch, repo, tmp_path):
    _, execution_id = queued(repo)
    entered, release = threading.Event(), threading.Event()
    def long_call(*_):
        entered.set()
        assert release.wait(3)
        return {}
    worker = dummy_worker(monkeypatch, repo, tmp_path, long_call, lease_seconds=0.3, heartbeat_seconds=0.03)
    worker.start()
    try:
        assert entered.wait(2)
        time.sleep(0.45)
        assert InvestigationRepository(repo.path).claim() is None
        release.set()
        deadline = time.monotonic() + 2
        while repo.execution(execution_id)["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert repo.execution(execution_id)["status"] == "succeeded"
    finally:
        release.set()
        assert worker.close()


def test_shutdown_cancels_cooperative_call_requeues_and_allows_restart(monkeypatch, repo, tmp_path):
    _, execution_id = queued(repo)
    entered = threading.Event()
    def cancellable(*_):
        entered.set()
        while True:
            repo.check_active_job()
            time.sleep(0.01)
    worker = dummy_worker(monkeypatch, repo, tmp_path, cancellable)
    worker.start()
    assert entered.wait(2)
    assert worker.close(timeout=2)
    assert repo.execution(execution_id)["status"] == "queued"
    replacement = dummy_worker(monkeypatch, repo, tmp_path, lambda *_: {})
    try:
        assert replacement.drain_once()
        assert repo.execution(execution_id)["status"] == "succeeded"
        assert repo.execution(execution_id)["metrics"]["job_attempt"] == 2
    finally:
        replacement.close()


def test_close_does_not_close_checkpoint_connection_under_active_tool(monkeypatch, repo, tmp_path):
    _, execution_id = queued(repo)
    entered, release = threading.Event(), threading.Event()
    def uninterruptible(*_):
        entered.set()
        assert release.wait(3)
        return {}
    worker = dummy_worker(monkeypatch, repo, tmp_path, uninterruptible, lease_seconds=0.3, heartbeat_seconds=0.03)
    worker.start()
    try:
        assert entered.wait(2)
        assert worker.close(timeout=0.01) is False
        assert worker.checkpoint_connection.execute("SELECT 1").fetchone() == (1,)
        time.sleep(0.35)
        assert InvestigationRepository(repo.path).claim() is None
    finally:
        release.set()
        assert worker.close(timeout=2)
    assert repo.execution(execution_id)["status"] == "queued"


class State(TypedDict, total=False):
    case_id: str
    execution_id: str
    brief: dict
    review: dict


def review_graph(repo, _tools, saver, *, welfare_request=None):
    assert welfare_request is None
    def prepare(state):
        repo.tool_result(state["execution_id"], "once", "fixture_read", {}, lambda: {"ok": True})
        return {"brief": repo.put_brief(state["case_id"], state["execution_id"], {
            "measurement_status": "held", "support_status": "needs_confirmation", "open_questions": [],
        })}
    def review(state):
        return {"review": interrupt({"brief": state["brief"]})}
    def finalize(state):
        repo.apply_review(state["case_id"], state["execution_id"], state["review"])
        return {}
    graph = StateGraph(State)
    for name, fn in (("prepare", prepare), ("review", review), ("finalize", finalize)):
        graph.add_node(name, fn)
    for source, target in ((START, "prepare"), ("prepare", "review"), ("review", "finalize"), ("finalize", END)):
        graph.add_edge(source, target)
    return graph.compile(checkpointer=saver)


def review_request(repo, case_id):
    case = repo.get(case_id)
    brief = case["briefs"][0]
    return {"expected_case_version": case["case_version"], "brief_id": brief["brief_id"],
            "brief_version": brief["brief_version"], "decision": "accept", "idempotency_key": "review"}


def test_restart_reconciles_checkpoint_before_finish_and_consumed_review(monkeypatch, repo, tmp_path):
    monkeypatch.setattr(worker_module, "build_investigation_graph", review_graph)
    case_id, execution_id = queued(repo)
    worker = InvestigationWorker(repo, None, tmp_path / "checkpoint.sqlite3")
    original_finish = repo.finish_job
    # Simulate loss after the graph checkpoint committed but before job completion.
    repo.finish_job = lambda *args, **kwargs: False
    assert worker.drain_once()
    with repo.connection() as db:
        db.execute("UPDATE jobs SET lease_until=0 WHERE status='running'")
    worker.close()
    repo.finish_job = original_finish
    worker = InvestigationWorker(repo, None, tmp_path / "checkpoint.sqlite3")
    assert worker.drain_once()
    assert repo.get(case_id)["status"] == "awaiting_review"
    assert repo.execution_metrics(execution_id)["tool_calls"] == 1
    repo.review(case_id, review_request(repo, case_id))
    repo.finish_job = lambda *args, **kwargs: False
    assert worker.drain_once()
    assert repo.get(case_id)["status"] == "reviewed"
    with repo.connection() as db:
        db.execute("UPDATE jobs SET lease_until=0 WHERE status='running'")
    worker.close()
    repo.finish_job = original_finish
    worker = InvestigationWorker(repo, None, tmp_path / "checkpoint.sqlite3")
    try:
        assert worker.drain_once()
        case = repo.get(case_id)
        assert case["status"] == "reviewed" and repo.execution(execution_id)["status"] == "succeeded"
        assert len(case["briefs"]) == 1 and len(case["reviews"]) == 1
        assert sum(event["type"] == "review_applied" for event in case["events"]) == 1
    finally:
        worker.close()


def test_fenced_checkpoint_refuses_stale_write(monkeypatch, repo, tmp_path):
    monkeypatch.setattr(worker_module, "build_investigation_graph", review_graph)
    _, execution_id = queued(repo)
    job = repo.claim()
    worker = InvestigationWorker(repo, None, tmp_path / "checkpoint.sqlite3")
    from langgraph.checkpoint.base import empty_checkpoint
    config = {"configurable": {"thread_id": execution_id, "checkpoint_ns": ""}}
    try:
        with repo.job_scope(job):
            saved = worker.saver.put(config, empty_checkpoint(), {"source": "input", "step": -1, "parents": {}}, {})
        expire(repo, job)
        assert InvestigationRepository(repo.path).claim()
        with pytest.raises(RepositoryError), repo.job_scope(job):
            worker.saver.put(saved, empty_checkpoint(), {"source": "loop", "step": 0, "parents": {}}, {})
        assert len(list(worker.saver.list(config))) == 1
    finally:
        worker.close()


def test_unknown_token_usage_is_not_reported_as_zero(repo):
    _, execution_id = queued(repo)
    repo.tool_result(execution_id, "unknown_usage", "model_decide", {}, lambda: {"metrics": {
        "model_call_succeeded": True, "provider": "codex_cli", "billing_mode": "subscription", "usage": None,
    }})
    result = repo.execution_metrics(execution_id)
    assert result["token_usage"] == {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    assert not result["token_usage_complete"]
    assert result["actual_model_calls"] == 1


def test_retry_preserves_review_not_yet_consumed_by_checkpoint(monkeypatch, repo, tmp_path):
    monkeypatch.setattr(worker_module, "build_investigation_graph", review_graph)
    case_id, execution_id = queued(repo)
    worker = InvestigationWorker(repo, None, tmp_path / "checkpoint.sqlite3")
    try:
        assert worker.drain_once()
        repo.review(case_id, review_request(repo, case_id))
        original_invoke = worker.graph.invoke
        def failure_before_resume(*_args, **_kwargs):
            raise RuntimeError("private response text must never persist")
        worker.graph.invoke = failure_before_resume
        assert worker.drain_once()
        failed = repo.get(case_id)
        assert failed["executions"][0]["status"] == "failed"
        assert "private response text" not in str(failed)
        request = {"execution_id": execution_id, "expected_case_version": failed["case_version"], "idempotency_key": "retry-review"}
        assert repo.retry(case_id, request) == repo.retry(case_id, request)
        worker.graph.invoke = original_invoke
        assert worker.drain_once()
        case = repo.get(case_id)
        assert case["status"] == "reviewed" and case["executions"][0]["status"] == "succeeded"
        assert len(case["reviews"]) == 1 and len(case["briefs"]) == 1
    finally:
        worker.close()


def test_lost_lease_in_worker_discards_late_business_result(monkeypatch, repo, tmp_path):
    case_id, execution_id = queued(repo)
    replacement_job = []
    def lost_during_call(*_):
        with repo.connection() as db:
            db.execute("UPDATE jobs SET lease_until=0 WHERE execution_id=?", (execution_id,))
        replacement_job.append(InvestigationRepository(repo.path).claim())
        repo.event(case_id, execution_id, "forbidden_late_write", {}, "late")
        return {}
    worker = dummy_worker(monkeypatch, repo, tmp_path, lost_during_call)
    try:
        assert worker.drain_once()
        assert worker.last_error_code == "job_lease_lost"
        assert repo.execution(execution_id)["status"] == "running"
        assert all(event["type"] != "forbidden_late_write" for event in repo.get(case_id)["events"])
        assert repo.finish_job(replacement_job[0], "interrupted")
    finally:
        worker.close()


def test_model_quota_failure_is_explicit_and_never_automatically_retried(monkeypatch, repo, tmp_path):
    from donghaeng_agent.provider import ProviderUnavailable
    _, execution_id = queued(repo)
    calls = []
    def limited(*_):
        calls.append(1)
        raise ProviderUnavailable("subscription_quota_exhausted")
    worker = dummy_worker(monkeypatch, repo, tmp_path, limited)
    try:
        assert worker.drain_once()
        assert worker.drain_once() is False
        execution = repo.execution(execution_id)
        assert execution["status"] == "failed"
        assert execution["error"]["code"] == "subscription_quota_exhausted"
        assert execution["error"]["retryable"] is False
        assert len(calls) == 1
    finally:
        worker.close()


def test_tool_trace_reports_cache_hit_without_recording_payload(monkeypatch, repo):
    from contextlib import contextmanager
    import donghaeng_agent.repository as repository_module
    spans = []
    class Span:
        def __init__(self, name, kind, metadata):
            self.name, self.kind, self.metadata = name, kind, metadata
        def set_attributes(self, attributes):
            self.metadata.update(attributes)
    @contextmanager
    def trace(name, kind, metadata):
        span = Span(name, kind, metadata)
        spans.append(span)
        yield span
    monkeypatch.setattr(repository_module, "trace_step", trace)
    _, execution_id = queued(repo)
    arguments = {"private_input": "should stay in business store"}
    result = repo.tool_result(execution_id, "lookup", "official_lookup", arguments, lambda: {"private_output": True})
    assert repo.tool_result(execution_id, "lookup", "official_lookup", arguments, lambda: pytest.fail("cache missed")) == result
    repo.tool_result(execution_id, "model", "model_decide", {}, lambda: {})
    assert [span.kind for span in spans] == ["TOOL", "TOOL", "CHAIN"]
    assert [span.metadata["cache_hit"] for span in spans] == [False, True, False]
    assert "private_input" not in str([span.metadata for span in spans])
    assert "private_output" not in str([span.metadata for span in spans])


def test_langgraph_checkpoint_executor_inherits_lease_fence(monkeypatch, repo, tmp_path):
    _, execution_id = queued(repo)
    replacement_job = []
    def stale_graph(repository, _tools, saver, *, welfare_request=None):
        assert welfare_request is None
        def slow_node(_state):
            with repository.connection() as db:
                db.execute("UPDATE jobs SET lease_until=0 WHERE execution_id=?", (execution_id,))
            replacement_job.append(InvestigationRepository(repo.path).claim())
            # LangGraph persists node output in its background checkpoint executor.
            # That thread must retain the old job context and reject these bytes.
            return {"review": {"decision": "stale-must-not-persist"}}
        graph = StateGraph(State)
        graph.add_node("slow", slow_node)
        graph.add_edge(START, "slow")
        graph.add_edge("slow", END)
        return graph.compile(checkpointer=saver)
    monkeypatch.setattr(worker_module, "build_investigation_graph", stale_graph)
    worker = InvestigationWorker(repo, None, tmp_path / "checkpoint.sqlite3")
    try:
        assert worker.drain_once()
        assert worker.last_error_code == "job_lease_lost"
        config = {"configurable": {"thread_id": execution_id, "checkpoint_ns": ""}}
        checkpoints = list(worker.saver.list(config))
        assert checkpoints  # Initial valid checkpoints remain recoverable.
        assert all("stale-must-not-persist" not in str(checkpoint) for checkpoint in checkpoints)
        assert repo.execution(execution_id)["status"] == "running"
        assert repo.finish_job(replacement_job[0], "failed", {"code": "fixture_complete"})
    finally:
        worker.close()
