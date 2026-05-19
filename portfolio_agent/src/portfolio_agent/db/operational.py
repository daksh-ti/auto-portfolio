"""
Read/write access to the portfolio_agent.* operational schema.
All tables live in the `portfolio_agent` Postgres schema (see migration 0001).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class OperationalRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    # -------------------------------------------------------------------------
    # Google Doc index
    # -------------------------------------------------------------------------

    async def get_or_create_doc(
        self,
        *,
        user_email: str,
        year_month: str,
        creator: Callable[[], str],
    ) -> str:
        """
        Return the doc_id for (user_email, year_month), creating it if absent.
        Uses a transaction + ON CONFLICT to be safe under concurrent callers.
        """
        async with self._engine.begin() as conn:
            # Try to fetch first (avoids calling creator unnecessarily).
            row = await conn.execute(
                text("""
                    SELECT doc_id FROM portfolio_agent.gdoc_index
                    WHERE user_email = :email AND year_month = :ym
                """),
                {"email": user_email, "ym": year_month},
            )
            existing = row.fetchone()
            if existing:
                return existing[0]

            doc_id = creator()

            result = await conn.execute(
                text("""
                    INSERT INTO portfolio_agent.gdoc_index (user_email, year_month, doc_id)
                    VALUES (:email, :ym, :doc_id)
                    ON CONFLICT (user_email, year_month) DO UPDATE
                        SET doc_id = portfolio_agent.gdoc_index.doc_id
                    RETURNING doc_id
                """),
                {"email": user_email, "ym": year_month, "doc_id": doc_id},
            )
            return result.fetchone()[0]  # type: ignore[index]

    async def list_all_doc_ids(self) -> list[str]:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text("SELECT doc_id FROM portfolio_agent.gdoc_index")
            )
            return [row[0] for row in result]

    async def update_last_checked(self, doc_id: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("""
                    UPDATE portfolio_agent.gdoc_index
                    SET last_checked_at = now()
                    WHERE doc_id = :doc_id
                """),
                {"doc_id": doc_id},
            )

    # -------------------------------------------------------------------------
    # Entry index
    # -------------------------------------------------------------------------

    async def record_entry(
        self,
        *,
        chat_id: str,
        doc_id: str,
        anchor_id: str,
        overall_score: int,
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("""
                    INSERT INTO portfolio_agent.entry_index
                        (chat_id, doc_id, anchor_id, overall_score)
                    VALUES (:chat_id, :doc_id, :anchor_id, :score)
                    ON CONFLICT (chat_id) DO NOTHING
                """),
                {
                    "chat_id": chat_id,
                    "doc_id": doc_id,
                    "anchor_id": anchor_id,
                    "score": overall_score,
                },
            )

    async def entries_in_doc(self, doc_id: str) -> dict[str, str]:
        """Returns {anchor_id: chat_id} for all entries in a given doc."""
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text("""
                    SELECT anchor_id, chat_id
                    FROM portfolio_agent.entry_index
                    WHERE doc_id = :doc_id
                """),
                {"doc_id": doc_id},
            )
            return {row[0]: row[1] for row in result}

    # -------------------------------------------------------------------------
    # Analysis runs
    # -------------------------------------------------------------------------

    async def record_analysis(
        self,
        *,
        chat_id: str,
        run_id: str,
        rules_version: int,
        rule_scores: list[dict[str, Any]],
        overall_score: int,
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("""
                    INSERT INTO portfolio_agent.analysis_runs
                        (chat_id, run_id, rules_version, rule_scores_jsonb, overall_score)
                    VALUES (:chat_id, :run_id, :rv, :scores, :score)
                    ON CONFLICT (chat_id, run_id) DO NOTHING
                """),
                {
                    "chat_id": chat_id,
                    "run_id": run_id,
                    "rv": rules_version,
                    "scores": json.dumps(rule_scores),
                    "score": overall_score,
                },
            )

    # -------------------------------------------------------------------------
    # Calibration tables (used to seed LLM prompts)
    # -------------------------------------------------------------------------

    async def recent_analyzer_feedback(self, limit: int = 5) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text("""
                    SELECT feedback_id, chat_id, comment_body, sentiment, takeaway
                    FROM portfolio_agent.analyzer_feedback
                    ORDER BY created_at DESC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            return [dict(row._mapping) for row in result]

    async def recent_generator_feedback(self, limit: int = 5) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text("""
                    SELECT feedback_id, chat_id, comment_body, sentiment, takeaway
                    FROM portfolio_agent.generator_feedback
                    ORDER BY created_at DESC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            return [dict(row._mapping) for row in result]

    async def insert_analyzer_feedback(
        self,
        *,
        feedback_id: str,
        chat_id: str | None,
        comment_body: str,
        sentiment: str,
        takeaway: str,
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("""
                    INSERT INTO portfolio_agent.analyzer_feedback
                        (feedback_id, chat_id, comment_body, sentiment, takeaway)
                    VALUES (:fid, :cid, :body, :sentiment, :takeaway)
                    ON CONFLICT (feedback_id) DO NOTHING
                """),
                {
                    "fid": feedback_id,
                    "cid": chat_id,
                    "body": comment_body,
                    "sentiment": sentiment,
                    "takeaway": takeaway,
                },
            )

    async def insert_generator_feedback(
        self,
        *,
        feedback_id: str,
        chat_id: str | None,
        comment_body: str,
        sentiment: str,
        takeaway: str,
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("""
                    INSERT INTO portfolio_agent.generator_feedback
                        (feedback_id, chat_id, comment_body, sentiment, takeaway)
                    VALUES (:fid, :cid, :body, :sentiment, :takeaway)
                    ON CONFLICT (feedback_id) DO NOTHING
                """),
                {
                    "fid": feedback_id,
                    "cid": chat_id,
                    "body": comment_body,
                    "sentiment": sentiment,
                    "takeaway": takeaway,
                },
            )

    # -------------------------------------------------------------------------
    # Feedback log (human-reviewable audit trail, replaces feedback Google Doc)
    # -------------------------------------------------------------------------

    async def is_comment_seen(self, comment_id: str) -> bool:
        """True if the comment is already in feedback_log (idempotency guard)."""
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text("""
                    SELECT 1 FROM portfolio_agent.feedback_log
                    WHERE comment_id = :cid
                """),
                {"cid": comment_id},
            )
            return result.fetchone() is not None

    async def insert_feedback_log(
        self,
        *,
        comment_id: str,
        doc_id: str,
        entry_anchor_id: str | None,
        chat_id: str | None,
        author_email: str,
        author_name: str,
        quoted_text: str | None,
        comment_body: str,
        target: str,
        sentiment: str,
        rule_ids_touched: list[str],
        suggested_deltas: dict[str, float],
        actionable_takeaway: str,
        created_at: datetime,
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("""
                    INSERT INTO portfolio_agent.feedback_log (
                        comment_id, doc_id, entry_anchor_id, chat_id,
                        author_email, author_name, quoted_text, comment_body,
                        target, sentiment, rule_ids_touched, suggested_deltas,
                        actionable_takeaway, created_at
                    ) VALUES (
                        :comment_id, :doc_id, :anchor, :chat_id,
                        :author_email, :author_name, :quoted, :body,
                        :target, :sentiment, :rule_ids::jsonb, :deltas::jsonb,
                        :takeaway, :created_at
                    )
                    ON CONFLICT (comment_id) DO NOTHING
                """),
                {
                    "comment_id": comment_id,
                    "doc_id": doc_id,
                    "anchor": entry_anchor_id,
                    "chat_id": chat_id,
                    "author_email": author_email,
                    "author_name": author_name,
                    "quoted": quoted_text,
                    "body": comment_body,
                    "target": target,
                    "sentiment": sentiment,
                    "rule_ids": json.dumps(rule_ids_touched),
                    "deltas": json.dumps(suggested_deltas),
                    "takeaway": actionable_takeaway,
                    "created_at": created_at,
                },
            )

    # -------------------------------------------------------------------------
    # Rules pending changes
    # -------------------------------------------------------------------------

    async def record_pending_change(
        self,
        *,
        rule_id: str,
        current_weight: float,
        proposed_weight: float,
        driving_comment_ids: list[str],
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("""
                    INSERT INTO portfolio_agent.rules_pending_changes
                        (rule_id, current_weight, proposed_weight, driving_comment_ids)
                    VALUES (:rid, :cw, :pw, :cids::jsonb)
                """),
                {
                    "rid": rule_id,
                    "cw": current_weight,
                    "pw": proposed_weight,
                    "cids": json.dumps(driving_comment_ids),
                },
            )

    # -------------------------------------------------------------------------
    # Per-user Google config (Model B: per-user OAuth + per-user folder)
    # -------------------------------------------------------------------------

    async def get_user_google_config(self, user_email: str) -> dict[str, Any] | None:
        """Return {'google_folder_id': ..., 'google_token_json': ...} or None."""
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text("""
                    SELECT google_folder_id, google_token_json
                    FROM portfolio_agent.user_google_config
                    WHERE user_email = :email
                """),
                {"email": user_email},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None

    async def save_user_google_config(
        self,
        *,
        user_email: str,
        google_folder_id: str,
        google_token_json: str,
    ) -> None:
        """Upsert Google config for a user (called from auth-user CLI)."""
        async with self._engine.begin() as conn:
            await conn.execute(
                text("""
                    INSERT INTO portfolio_agent.user_google_config
                        (user_email, google_folder_id, google_token_json)
                    VALUES (:email, :folder_id, :token_json)
                    ON CONFLICT (user_email) DO UPDATE
                        SET google_folder_id  = EXCLUDED.google_folder_id,
                            google_token_json = EXCLUDED.google_token_json,
                            token_updated_at  = now()
                """),
                {
                    "email": user_email,
                    "folder_id": google_folder_id,
                    "token_json": google_token_json,
                },
            )

    async def update_user_google_token(
        self,
        *,
        user_email: str,
        google_token_json: str,
    ) -> None:
        """Persist a refreshed token back to the DB."""
        async with self._engine.begin() as conn:
            await conn.execute(
                text("""
                    UPDATE portfolio_agent.user_google_config
                    SET google_token_json = :token_json,
                        token_updated_at  = now()
                    WHERE user_email = :email
                """),
                {"token_json": google_token_json, "email": user_email},
            )

    async def approve_pending_change(
        self,
        *,
        change_id: int,
        approved_by: str,
    ) -> dict[str, Any]:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("""
                    UPDATE portfolio_agent.rules_pending_changes
                    SET approved_by = :approver, applied = TRUE
                    WHERE id = :id AND applied = FALSE
                    RETURNING id, rule_id, current_weight, proposed_weight,
                              driving_comment_ids, approved_by
                """),
                {"approver": approved_by, "id": change_id},
            )
            row = result.fetchone()
            if row is None:
                raise ValueError(f"Change {change_id} not found or already applied")
            return dict(row._mapping)
