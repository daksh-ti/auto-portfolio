"""Initial portfolio_agent schema

Revision ID: 0001
Revises:
Create Date: 2026-05-19
"""
from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS portfolio_agent")

    op.execute("""
        CREATE TABLE portfolio_agent.gdoc_index (
            user_email      TEXT        NOT NULL,
            year_month      TEXT        NOT NULL,
            doc_id          TEXT        NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_checked_at TIMESTAMPTZ,
            PRIMARY KEY (user_email, year_month)
        )
    """)

    op.execute("""
        CREATE TABLE portfolio_agent.entry_index (
            chat_id        TEXT PRIMARY KEY,
            doc_id         TEXT        NOT NULL,
            anchor_id      TEXT        NOT NULL,
            overall_score  INT         NOT NULL,
            written_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ON portfolio_agent.entry_index(doc_id)")

    op.execute("""
        CREATE TABLE portfolio_agent.analysis_runs (
            chat_id           TEXT        NOT NULL,
            run_id            TEXT        NOT NULL,
            rules_version     INT         NOT NULL,
            rule_scores_jsonb JSONB       NOT NULL,
            overall_score     INT         NOT NULL,
            analyzed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (chat_id, run_id)
        )
    """)

    op.execute("""
        CREATE TABLE portfolio_agent.analyzer_feedback (
            feedback_id  TEXT PRIMARY KEY,
            chat_id      TEXT,
            comment_body TEXT        NOT NULL,
            sentiment    TEXT        NOT NULL,
            takeaway     TEXT        NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ON portfolio_agent.analyzer_feedback(created_at DESC)")

    op.execute("""
        CREATE TABLE portfolio_agent.generator_feedback (
            feedback_id  TEXT PRIMARY KEY,
            chat_id      TEXT,
            comment_body TEXT        NOT NULL,
            sentiment    TEXT        NOT NULL,
            takeaway     TEXT        NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ON portfolio_agent.generator_feedback(created_at DESC)")

    op.execute("""
        CREATE TABLE portfolio_agent.feedback_log (
            comment_id          TEXT PRIMARY KEY,
            doc_id              TEXT        NOT NULL,
            entry_anchor_id     TEXT,
            chat_id             TEXT,
            author_email        TEXT        NOT NULL,
            author_name         TEXT        NOT NULL,
            quoted_text         TEXT,
            comment_body        TEXT        NOT NULL,
            target              TEXT        NOT NULL,
            sentiment           TEXT        NOT NULL,
            rule_ids_touched    JSONB       NOT NULL DEFAULT '[]',
            suggested_deltas    JSONB       NOT NULL DEFAULT '{}',
            actionable_takeaway TEXT        NOT NULL,
            created_at          TIMESTAMPTZ NOT NULL,
            seen_at             TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ON portfolio_agent.feedback_log(created_at DESC)")
    op.execute("CREATE INDEX ON portfolio_agent.feedback_log(doc_id)")

    op.execute("""
        CREATE TABLE portfolio_agent.rules_pending_changes (
            id                  SERIAL      PRIMARY KEY,
            proposed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            rule_id             TEXT        NOT NULL,
            current_weight      REAL        NOT NULL,
            proposed_weight     REAL        NOT NULL,
            driving_comment_ids JSONB       NOT NULL,
            approved_by         TEXT,
            applied             BOOLEAN     NOT NULL DEFAULT FALSE
        )
    """)

    # Per-user Google OAuth config (Model B: per-user OAuth + per-user folder)
    op.execute("""
        CREATE TABLE portfolio_agent.user_google_config (
            user_email        TEXT        PRIMARY KEY,
            google_folder_id  TEXT        NOT NULL,
            google_token_json TEXT,
            configured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            token_updated_at  TIMESTAMPTZ
        )
    """)


def downgrade() -> None:
    op.execute("DROP SCHEMA portfolio_agent CASCADE")
