"""Evidence exports preserve SQL ownership even when payload fields disagree."""
import json

from donghaeng_agent.repository import InvestigationRepository


def test_exported_evidence_ownership_comes_from_sql_and_payload_is_unchanged(tmp_path):
    repo = InvestigationRepository(tmp_path / "business.sqlite3")
    case = repo.create({"idempotency_key": "case"})
    run = repo.enqueue(case["case_id"], {"expected_case_version": case["case_version"],
        "idempotency_key": "run", "strategy": "rule_baseline"}, {})
    payload = {"evidence_id": "evidence_row", "source_id": "synthetic_source", "version_hash": "fixture",
        "case_id": "forged_case", "execution_id": "forged_execution", "extracted_values": {"count": 3}}
    repo.put_evidence(case["case_id"], run["execution_id"], [payload])
    # Also verify that even an inconsistent serialized identity cannot replace
    # the primary key used by the saved ownership relationship.
    with repo.connection(write=True) as db:
        stored = {**payload, "evidence_id": "forged_evidence"}
        db.execute("UPDATE evidence SET data=? WHERE evidence_id=?", (json.dumps(stored), "evidence_row"))
    exported = repo.get(case["case_id"])["evidence"][0]
    assert exported["evidence_id"] == "evidence_row"
    assert exported["case_id"] == case["case_id"]
    assert exported["execution_id"] == run["execution_id"]
    assert exported["extracted_values"] == {"count": 3}
    with repo.connection() as db:
        assert json.loads(db.execute("SELECT data FROM evidence WHERE evidence_id='evidence_row'").fetchone()[0]) == stored


def test_case_export_does_not_include_another_cases_evidence(tmp_path):
    repo = InvestigationRepository(tmp_path / "business.sqlite3")
    cases = []
    for name in ("one", "two"):
        case = repo.create({"idempotency_key": name})
        run = repo.enqueue(case["case_id"], {"expected_case_version": case["case_version"],
            "idempotency_key": "run", "strategy": "rule_baseline"}, {})
        repo.put_evidence(case["case_id"], run["execution_id"], [{"evidence_id": name,
            "source_id": "synthetic_source", "version_hash": name}])
        cases.append(case)
    assert [e["evidence_id"] for e in repo.get(cases[0]["case_id"])["evidence"]] == ["one"]
    assert [e["evidence_id"] for e in repo.get(cases[1]["case_id"])["evidence"]] == ["two"]
