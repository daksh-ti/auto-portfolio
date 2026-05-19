"""
CLI entry-point.

Commands:
  run-portfolio   --start ISO --end ISO     run the portfolio pipeline over a window
  run-portfolio-now                         run over the last portfolio_window_hours
  dry-run         --chat-id ID              extract + analyze one chat; print report
  validate-rules                            parse rules_config.yaml and exit 0/1
  show-config                               print redacted settings
  approve-pending --id N --as EMAIL         approve a pending rule-weight change
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import structlog
import typer

app = typer.Typer(add_completion=False)
log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _build():
    from portfolio_agent.deps import build_deps
    return await build_deps()


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.command("run-portfolio")
def run_portfolio(
    start: str = typer.Option(..., help="Window start (ISO8601 UTC)"),
    end:   str = typer.Option(..., help="Window end   (ISO8601 UTC)"),
):
    """Run the portfolio pipeline over an explicit time window."""
    async def _inner():
        from portfolio_agent.graphs.portfolio_graph import build_portfolio_graph
        deps = await _build()
        run_id = str(uuid4())
        state = {
            "run_id": run_id,
            "window_start": start,
            "window_end": end,
            "metrics": {},
        }
        async with build_portfolio_graph(deps) as graph:
            final = await graph.ainvoke(
                state, config={"configurable": {"thread_id": run_id}}
            )
        typer.echo(json.dumps({"run_id": run_id, "metrics": final.get("metrics"), "errors": final.get("errors", [])}, indent=2))
        if final.get("errors"):
            raise SystemExit(1)

    _run(_inner())


@app.command("run-portfolio-now")
def run_portfolio_now():
    """Run the portfolio pipeline over the last portfolio_window_hours."""
    async def _inner():
        from portfolio_agent.graphs.portfolio_graph import build_portfolio_graph
        deps = await _build()
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=deps.settings.portfolio_window_hours)
        run_id = str(uuid4())
        state = {
            "run_id": run_id,
            "window_start": start.isoformat(),
            "window_end": now.isoformat(),
            "metrics": {},
        }
        async with build_portfolio_graph(deps) as graph:
            final = await graph.ainvoke(
                state, config={"configurable": {"thread_id": run_id}}
            )
        typer.echo(json.dumps({"run_id": run_id, "metrics": final.get("metrics"), "errors": final.get("errors", [])}, indent=2))
        if final.get("errors"):
            raise SystemExit(1)

    _run(_inner())


@app.command("run-feedback")
def run_feedback(
    since: str = typer.Option(
        ...,
        help="Fetch comments created after this ISO8601 UTC timestamp",
    ),
):
    """Run the feedback pipeline: fetch comments → classify → persist → update rules."""
    async def _inner():
        from portfolio_agent.graphs.feedback_graph import build_feedback_graph
        deps   = await _build()
        run_id = str(uuid4())
        state  = {
            "run_id":  run_id,
            "since":   since,
            "metrics": {},
        }
        async with build_feedback_graph(deps) as graph:
            final = await graph.ainvoke(state)
        typer.echo(json.dumps(
            {
                "run_id":           run_id,
                "metrics":          final.get("metrics"),
                "persisted":        final.get("persisted_to_db", []),
                "rules_updated":    final.get("rules_updated", False),
                "new_rules_version": final.get("new_rules_version"),
                "errors":           final.get("errors", []),
            },
            indent=2,
        ))
        if final.get("errors"):
            raise SystemExit(1)

    _run(_inner())


@app.command("dry-run")
def dry_run(
    chat_id: str = typer.Option(..., help="chat_id to analyze"),
):
    """
    Extract + preprocess + analyze a single chat from the DB and print a report.
    Does NOT write to Google Doc or the operational DB.
    """
    async def _inner():
        from portfolio_agent.agents.analyzer import _analyze_one, render_chat_text
        from portfolio_agent.agents.preprocessor import _preprocess_one
        from portfolio_agent.rules import load_rules
        deps = await _build()
        rc = load_rules(deps.settings.rules_config_path)

        # Pull the single chat from DB.
        now = datetime.now(timezone.utc)
        all_chats = await deps.chat_repo.fetch_chats(
            start=now - timedelta(days=365), end=now, limit=50000
        )
        matching = [c for c in all_chats if c["chat_id"] == chat_id]
        if not matching:
            typer.echo(f"chat_id {chat_id!r} not found in the last 365 days", err=True)
            raise SystemExit(1)

        chat = matching[0]
        preprocessed = await _preprocess_one(chat, rc.preprocess, deps)
        if preprocessed is None:
            typer.echo("Chat was filtered out during preprocessing.")
            raise SystemExit(0)

        class _FakeOps:
            async def recent_analyzer_feedback(self, limit=5): return []
            async def record_analysis(self, **_): pass

        deps.ops_repo = _FakeOps()  # type: ignore[assignment]
        analyzed = await _analyze_one(preprocessed, rc, [], "dry-run", deps)

        report = {
            "chat_id": chat_id,
            "overall_score": analyzed.overall_score,
            "analysis_summary": analyzed.analysis_summary,
            "rule_scores": [r.model_dump() for r in analyzed.rule_scores],
            "would_qualify": analyzed.overall_score >= rc.threshold,
            "threshold": rc.threshold,
        }
        typer.echo(json.dumps(report, indent=2, default=str))

    _run(_inner())


@app.command("validate-rules")
def validate_rules(
    path: Path = typer.Option(None, help="Path to rules_config.yaml"),
):
    """Parse and validate rules_config.yaml; exit 0 on success."""
    from portfolio_agent.rules import load_rules
    from portfolio_agent.settings import get_settings
    cfg_path = path or get_settings().rules_config_path
    try:
        rc = load_rules(cfg_path)
        typer.echo(f"OK — v{rc.version}, {len(rc.rules)} rules, threshold={rc.threshold}")
    except Exception as exc:
        typer.echo(f"INVALID: {exc}", err=True)
        raise SystemExit(1)


@app.command("show-config")
def show_config():
    """Print current settings with secrets redacted."""
    from portfolio_agent.settings import get_settings
    s = get_settings()
    data = s.model_dump()
    for key in ("openai_api_key",):
        if key in data and data[key]:
            data[key] = data[key][:8] + "…[redacted]"
    typer.echo(json.dumps(data, indent=2, default=str))


@app.command("auth-user")
def auth_user(
    email:     str  = typer.Option(..., help="User's email (must exist in users table)"),
    folder_id: str  = typer.Option(..., help="Google Drive folder ID for this user's portfolio docs"),
):
    """
    Run the one-time OAuth2 consent flow for a user and store credentials in DB.

    This opens a browser (or prints a URL on WSL2/headless). After consent,
    the token is saved to portfolio_agent.user_google_config and the pipeline
    can write to this user's Drive folder automatically from then on.
    """
    async def _inner():
        from portfolio_agent.gdocs.auth import run_oauth_flow
        deps = await _build()
        typer.echo(f"Starting OAuth flow for {email}...")
        typer.echo("A browser will open (or copy-paste the URL if it doesn't).")
        creds = run_oauth_flow(deps.settings.google_client_secrets_path)
        token_json = creds.to_json()
        await deps.ops_repo.save_user_google_config(
            user_email=email,
            google_folder_id=folder_id,
            google_token_json=token_json,
        )
        typer.echo(f"✓ OAuth token stored for {email} (folder: {folder_id})")

    _run(_inner())


@app.command("serve")
def serve():
    """Start the APScheduler loop (portfolio daily + feedback sweep). Runs forever."""
    import asyncio
    from portfolio_agent.scheduler import main as scheduler_main
    asyncio.run(scheduler_main())


@app.command("approve-pending")
def approve_pending(
    id: int  = typer.Option(..., help="Pending change ID"),
    as_: str = typer.Option(..., "--as", help="Approver email"),
):
    """Approve a pending rule-weight change in the DB."""
    async def _inner():
        deps = await _build()
        result = await deps.ops_repo.approve_pending_change(change_id=id, approved_by=as_)
        typer.echo(json.dumps(result, indent=2, default=str))

    _run(_inner())


if __name__ == "__main__":
    app()
