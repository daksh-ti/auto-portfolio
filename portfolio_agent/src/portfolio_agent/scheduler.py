"""
APScheduler entrypoint.

Jobs:
  portfolio_daily  — runs PortfolioGraph every day at settings.portfolio_cron_hour:minute
  feedback_sweep   — runs FeedbackGraph every settings.feedback_cron_every_hours hours

Both graphs own their Postgres checkpointer via an async context manager.  The
scheduler keeps a single graph instance alive for the process lifetime, so we
enter the context manager in main() and never exit (the process runs forever).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from portfolio_agent.deps import build_deps
from portfolio_agent.graphs.feedback_graph import build_feedback_graph
from portfolio_agent.graphs.portfolio_graph import build_portfolio_graph

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# DB warmup — pings the DB before each job so Neon cold-starts are absorbed
# here rather than mid-pipeline.
# ---------------------------------------------------------------------------

async def _wait_for_db(engine, run_id: str, retries: int = 5) -> None:
    """
    Fire a lightweight SELECT 1 to wake Neon before the pipeline touches any
    real tables.  Retries with linear back-off (2 s, 4 s, 6 s …) so a
    cold-start that takes ~10 s is handled transparently.
    """
    for attempt in range(1, retries + 1):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            if attempt > 1:
                log.info("db.warmup.ok", run_id=run_id, attempt=attempt)
            return
        except OperationalError as exc:
            if attempt == retries:
                log.error("db.warmup.failed", run_id=run_id, err=repr(exc))
                raise
            wait = attempt * 2.0
            log.warning("db.warmup.retry", run_id=run_id, attempt=attempt, wait_s=wait, err=repr(exc))
            await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# Retry wrapper for ainvoke — catches transient OperationalErrors mid-run
# ---------------------------------------------------------------------------

def _make_retry_invoke(graph):
    """Return a tenacity-wrapped coroutine that calls graph.ainvoke."""

    @retry(
        retry=retry_if_exception_type(OperationalError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=3, max=15),
        reraise=True,
    )
    async def _invoke(state, **kwargs):
        return await graph.ainvoke(state, **kwargs)

    return _invoke


# ---------------------------------------------------------------------------
# Job functions (receive compiled graph instances)
# ---------------------------------------------------------------------------

async def run_portfolio(graph, deps) -> None:
    run_id = str(uuid4())
    now    = datetime.now(timezone.utc)
    start  = now - timedelta(hours=deps.settings.portfolio_window_hours)
    state  = {
        "run_id":       run_id,
        "window_start": start.isoformat(),
        "window_end":   now.isoformat(),
        "metrics":      {},
    }
    log.info("portfolio.run.start", run_id=run_id, window_start=state["window_start"])
    try:
        await _wait_for_db(deps.ops_repo._engine, run_id)
        invoke = _make_retry_invoke(graph)
        final  = await invoke(state, config={"configurable": {"thread_id": run_id}})
        log.info("portfolio.run.done", run_id=run_id, metrics=final.get("metrics"))
        if deps.settings.slack_webhook_url:
            _notify_slack(deps.settings.slack_webhook_url, _portfolio_summary(run_id, final))
    except Exception as exc:
        log.error("portfolio.run.failed", run_id=run_id, err=repr(exc))


async def run_feedback(graph, deps) -> None:
    run_id = str(uuid4())
    since  = datetime.now(timezone.utc) - timedelta(
        hours=deps.settings.feedback_cron_every_hours + 1
    )
    state = {
        "run_id":  run_id,
        "since":   since.isoformat(),
        "metrics": {},
    }
    log.info("feedback.run.start", run_id=run_id, since=state["since"])
    try:
        await _wait_for_db(deps.ops_repo._engine, run_id)
        invoke = _make_retry_invoke(graph)
        final  = await invoke(state)
        log.info("feedback.run.done", run_id=run_id, metrics=final.get("metrics"))
        if deps.settings.slack_webhook_url:
            _notify_slack(deps.settings.slack_webhook_url, _feedback_summary(run_id, final))
    except Exception as exc:
        log.error("feedback.run.failed", run_id=run_id, err=repr(exc))


# ---------------------------------------------------------------------------
# Summaries / helpers
# ---------------------------------------------------------------------------

def _portfolio_summary(run_id: str, final: dict) -> str:
    m      = final.get("metrics", {})
    errors = final.get("errors", [])
    return (
        f"Portfolio run {run_id[:8]} complete.\n"
        f"• Extracted: {m.get('extracted_count', '?')}"
        f"  • After user filter: {m.get('after_user_filter', '?')}\n"
        f"• After preprocess: {m.get('after_preprocess', '?')}"
        f"  • Analyzed: {m.get('analyzed_count', '?')}\n"
        f"• Qualifying (≥{m.get('threshold', '?')}): {m.get('qualifying_count', '?')}"
        f"  • Written: {m.get('written_count', '?')}\n"
        f"• Errors: {len(errors)}"
    )


def _feedback_summary(run_id: str, final: dict) -> str:
    m      = final.get("metrics", {})
    errors = final.get("errors", [])
    return (
        f"Feedback run {run_id[:8]} complete.\n"
        f"• Fetched: {m.get('fetched_count', '?')}"
        f"  • Classified: {m.get('classified_count', '?')}"
        f"  • Persisted: {m.get('persisted_count', '?')}\n"
        f"• Rules updated: {final.get('rules_updated', False)}"
        f"  New version: {final.get('new_rules_version', '—')}\n"
        f"• Errors: {len(errors)}"
    )


def _notify_slack(webhook_url: str, text: str) -> None:
    import httpx
    try:
        httpx.post(webhook_url, json={"text": text}, timeout=5.0)
    except Exception:
        log.warning("slack.notify_failed")


# ---------------------------------------------------------------------------
# Main scheduler loop
# ---------------------------------------------------------------------------

async def main() -> None:
    deps = await build_deps()

    async with build_portfolio_graph(deps) as portfolio_graph, \
               build_feedback_graph(deps)  as feedback_graph:

        # Wire graph instances into the trigger API.
        from portfolio_agent.api import api, set_app_state
        set_app_state(deps, portfolio_graph, feedback_graph)

        # APScheduler — cron jobs.
        scheduler = AsyncIOScheduler(timezone=deps.settings.schedule_timezone)

        scheduler.add_job(
            run_portfolio, "cron",
            hour=deps.settings.portfolio_cron_hour,
            minute=deps.settings.portfolio_cron_minute,
            id="portfolio_daily",
            misfire_grace_time=3600,
            coalesce=True,
            kwargs={"graph": portfolio_graph, "deps": deps},
        )
        scheduler.add_job(
            run_feedback, "cron",
            hour=f"*/{deps.settings.feedback_cron_every_hours}",
            id="feedback_sweep",
            misfire_grace_time=1800,
            coalesce=True,
            kwargs={"graph": feedback_graph, "deps": deps},
        )

        scheduler.start()
        log.info(
            "scheduler.started",
            portfolio_cron=(
                f"{deps.settings.portfolio_cron_hour:02d}:"
                f"{deps.settings.portfolio_cron_minute:02d}"
            ),
            feedback_every_h=deps.settings.feedback_cron_every_hours,
            tz=deps.settings.schedule_timezone,
        )

        # Uvicorn — serve the trigger API on the same event loop.
        import uvicorn
        uv_config = uvicorn.Config(
            api,
            host="0.0.0.0",
            port=deps.settings.api_port,
            loop="none",          # use the already-running asyncio loop
            log_level="warning",  # structlog handles app-level logging
        )
        server = uvicorn.Server(uv_config)
        log.info("api.server.starting", port=deps.settings.api_port)

        # server.serve() keeps the loop alive; scheduler runs inside the same loop.
        await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
