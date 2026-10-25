"""Shared recommendation subgraph for dashboard and investigation execution."""
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from .welfare_pipeline import run


class WelfareState(TypedDict, total=False):
    request: dict
    result: dict


def build_welfare_graph(runner=run):
    graph=StateGraph(WelfareState)
    graph.add_node('recommend',lambda state: {'result':runner(**state['request'])})
    def finalize(state):
        result=state['result']
        if result.get('operational_alert_allowed') is not False:
            raise ValueError('research result must not enable alerts')
        return {'result':{**result,'execution_graph':'welfare-research-v1'}}
    graph.add_node('verify_research_output',finalize)
    graph.add_edge(START,'recommend')
    graph.add_edge('recommend','verify_research_output')
    graph.add_edge('verify_research_output',END)
    return graph.compile()
