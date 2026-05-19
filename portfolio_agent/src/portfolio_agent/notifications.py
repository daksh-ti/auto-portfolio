"""
Email notifications for the portfolio agent.

Two digests are sent:
  1. portfolio_digest  — after entries are written to a user's Google Doc
  2. feedback_digest   — after a user's comments are classified and persisted

Transport: async SMTP via aiosmtplib (STARTTLS, port 587).
If notifications_enabled=False or SMTP creds are absent, all sends are no-ops.
A failed send logs a warning but never raises — notifications must not fail the pipeline.
"""
from __future__ import annotations

import textwrap
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from portfolio_agent.settings import Settings
    from portfolio_agent.types import ClassifiedFeedback, PortfolioEntry

log = structlog.get_logger()

_DIVIDER = "─" * 60


class EmailNotifier:
    def __init__(self, settings: "Settings") -> None:
        self._s = settings

    @property
    def _enabled(self) -> bool:
        s = self._s
        return bool(
            s.notifications_enabled
            and s.smtp_user
            and s.smtp_password
            and s.email_from
        )

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def send_portfolio_digest(
        self,
        *,
        user_email: str,
        entries: list["PortfolioEntry"],
        doc_id: str,
        year_month: str,
    ) -> None:
        if not self._enabled:
            return
        subject = (
            f"Your portfolio was updated — {len(entries)} new"
            f" entr{'y' if len(entries) == 1 else 'ies'} [{year_month}]"
        )
        body = _build_portfolio_body(entries, doc_id, year_month)
        await self._send(to=user_email, subject=subject, body=body)

    async def send_feedback_digest(
        self,
        *,
        user_email: str,
        classified: list["ClassifiedFeedback"],
        rules_updated: bool,
        new_rules_version: int | None,
        applied_deltas: dict[str, tuple[float, float]],   # rule_id → (old, new)
        pending_rule_ids: list[str],
    ) -> None:
        if not self._enabled:
            return
        n = len(classified)
        subject = f"Feedback processed — {n} comment{'s' if n != 1 else ''} classified"
        body = _build_feedback_body(
            user_email=user_email,
            classified=classified,
            rules_updated=rules_updated,
            new_rules_version=new_rules_version,
            applied_deltas=applied_deltas,
            pending_rule_ids=pending_rule_ids,
        )
        await self._send(to=user_email, subject=subject, body=body)

    # -------------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------------

    async def _send(self, *, to: str, subject: str, body: str) -> None:
        import aiosmtplib

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = self._s.email_from
        msg["To"]      = to
        msg.attach(MIMEText(body, "plain", "utf-8"))

        try:
            await aiosmtplib.send(
                msg,
                hostname=self._s.smtp_host,
                port=self._s.smtp_port,
                username=self._s.smtp_user,
                password=self._s.smtp_password,
                start_tls=True,
            )
            log.info("email.sent", to=to, subject=subject)
        except Exception as exc:
            log.warning("email.send_failed", to=to, subject=subject, err=repr(exc))


# ---------------------------------------------------------------------------
# Body builders
# ---------------------------------------------------------------------------

def _build_portfolio_body(
    entries: list["PortfolioEntry"],
    doc_id: str,
    year_month: str,
) -> str:
    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
    lines: list[str] = [
        f"Your portfolio doc for {year_month} has been updated with"
        f" {len(entries)} new entr{'y' if len(entries) == 1 else 'ies'}.",
        "",
    ]

    for i, e in enumerate(entries, 1):
        top_rules = sorted(
            e.rule_scores, key=lambda r: r.score * r.weight, reverse=True
        )[:3]
        signals = "\n".join(
            f"    • {r.rule_name} ({r.score}/100): {r.justification}"
            for r in top_rules
        )
        block = textwrap.dedent(f"""\
            {i}. {e.conversation_title}   [Score: {e.overall_score}/100]
            {_DIVIDER}
            Why this matters:
            {textwrap.fill(e.why_it_matters, width=72, subsequent_indent='  ')}

            Top signals:
            {signals}

            Source: {e.citation}
        """)
        lines.append(block)
        lines.append("")

    lines += [
        _DIVIDER,
        f"View your portfolio doc:",
        f"  {doc_url}",
        "",
        "—Portfolio Agent",
    ]
    return "\n".join(lines)


def _build_feedback_body(
    *,
    user_email: str,
    classified: list["ClassifiedFeedback"],
    rules_updated: bool,
    new_rules_version: int | None,
    applied_deltas: dict[str, tuple[float, float]],
    pending_rule_ids: list[str],
) -> str:
    first_name = user_email.split("@")[0].split(".")[0].capitalize()
    n = len(classified)
    lines: list[str] = [
        f"Hi {first_name},",
        "",
        f"{n} of your comment{'s' if n != 1 else ''} on the portfolio doc"
        f" {'were' if n != 1 else 'was'} processed:",
        "",
    ]

    for i, f in enumerate(classified, 1):
        c = f.comment
        quoted = (
            f'"{c.quoted_text[:120]}…"' if c.quoted_text and len(c.quoted_text) > 120
            else f'"{c.quoted_text}"' if c.quoted_text
            else "(doc-level comment)"
        )
        body_preview = (
            c.body[:200] + "…" if len(c.body) > 200 else c.body
        )
        lines += [
            f"Comment {i}",
            f"  Anchored to : {quoted}",
            f"  Your comment: \"{body_preview}\"",
            f"  Classification: {f.target.value.upper()} · {f.sentiment}",
            f"  Takeaway: {f.actionable_takeaway}",
            "",
        ]

    # Rule weight changes section
    lines.append(_DIVIDER)
    if applied_deltas or pending_rule_ids:
        lines.append("Rule weight changes this run:")
        for rule_id, (old_w, new_w) in applied_deltas.items():
            lines.append(f"  • {rule_id:<30s}  {old_w:.2f} → {new_w:.2f}  (auto-applied)")
        for rule_id in pending_rule_ids:
            lines.append(
                f"  • {rule_id:<30s}  (pending approval — "
                f"run: portfolio-agent approve-pending --as {user_email})"
            )
        if new_rules_version:
            lines.append(f"\n  Rules config bumped to v{new_rules_version}.")
    else:
        lines += [
            "No rule weight changes this run.",
            "Your feedback was logged and will influence future analysis",
            "even without an immediate weight adjustment.",
        ]

    lines += [
        "",
        "—Portfolio Agent",
        f"  (sent {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})",
    ]
    return "\n".join(lines)
