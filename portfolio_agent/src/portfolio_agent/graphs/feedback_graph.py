"""
Feedback graph — no checkpointer needed.

The feedback pipeline is a linear, single-shot sweep: fetch → classify →
route → persist → update_rules. There is no long-running work to resume, so
checkpointing adds overhead and can cause stale state bleed between runs when
the Postgres saver restores channel values from prior threads. We compile
without a checkpointer.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from langgraph.graph import END, START, StateGraph

from portfolio_agent.agents.feedback import (
    classify_comments_node,
    persist_feedback_node,
    route_feedback_node,
    update_rules_config_node,
)
from portfolio_agent.deps import Deps
from portfolio_agent.gdocs.comments_reader import fetch_comments_node
from portfolio_agent.state import FeedbackState


def _curry(fn, deps: Deps):
    if asyncio.iscoroutinefunction(fn):
        async def _async_inner(state: FeedbackState) -> FeedbackState:
            return await fn(state, deps)
        _async_inner.__name__ = fn.__name__
        return _async_inner
    else:
        def _sync_inner(state: FeedbackState) -> FeedbackState:
            return fn(state, deps)
        _sync_inner.__name__ = fn.__name__
        return _sync_inner


def _build_graph(deps: Deps):
    g: StateGraph = StateGraph(FeedbackState)

    g.add_node("fetch_comments",      _curry(fetch_comments_node, deps))
    g.add_node("classify_comments",   _curry(classify_comments_node, deps))
    g.add_node("route_feedback",      _curry(route_feedback_node, deps))
    g.add_node("persist_feedback",    _curry(persist_feedback_node, deps))
    g.add_node("update_rules_config", _curry(update_rules_config_node, deps))

    g.add_edge(START, "fetch_comments")
    g.add_conditional_edges(
        "fetch_comments",
        lambda s: "classify_comments" if s.get("fetched_comments") else END,
        {"classify_comments": "classify_comments", END: END},
    )
    g.add_edge("classify_comments",   "route_feedback")
    g.add_edge("route_feedback",      "persist_feedback")
    g.add_edge("persist_feedback",    "update_rules_config")
    g.add_edge("update_rules_config", END)

    return g.compile()   # no checkpointer


@asynccontextmanager
async def build_feedback_graph(deps: Deps) -> AsyncIterator:
    """Yield a compiled feedback graph (no checkpointer — single-shot pipeline)."""
    yield _build_graph(deps)
