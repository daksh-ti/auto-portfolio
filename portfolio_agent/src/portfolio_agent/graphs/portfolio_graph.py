from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph

from portfolio_agent.agents.analyzer import analyze_node, threshold_gate_node
from portfolio_agent.agents.extractor import extract_node, filter_users_node
from portfolio_agent.agents.generator import generate_entries_node
from portfolio_agent.agents.preprocessor import preprocess_node
from portfolio_agent.deps import Deps
from portfolio_agent.gdocs.portfolio_writer import write_to_gdoc_node
from portfolio_agent.state import PortfolioState


def _curry(fn, deps: Deps):
    """Bind deps into a node function, preserving sync/async."""
    if asyncio.iscoroutinefunction(fn):
        async def _async_inner(state: PortfolioState) -> PortfolioState:
            return await fn(state, deps)
        _async_inner.__name__ = fn.__name__
        return _async_inner
    else:
        def _sync_inner(state: PortfolioState) -> PortfolioState:
            return fn(state, deps)
        _sync_inner.__name__ = fn.__name__
        return _sync_inner


def _compiled_graph(deps: Deps, checkpointer=None):
    """Build and compile the StateGraph. Checkpointer is optional."""
    g: StateGraph = StateGraph(PortfolioState)

    g.add_node("extract",          _curry(extract_node, deps))
    g.add_node("filter_users",     _curry(filter_users_node, deps))
    g.add_node("preprocess",       _curry(preprocess_node, deps))
    g.add_node("analyze",          _curry(analyze_node, deps))
    g.add_node("threshold_gate",   _curry(threshold_gate_node, deps))
    g.add_node("generate_entries", _curry(generate_entries_node, deps))
    g.add_node("write_to_gdoc",    _curry(write_to_gdoc_node, deps))

    g.add_edge(START,            "extract")
    g.add_edge("extract",        "filter_users")
    g.add_edge("filter_users",   "preprocess")
    g.add_edge("preprocess",     "analyze")
    g.add_edge("analyze",        "threshold_gate")
    g.add_conditional_edges(
        "threshold_gate",
        lambda s: "generate_entries" if s.get("qualifying_chats") else END,
        {"generate_entries": "generate_entries", END: END},
    )
    g.add_edge("generate_entries", "write_to_gdoc")
    g.add_edge("write_to_gdoc",    END)

    return g.compile(checkpointer=checkpointer)


@asynccontextmanager
async def build_portfolio_graph(deps: Deps) -> AsyncIterator:
    """
    Async context manager that owns the Postgres checkpointer lifetime.

    Usage:
        async with build_portfolio_graph(deps) as graph:
            await graph.ainvoke(state, ...)
    """
    # AsyncPostgresSaver expects a plain psycopg DSN (no driver prefix).
    conn_str = (
        deps.settings.operational_db_url
        .replace("postgresql+psycopg://", "postgresql://")
        .replace("postgresql+psycopg2://", "postgresql://")
    )
    async with AsyncPostgresSaver.from_conn_string(conn_str) as checkpointer:
        await checkpointer.setup()   # creates alembic_version + checkpoint tables
        yield _compiled_graph(deps, checkpointer=checkpointer)
