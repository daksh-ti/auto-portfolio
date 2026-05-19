from __future__ import annotations

import structlog
from datetime import datetime, timezone


def _parse_dt(s: str) -> datetime:
    """Parse ISO8601 string, handling trailing 'Z' for Python 3.10 compat."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

from portfolio_agent.deps import Deps
from portfolio_agent.state import PortfolioState

log = structlog.get_logger()


async def extract_node(state: PortfolioState, deps: Deps) -> PortfolioState:
    start = _parse_dt(state["window_start"])
    end   = _parse_dt(state["window_end"])
    chats = await deps.chat_repo.fetch_chats(
        start=start,
        end=end,
        limit=deps.settings.max_chats_per_run,
    )
    log.info("extract.done", run_id=state["run_id"], count=len(chats))
    return {
        "raw_chats": chats,
        "metrics": {**state.get("metrics", {}), "extracted_count": len(chats)},
    }


async def filter_users_node(state: PortfolioState, deps: Deps) -> PortfolioState:
    active = await deps.users_repo.fetch_active()
    active_ids = {u["user_id"] for u in active}
    kept = [c for c in state["raw_chats"] if c["user_id"] in active_ids]
    dropped = len(state["raw_chats"]) - len(kept)
    log.info(
        "filter_users.done",
        run_id=state["run_id"],
        active=len(active_ids),
        kept=len(kept),
        dropped=dropped,
    )
    return {
        "active_users": active,
        "raw_chats": kept,
        "metrics": {**state.get("metrics", {}), "after_user_filter": len(kept)},
    }
