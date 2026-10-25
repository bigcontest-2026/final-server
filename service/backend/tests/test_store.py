import pytest

pytest.importorskip("donghaeng_agent", reason="optional legacy agent tests: install requirements-legacy.txt")
pytest.importorskip("donghaeng_model", reason="optional legacy model_mori snapshot tests")

from donghaeng_agent.mock_graph import execute_run
from donghaeng_agent.mock_contracts import RunRequest
from app.store import RunStore


def test_ttl_expires_and_returned_values_cannot_mutate_history(dataset):
    now = [100.0]
    store = RunStore(2, 10, clock=lambda: now[0])
    result = execute_run(dataset, RunRequest(district_code="demo-001"))
    store.put(result)
    result.warnings.append("mutation")
    fetched = store.get(result.run_id)
    assert "mutation" not in fetched.warnings
    fetched.warnings.append("another mutation")
    assert "another mutation" not in store.get(result.run_id).warnings
    now[0] = 110.0
    assert store.get(result.run_id) is None
