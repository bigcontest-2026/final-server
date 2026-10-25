from copy import deepcopy

import pytest
from langgraph.graph.state import CompiledStateGraph

from donghaeng_agent.mock_graph import execute_run, graph
from donghaeng_agent.mock_contracts import Dataset, RunRequest


def run(dataset, scenario="normal"):
    return execute_run(dataset, RunRequest(district_code="demo-001", scenario=scenario))


def test_real_graph_and_normal_workflow(dataset):
    assert isinstance(graph, CompiledStateGraph)
    result = run(dataset)
    assert (result.status, result.risk_score, result.risk_level) == ("completed", 55, "watch")
    assert [s.id for s in result.steps] == ["validate", "risk", "investigate", "search", "eligibility", "summary"]
    assert result.policies[0].id == "demo-policy-1"
    assert "LLM" in result.warnings[0]


@pytest.mark.parametrize(("score", "expected", "level"), [(10, 40, "watch"), (55, 85, "high"), (95, 99, "high")])
def test_spike_boundaries(dataset, score, expected, level):
    dataset.districts[0].risk_score = score
    result = run(dataset, "risk_spike")
    assert (result.risk_score, result.risk_level) == (expected, level)
    assert "급등" in next(s.detail for s in result.steps if s.id == "investigate")


@pytest.mark.parametrize("score,level", [(0, "low"), (39, "low"), (40, "watch"), (69, "watch"), (70, "high"), (100, "high")])
def test_risk_thresholds(dataset, score, level):
    dataset.districts[0].risk_score = score
    assert run(dataset).risk_level == level


def test_missing_data_never_reaches_risk_or_search(dataset):
    result = run(dataset, "missing_data")
    assert (result.status, result.risk_score, result.risk_level) == ("held", None, "unknown")
    assert result.policies == []
    assert [s.id for s in result.steps] == ["validate", "summary"]
    assert any("누락" in w for w in result.warnings)


@pytest.mark.parametrize("condition", ["quality", "score", "signals", "value"])
def test_actual_missing_data_also_holds(dataset, condition):
    if condition == "quality":
        dataset.districts[0].quality = "missing"
    elif condition == "score":
        dataset.districts[0].risk_score = None
    elif condition == "signals":
        dataset.districts[0].signals = []
    else:
        dataset.districts[0].signals[0].value = None
    result = run(dataset)
    assert result.status == "held"
    assert [s.id for s in result.steps] == ["validate", "summary"]


def test_expired_candidate_is_rejected_and_researched(dataset):
    original = dataset.model_dump()
    result = run(dataset, "expired_policy")
    assert result.policies[0].id == "demo-policy-1"
    assert "research" in [s.id for s in result.steps]
    assert "eligibility_retry" in [s.id for s in result.steps]
    assert any("만료 시연 후보" in w for w in result.warnings)
    assert dataset.model_dump() == original


def test_no_policy_finishes_once(dataset):
    result = run(dataset, "no_policy")
    assert result.policies == []
    assert "담당자 확인" in result.summary
    assert [s.id for s in result.steps].count("search") == 1
    assert "research" not in [s.id for s in result.steps]


def test_all_expired_candidates_terminate_after_one_retry(payload):
    payload["policies"][0]["end_date"] = "2026-01-31"
    result = run(Dataset.model_validate(payload))
    assert not result.policies
    assert len(result.steps) == 8
    assert [s.id for s in result.steps].count("research") == 1


def test_eligibility_excludes_wrong_region_future_and_expired(payload):
    original = payload["policies"][0]
    for id, changes in [("wrong", {"districts": ["다른구"]}),
                        ("expired", {"end_date": "2026-09-28"}),
                        ("future", {"start_date": "2026-09-30"})]:
        policy = deepcopy(original)
        policy.update(id=id, **changes)
        payload["policies"].append(policy)
    result = run(Dataset.model_validate(payload))
    assert [p.id for p in result.policies] == ["demo-policy-1"]


def test_policy_dates_are_inclusive_at_dataset_date(payload):
    payload["policies"][0].update(start_date="2026-09-29", end_date="2026-09-29")
    assert len(run(Dataset.model_validate(payload)).policies) == 1


def test_at_most_three_policies(payload):
    for n in range(5):
        payload["policies"].append({**payload["policies"][0], "id": f"additional-{n}"})
    assert len(run(Dataset.model_validate(payload)).policies) == 3


@pytest.mark.parametrize("scenario", ["normal", "risk_spike", "missing_data", "expired_policy", "no_policy"])
def test_repeatable_results_without_dataset_mutation(dataset, scenario):
    before = dataset.model_dump()
    first, second = run(dataset, scenario), run(dataset, scenario)
    assert first.model_dump(exclude={"run_id", "completed_at"}) == second.model_dump(exclude={"run_id", "completed_at"})
    assert before == dataset.model_dump()
