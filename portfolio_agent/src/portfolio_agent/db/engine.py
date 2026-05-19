from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from portfolio_agent.settings import Settings


async def build_engines(s: Settings) -> tuple[AsyncEngine, AsyncEngine]:
    """
    Returns (source_eng, ops_eng).

    source_eng  — connects to the DB containing cursor chats + users tables.
    ops_eng     — connects to the DB hosting portfolio_agent.* operational schema.
                  Re-uses source_eng when both URLs are identical.
    """
    source_eng = create_async_engine(
        s.source_db_url,
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
    )
    ops_eng = (
        source_eng
        if s.operational_db_url == s.source_db_url
        else create_async_engine(
            s.operational_db_url,
            pool_size=10,
            max_overflow=5,
            pool_pre_ping=True,
        )
    )
    return source_eng, ops_eng
