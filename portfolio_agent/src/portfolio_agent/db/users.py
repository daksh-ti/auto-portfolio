"""
Read-only access to the users table in the source DB.

SCHEMA_TODO: Column names below are assumed. Adjust to match the real schema.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from portfolio_agent.types import ActiveUser


class UsersRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def fetch_active(self) -> list[ActiveUser]:
        """
        Return all active users.

        SCHEMA_TODO: confirm column names (is_active, email, display_name, role).
        """
        sql = text("""
            SELECT user_id, email, display_name, role, is_active
            FROM users
            WHERE is_active = TRUE
        """)
        async with self._engine.connect() as conn:
            result = await conn.execute(sql)
            return [dict(row._mapping) for row in result]  # type: ignore[return-value]
