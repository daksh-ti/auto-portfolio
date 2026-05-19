"""
Read-only access to the cursor chats table in the source DB.

SCHEMA_TODO: Column names below are assumed. Adjust to match the real schema.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from portfolio_agent.types import ChatRecord


class ChatRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def fetch_chats(
        self,
        *,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[ChatRecord]:
        """
        Fetch chats whose started_at falls in [start, end).
        Ordered by user_id then started_at so per-user batches are contiguous.

        SCHEMA_TODO: real column names may differ — this is the only place to fix them.
        """
        sql = text("""
            SELECT
                c.chat_id,
                c.user_id,
                u.email          AS user_email,
                c.project_id,
                c.started_at,
                c.ended_at,
                c.messages_jsonb AS messages,
                c.metadata_jsonb AS metadata
            FROM cursor_chats c
            JOIN users u ON u.user_id = c.user_id
            WHERE c.started_at >= :start
              AND c.started_at <  :end
            ORDER BY c.user_id, c.started_at
            LIMIT :limit
        """)
        async with self._engine.connect() as conn:
            result = await conn.execute(sql, {"start": start, "end": end, "limit": limit})
            rows = []
            for row in result:
                mapping = dict(row._mapping)
                # messages / metadata may be returned as strings depending on driver
                if isinstance(mapping.get("messages"), str):
                    mapping["messages"] = json.loads(mapping["messages"])
                if isinstance(mapping.get("metadata"), str):
                    mapping["metadata"] = json.loads(mapping["metadata"])
                rows.append(mapping)  # type: ignore[arg-type]
            return rows
