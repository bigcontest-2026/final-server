"""A real LangGraph workflow whose decisions are deterministic demo rules.

Extension points: demo_risk (trained model), investigate (approved evidence),
search_policies (verified policy provider), summarize (optional grounded LLM).
No extension is connected in this demonstration.
"""
from datetime import datetime, timedelta, timezone
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from .mock_contracts import Dataset, District, Policy, RiskLevel, RunRequest, RunResult, Scenario, Step

DISCLAIMER = "시연용 합성 데이터와 결정적 규칙의 결과입니다. 실제 진단·학습 모델·LLM 판단이 아닙니다."
MAX_SEARCH_ATTEMPTS = 2


class GraphState(TypedDict, total=False):
    dataset: Dataset
    district: District
    scenario: Scenario
    held: bool
    risk_score: float | None
    risk_level: RiskLevel
    steps: list[Step]
    warnings: list[str]
    candidates: list[Policy]
    policies: list[Policy]
    rejected_ids: list[str]
    search_attempts: int
    retry: bool
    result: RunResult


def step(state: GraphState, id: str, label: str, detail: str, status="completed"):
    return state["steps"] + [Step(id=id, label=label, status=status, detail=detail)]


def validate(state: GraphState):
    district = state["district"]
    held = (
        state["scenario"] == "missing_data" or district.quality != "valid"
        or district.risk_score is None or not district.signals
        or any(signal.value is None for signal in district.signals)
    )
    detail = "필수 지표가 누락되었거나 품질 확인이 필요하여 위험 계산과 정책 검색을 중단했습니다." if held else "필수 합성 지표와 데이터 품질을 확인했습니다."
    return {
        "held": held, "risk_score": None, "risk_level": "unknown",
        "steps": step(state, "validate", "데이터 품질 확인", detail, "held" if held else "completed"),
        "warnings": state["warnings"] + ([detail] if held else []),
    }


def demo_risk(state: GraphState):
    score = state["district"].risk_score
    assert score is not None
    if state["scenario"] == "risk_spike":
        score = min(99, score + 30)
    level: RiskLevel = "high" if score >= 70 else "watch" if score >= 40 else "low"
    return {
        "risk_score": score, "risk_level": level,
        "steps": step(state, "risk", "시연 위험 결과 확인", f"합성 지수 {score:g}/100, 등급 {level}. 확률이나 개인의 고립 진단이 아닙니다."),
    }


def investigate(state: GraphState):
    district = state["district"]
    changes = [f"{s.label} {s.change_pct:+g}%" for s in district.signals if s.change_pct is not None]
    detail = "제공된 합성 지표 검토: " + (", ".join(changes) or "변화율 자료 없음")
    if state["scenario"] == "risk_spike":
        detail += ". 급등 시나리오의 +30 지수 변화와 지표를 확인했습니다. 원인이나 인과관계는 확인하지 않았습니다."
    else:
        detail += ". 외부 조사나 개인별 추론을 수행하지 않았습니다."
    return {"steps": step(state, "investigate", "추가 조사 — 합성 지표 검토", detail)}


def region_matches(policy: Policy, district: District) -> bool:
    return bool({"*", district.code, district.name, district.district}.intersection(policy.districts))


def search_policies(state: GraphState):
    attempt = state["search_attempts"] + 1
    candidates = []
    if state["scenario"] != "no_policy":
        candidates = [p for p in state["dataset"].policies
                      if region_matches(p, state["district"]) and p.id not in state["rejected_ids"]]
        signal_keys = {s.key for s in state["district"].signals}
        candidates.sort(key=lambda p: (-len(signal_keys.intersection(p.tags)), p.id))
        if state["scenario"] == "expired_policy" and attempt == 1:
            # The first candidate is deliberately expired; retry uses unmodified shared policies.
            if candidates:
                as_of = state["dataset"].as_of.date()
                candidates = [candidates[0].model_copy(update={
                    "id": candidates[0].id + "-expired-simulation",
                    "title": candidates[0].title + " (만료 시연 후보)",
                    "start_date": as_of - timedelta(days=365),
                    "end_date": as_of - timedelta(days=1),
                })]
    return {
        "search_attempts": attempt, "candidates": candidates,
        "steps": step(state, "search" if attempt == 1 else "research", "가상 정책 검색" if attempt == 1 else "가상 정책 재검색",
                      f"{attempt}/{MAX_SEARCH_ATTEMPTS}회 검색: 지역 범위에 맞는 가상 후보 {len(candidates)}개. 공공 정책 API는 연결되지 않았습니다."),
    }


def check_conditions(state: GraphState):
    as_of = state["dataset"].as_of.date()
    accepted, rejected = [], []
    for policy in state["candidates"]:
        if policy.start_date <= as_of <= policy.end_date and region_matches(policy, state["district"]):
            accepted.append(policy)
        else:
            rejected.append(policy)
    retry = bool(rejected) and not accepted and state["search_attempts"] < MAX_SEARCH_ATTEMPTS
    warnings = state["warnings"] + [f"기간 또는 지역 조건 불충족으로 제외: {p.title}" for p in rejected]
    if not accepted and not retry:
        warnings.append("적용 가능한 가상 정책이 없습니다. 정책 공백에 대한 담당자 확인이 필요합니다.")
    return {
        "policies": accepted[:3], "retry": retry,
        "rejected_ids": state["rejected_ids"] + [p.id for p in rejected],
        "warnings": warnings,
        "steps": step(state, "eligibility" if state["search_attempts"] == 1 else "eligibility_retry", "가상 정책 조건 확인",
                      f"합성 자료 기준일 {as_of.isoformat()}의 기간·지역 조건 확인: 유효 {len(accepted)}개, 제외 {len(rejected)}개. 실제 개인 자격 심사가 아닙니다."),
    }


def summarize(state: GraphState):
    name = state["district"].name
    if state["held"]:
        summary = f"{name}: 자료 부족으로 판단을 보류했습니다. 누락 지표를 확인한 후 다시 실행하세요."
    elif not state["policies"]:
        summary = f"{name}: 시연 위험 지수 {state['risk_score']:g}/100. 가상 정책 공백으로 담당자 확인이 필요합니다."
    else:
        summary = f"{name}: 시연 위험 지수 {state['risk_score']:g}/100, 검토할 가상 정책 {len(state['policies'])}개. 실제 지원 가능 여부는 담당자 확인이 필요합니다."
    steps = step(state, "summary", "결과 정리", summary)
    result = RunResult(
        run_id=str(uuid4()), district_code=state["district"].code, scenario=state["scenario"],
        status="held" if state["held"] else "completed", risk_score=state["risk_score"],
        risk_level=state["risk_level"], summary=summary, policies=state["policies"],
        warnings=state["warnings"], steps=steps, completed_at=datetime.now(timezone.utc),
    )
    return {"steps": steps, "result": result}


def build_graph():
    builder = StateGraph(GraphState)
    for name, node in (("validate", validate), ("risk", demo_risk), ("investigate", investigate),
                       ("search", search_policies), ("conditions", check_conditions), ("summary", summarize)):
        builder.add_node(name, node)
    builder.add_edge(START, "validate")
    builder.add_conditional_edges("validate", lambda s: "held" if s["held"] else "ready",
                                  {"held": "summary", "ready": "risk"})
    builder.add_edge("risk", "investigate")
    builder.add_edge("investigate", "search")
    builder.add_edge("search", "conditions")
    builder.add_conditional_edges("conditions", lambda s: "retry" if s["retry"] else "done",
                                  {"retry": "search", "done": "summary"})
    builder.add_edge("summary", END)
    return builder.compile()


graph = build_graph()


def execute_run(dataset: Dataset, request: RunRequest) -> RunResult:
    district = next((d for d in dataset.districts if d.code == request.district_code), None)
    if district is None:
        raise KeyError(request.district_code)
    state = graph.invoke({
        "dataset": dataset, "district": district, "scenario": request.scenario,
        "steps": [], "warnings": [DISCLAIMER], "policies": [], "candidates": [],
        "search_attempts": 0, "rejected_ids": [], "retry": False,
    }, config={"recursion_limit": 16})
    return state["result"]
