"""
APScheduler entrypoint.

Jobs:
  portfolio_daily  — runs PortfolioGraph every day at settings.portfolio_cron_hour:minute
  feedback_sweep   — runs FeedbackGraph every settings.feedback_cron_every_hours hours
                     (feedback graph not yet wired; placeholder logged)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from portfolio_agent.deps import build_deps
from portfolio_agent.graphs.portfolio_graph import build_portfolio_graph

log = structlog.get_logger()


async def run_portfolio(graph, deps) -> None:
    run_id = str(uuid4())
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=deps.settings.portfolio_window_hours)
    state = {
        "run_id": run_id,
        "window_start": start.isoformat(),
        "window_end": now.isoformat(),
        "metrics": {},
    }
    log.info("portfolio.run.start", run_id=run_id, window_start=state["window_start"])
    try:
        final = await graph.ainvoke(
            state,
            config={"configurable": {"thread_id": run_id}},
        )
        log.info("portfolio.run.done", run_id=run_id, metrics=final.get("metrics"))
        if deps.settings.slack_webhook_url:
            _notify_slack(deps.settings.slack_webhook_url, _portfolio_summary(run_id, final))
    except Exception as exc:
        log.error("portfolio.run.failed", run_id=run_id, err=repr(exc))


def _portfolio_summary(run_id: str, final: dict) -> str:
    m = final.get("metrics", {})
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


def _notify_slack(webhook_url: str, text: str) -> None:
    import httpx
    try:
        httpx.post(webhook_url, json={"text": text}, timeout=5.0)
    except Exception:
        log.warning("slack.notify_failed")


async def main() -> None:
    deps = await build_deps()
    portfolio_graph = build_portfolio_graph(deps)

    scheduler = AsyncIOScheduler(timezone=deps.settings.schedule_timezone)
    scheduler.add_job(
        run_portfolio,
        "cron",
        hour=deps.settings.portfolio_cron_hour,
        minute=deps.settings.portfolio_cron_minute,
        id="portfolio_daily",
        misfire_grace_time=3600,
        coalesce=True,
        kwargs={"graph": portfolio_graph, "deps": deps},
    )
    scheduler.start()
    log.info(
        "scheduler.started",
        portfolio_cron=f"{deps.settings.portfolio_cron_hour:02d}:{deps.settings.portfolio_cron_minute:02d}",
        tz=deps.settings.schedule_timezone,
    )
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
