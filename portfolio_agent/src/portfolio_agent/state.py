from operator import add
from typing import Annotated, TypedDict

from portfolio_agent.types import (
    ActiveUser,
    AnalyzedChat,
    ChatRecord,
    ClassifiedFeedback,
    CommentRecord,
    PortfolioEntry,
    PreprocessedChat,
)


class PortfolioState(TypedDict, total=False):
    run_id: str
    window_start: str       # ISO8601 datetime, inclusive
    window_end: str         # ISO8601 datetime, exclusive
    rules_version: int

    active_users: list[ActiveUser]
    raw_chats: list[ChatRecord]
    preprocessed_chats: list[PreprocessedChat]
    analyzed_chats: list[AnalyzedChat]
    qualifying_chats: list[AnalyzedChat]
    generated_entries: list[PortfolioEntry]
    written_entries: list[PortfolioEntry]

    errors: Annotated[list[str], add]
    metrics: dict


class FeedbackState(TypedDict, total=False):
    run_id: str
    since: str              # ISO8601 timestamp

    fetched_comments: list[CommentRecord]
    classified: list[ClassifiedFeedback]
    persisted_to_db: list[str]   # comment_ids written to feedback_log table
    rules_updated: bool
    new_rules_version: int | None

    errors: Annotated[list[str], add]
    metrics: dict
