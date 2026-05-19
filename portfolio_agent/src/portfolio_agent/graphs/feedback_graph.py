from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
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


def _compiled_graph(deps: Deps, checkpointer=None):
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

    return g.compile(checkpointer=checkpointer)


@asynccontextmanager
async def build_feedback_graph(deps: Deps) -> AsyncIterator:
    """
    Async context manager that owns the Postgres checkpointer lifetime.

    Usage:
        async with build_feedback_graph(deps) as graph:
            await graph.ainvoke(state, ...)
    """
    conn_str = (
        deps.settings.operational_db_url
        .replace("postgresql+psycopg://", "postgresql://")
        .replace("postgresql+psycopg2://", "postgresql://")
    )
    async with AsyncPostgresSaver.from_conn_string(conn_str) as checkpointer:
        await checkpointer.setup()
        yield _compiled_graph(deps, checkpointer=checkpointer)
