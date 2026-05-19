"""
FastAPI trigger API.

Exposes three endpoints so any external system can fire pipelines on demand:

    POST /trigger/portfolio   — run PortfolioGraph (optional time window in body)
    POST /trigger/feedback    — run FeedbackGraph  (optional since timestamp in body)
    GET  /health              — liveness probe

Authentication: X-API-Key header checked against PA_API_KEY env var.
If PA_API_KEY is empty the server starts without auth and logs a loud warning
(useful for local dev; always set it in production).

The pipeline runs are launched as BackgroundTasks so the HTTP response is
returned immediately with a run_id — runs can take 1-2 minutes.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# App state — injected by scheduler.main() before uvicorn starts
# ---------------------------------------------------------------------------

class _State:
    deps: Any = None
    portfolio_graph: Any = None
    feedback_graph: Any = None

_state = _State()


def set_app_state(deps: Any, portfolio_graph: Any, feedback_graph: Any) -> None:
    """Called once from scheduler.main() after graphs are initialised."""
    _state.deps = deps
    _state.portfolio_graph = portfolio_graph
    _state.feedback_graph = feedback_graph


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

api = FastAPI(
    title="Portfolio Agent API",
    description="Trigger portfolio/feedback pipeline runs from external systems.",
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# API key auth (middleware-style dependency)
# ---------------------------------------------------------------------------

async def _require_api_key(request: Request) -> None:
    """Reject requests with a wrong or missing X-API-Key header."""
    expected = (_state.deps.settings.api_key or "").strip() if _state.deps else ""
    if not expected:
        # No key configured → open access with a warning (dev mode).
        return
    provided = request.headers.get("X-API-Key", "")
    if provided != expected:
        raise HTTPException(status_code=403, detail="Invalid or missing X-API-Key")


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class PortfolioTriggerBody(BaseModel):
    """Optional explicit window; defaults to the last portfolio_window_hours."""
    start: str | None = None   # ISO8601 UTC  e.g. "2026-05-19T00:00:00Z"
    end:   str | None = None


class FeedbackTriggerBody(BaseModel):
    """Optional since timestamp; defaults to feedback_cron_every_hours ago."""
    since: str | None = None   # ISO8601 UTC


class TriggerResponse(BaseModel):
    run_id: str
    status: str = "queued"
    message: str


# ---------------------------------------------------------------------------
# Background job helpers (mirror scheduler.py logic)
# ---------------------------------------------------------------------------

async def _run_portfolio(run_id: str, start: str, end: str) -> None:
    deps  = _state.deps
    graph = _state.portfolio_graph
    state = {
        "run_id":       run_id,
        "window_start": start,
        "window_end":   end,
        "metrics":      {},
    }
    log.info("api.portfolio.start", run_id=run_id, window_start=start, window_end=end)
    try:
        final = await graph.ainvoke(
            state, config={"configurable": {"thread_id": run_id}}
        )
        log.info("api.portfolio.done", run_id=run_id, metrics=final.get("metrics"))
    except Exception as exc:
        log.error("api.portfolio.failed", run_id=run_id, err=repr(exc))


async def _run_feedback(run_id: str, since: str) -> None:
    deps  = _state.deps
    graph = _state.feedback_graph
    state = {
        "run_id":  run_id,
        "since":   since,
        "metrics": {},
    }
    log.info("api.feedback.start", run_id=run_id, since=since)
    try:
        final = await graph.ainvoke(
            state, config={"configurable": {"thread_id": run_id}}
        )
        log.info("api.feedback.done", run_id=run_id, metrics=final.get("metrics"))
    except Exception as exc:
        log.error("api.feedback.failed", run_id=run_id, err=repr(exc))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@api.get("/health", tags=["ops"])
async def health() -> dict:
    """Liveness probe — returns 200 if the server is up."""
    return {
        "status": "ok",
        "graphs_ready": _state.portfolio_graph is not None,
    }


@api.post(
    "/trigger/portfolio",
    response_model=TriggerResponse,
    tags=["triggers"],
    dependencies=[Depends(_require_api_key)],
)
async def trigger_portfolio(
    body: PortfolioTriggerBody = None,
) -> TriggerResponse:
    """
    Fire the portfolio pipeline in the background and return immediately.

    Omit the body (or pass null start/end) to use the default window
    (last `portfolio_window_hours` hours as configured in settings).
    """
    if _state.portfolio_graph is None:
        raise HTTPException(status_code=503, detail="Graphs not initialised yet — try again shortly")

    now   = datetime.now(timezone.utc)
    wh    = _state.deps.settings.portfolio_window_hours
    start = (body.start if body and body.start else None) or (now - timedelta(hours=wh)).isoformat()
    end   = (body.end   if body and body.end   else None) or now.isoformat()

    run_id = str(uuid4())
    asyncio.create_task(_run_portfolio(run_id, start, end))

    return TriggerResponse(
        run_id=run_id,
        status="queued",
        message=f"Portfolio pipeline queued for window {start} → {end}",
    )


@api.post(
    "/trigger/feedback",
    response_model=TriggerResponse,
    tags=["triggers"],
    dependencies=[Depends(_require_api_key)],
)
async def trigger_feedback(
    body: FeedbackTriggerBody = None,
) -> TriggerResponse:
    """
    Fire the feedback pipeline in the background and return immediately.

    Omit the body (or pass null since) to use the default lookback window
    (feedback_cron_every_hours + 1 hours ago).
    """
    if _state.feedback_graph is None:
        raise HTTPException(status_code=503, detail="Graphs not initialised yet — try again shortly")

    now   = datetime.now(timezone.utc)
    every = _state.deps.settings.feedback_cron_every_hours
    since = (body.since if body and body.since else None) or (now - timedelta(hours=every + 1)).isoformat()

    run_id = str(uuid4())
    asyncio.create_task(_run_feedback(run_id, since))

    return TriggerResponse(
        run_id=run_id,
        status="queued",
        message=f"Feedback pipeline queued (comments since {since})",
    )


# ---------------------------------------------------------------------------
# Startup / shutdown events — log noisy warning if running without auth
# ---------------------------------------------------------------------------

@api.on_event("startup")
async def _on_startup() -> None:
    if _state.deps and not (_state.deps.settings.api_key or "").strip():
        log.warning(
            "api.no_auth",
            msg="PA_API_KEY is not set — API is running WITHOUT authentication. "
                "Set PA_API_KEY in your .env before deploying.",
        )
    log.info("api.ready", msg="Trigger API is accepting requests")
