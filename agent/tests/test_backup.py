"""Local SQLite recovery evidence; no real model and no production RPO/RTO claim."""
import json
from pathlib import Path
import sqlite3
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from donghaeng_agent.backup import BackupError, create_backup, main, restore_backup
from donghaeng_agent.repository import InvestigationRepository
from donghaeng_agent.worker import InvestigationWorker
import donghaeng_agent.worker as worker_module


class State(TypedDict, total=False):
    case_id: str
    execution_id: str
    brief: dict
    review: dict


def recovery_graph(repository, _tools, saver, *, welfare_request=None):
    assert welfare_request is None
    def prepare(state):
        return {"brief": repository.put_brief(state["case_id"], state["execution_id"],
                {"measurement_status": "held", "support_status": "needs_confirmation"})}
    def review(state):
        return {"review": interrupt({"brief": state["brief"]})}
    def finalize(state):
        repository.apply_review(state["case_id"], state["execution_id"], state["review"])
        return {}
    graph = StateGraph(State)
    for name, callback in (("prepare", prepare), ("review", review), ("finalize", finalize)):
        graph.add_node(name, callback)
    for source, target in ((START, "prepare"), ("prepare", "review"), ("review", "finalize"), ("finalize", END)):
        graph.add_edge(source, target)
    return graph.compile(checkpointer=saver)


@pytest.fixture
def interrupted(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, "build_investigation_graph", recovery_graph)
    repository = InvestigationRepository(tmp_path / "business.sqlite3")
    case = repository.create({"idempotency_key": "recovery-test"})
    execution = repository.enqueue(case["case_id"], {"expected_case_version": 1, "idempotency_key": "run", "strategy": "rule_baseline"}, {})
    checkpoint = tmp_path / "checkpoints.sqlite3"
    worker = InvestigationWorker(repository, None, checkpoint)
    assert worker.drain_once()
    worker.close()
    assert repository.execution(execution["execution_id"])["status"] == "interrupted"
    return repository, checkpoint, case["case_id"]


def test_real_interrupt_backup_restore_review_resume(interrupted, tmp_path):
    repository, checkpoint, case_id = interrupted
    backup = create_backup(repository, checkpoint, tmp_path / "backup")
    assert backup["consistency"] == "business_write_fence_no_running_jobs"
    restored = restore_backup(tmp_path / "backup", tmp_path / "restored")
    recovered = InvestigationRepository(Path(restored["business_db"]))
    case = recovered.get(case_id)
    brief = case["briefs"][0]
    recovered.review(case_id, {"brief_id": brief["brief_id"], "brief_version": brief["brief_version"],
                              "expected_case_version": case["case_version"], "decision": "accept", "idempotency_key": "restored-review"})
    worker = InvestigationWorker(recovered, None, Path(restored["checkpoint_db"]))
    try:
        assert worker.drain_once()
        result = recovered.get(case_id)
        assert result["status"] == "reviewed"
        assert result["executions"][0]["status"] == "succeeded"
        assert len(result["briefs"]) == 1 and len(result["reviews"]) == 1
    finally:
        worker.close()
    assert repository.get(case_id)["status"] == "awaiting_review"


def test_backup_refuses_running_job_without_mutating_original(interrupted, tmp_path):
    repository, checkpoint, case_id = interrupted
    case = repository.get(case_id)
    brief = case["briefs"][0]
    repository.review(case_id, {"brief_id": brief["brief_id"], "brief_version": brief["brief_version"],
                              "expected_case_version": case["case_version"], "decision": "accept", "idempotency_key": "pending-review"})
    job = repository.claim()
    with pytest.raises(BackupError, match="running_jobs_must_stop"):
        create_backup(repository, checkpoint, tmp_path / "blocked")
    assert not (tmp_path / "blocked").exists()
    assert repository.execution(job["execution_id"])["status"] == "running"


def test_restore_rejects_corruption_and_never_overwrites(interrupted, tmp_path):
    repository, checkpoint, _ = interrupted
    create_backup(repository, checkpoint, tmp_path / "backup")
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    marker = occupied / "preserve"
    marker.write_text("existing")
    with pytest.raises(BackupError, match="destination_already_exists"):
        restore_backup(tmp_path / "backup", occupied)
    assert marker.read_text() == "existing"
    with (tmp_path / "backup/business.sqlite3").open("ab") as output:
        output.write(b"corruption")
    with pytest.raises(BackupError, match="backup_hash_mismatch"):
        restore_backup(tmp_path / "backup", tmp_path / "corrupt-restored")
    assert not (tmp_path / "corrupt-restored").exists()


def test_backup_cli_and_restore_cli(interrupted, tmp_path, capsys):
    repository, checkpoint, _ = interrupted
    main(["backup", "--business-db", str(repository.path), "--checkpoint-db", str(checkpoint), "--output", str(tmp_path / "backup")])
    assert json.loads(capsys.readouterr().out)["format"] == "hermes-sqlite-recovery-1.0"
    main(["restore", "--backup", str(tmp_path / "backup"), "--output", str(tmp_path / "restored")])
    assert Path(json.loads(capsys.readouterr().out)["business_db"]).is_file()


def test_backup_rejects_same_file_or_mismatched_checkpoint_pair(interrupted, tmp_path):
    repository, checkpoint, _ = interrupted
    with pytest.raises(BackupError, match="business_and_checkpoint_must_be_separate"):
        create_backup(repository, repository.path, tmp_path / "same")
    wrong = tmp_path / "wrong-checkpoints.sqlite3"
    with sqlite3.connect(wrong) as database:
        database.execute("CREATE TABLE checkpoints(thread_id TEXT)")
        database.execute("INSERT INTO checkpoints VALUES('unrelated_execution')")
    with pytest.raises(BackupError, match="checkpoint_pair_mismatch"):
        create_backup(repository, wrong, tmp_path / "mismatched")
    assert not (tmp_path / "mismatched").exists()


def test_restore_checks_pair_even_when_corrupt_pair_has_valid_hashes(interrupted, tmp_path):
    from donghaeng_agent.backup import _digest
    repository, checkpoint, _ = interrupted
    create_backup(repository, checkpoint, tmp_path / "backup")
    with sqlite3.connect(tmp_path / "backup/checkpoints.sqlite3") as database:
        database.execute("DELETE FROM checkpoints")
    manifest_path = tmp_path / "backup/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["checkpoints.sqlite3"] = _digest(tmp_path / "backup/checkpoints.sqlite3")
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(BackupError, match="checkpoint_pair_mismatch"):
        restore_backup(tmp_path / "backup", tmp_path / "bad-pair")


def test_backup_rejects_wrong_checkpoint_for_queued_review(interrupted, tmp_path):
    repository, checkpoint, case_id = interrupted
    case = repository.get(case_id)
    brief = case["briefs"][0]
    repository.review(case_id, {"brief_id": brief["brief_id"], "brief_version": brief["brief_version"],
                              "expected_case_version": case["case_version"], "decision": "request_info", "idempotency_key": "queued-review-pair"})
    assert repository.get(case_id)["executions"][0]["status"] == "queued"
    wrong = tmp_path / "wrong-queued-checkpoints.sqlite3"
    with sqlite3.connect(wrong) as database:
        database.execute("CREATE TABLE checkpoints(thread_id TEXT)")
        database.execute("INSERT INTO checkpoints VALUES('unrelated_execution')")
    with pytest.raises(BackupError, match="checkpoint_pair_mismatch"):
        create_backup(repository, wrong, tmp_path / "queued-wrong-pair")
    assert not (tmp_path / "queued-wrong-pair").exists()
    create_backup(repository, checkpoint, tmp_path / "queued-valid-pair")
    assert (tmp_path / "queued-valid-pair/manifest.json").is_file()
