from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, TypedDict

from typing_extensions import NotRequired

from pydantic import BaseModel, ConfigDict, Field


# ===== Cursor DB inputs  (SCHEMA_TODO: confirm against real sample) ==========

class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class ChatMessage(TypedDict):
    role: MessageRole
    content: str
    timestamp: datetime
    files: NotRequired[list[str]]
    tool_calls: NotRequired[list[dict]]


class ChatRecord(TypedDict):
    chat_id: str
    user_id: str
    user_email: str
    project_id: str | None
    started_at: datetime
    ended_at: datetime | None   # may be null for in-progress or incomplete sessions
    messages: list[ChatMessage]
    metadata: dict


class ActiveUser(TypedDict):
    user_id: str
    email: str
    display_name: str
    role: str
    is_active: bool


# ===== Pipeline artifacts =====================================================

class PreprocessedChat(BaseModel):
    model_config = ConfigDict(frozen=True)

    chat_id: str
    user_id: str
    user_email: str
    started_at: datetime
    ended_at: datetime | None   # may be null for in-progress sessions
    project_id: str | None
    original_message_count: int
    filtered_message_count: int
    messages: list[dict]
    removal_reasons: list[str]
    metadata: dict


class RuleScore(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_id: str
    rule_name: str
    score: int = Field(ge=0, le=100)
    weight: float = Field(ge=0.0, le=5.0)
    justification: str = Field(min_length=1, max_length=500)


class AnalyzedChat(BaseModel):
    chat_id: str
    user_id: str
    user_email: str
    preprocessed: PreprocessedChat
    rule_scores: list[RuleScore]
    overall_score: int = Field(ge=10, le=100)
    analysis_summary: str
    rules_version: int
    analyzed_at: datetime


class PortfolioEntry(BaseModel):
    chat_id: str
    user_id: str
    user_email: str
    overall_score: int
    conversation_title: str
    user_highlights: list[str]      # 2-4 key user messages (paraphrased/quoted, ≤80 words each)
    assistant_summary: str          # 1-2 sentences summarising the assistant's contribution
    why_it_matters: str
    citation: str
    rule_scores: list[RuleScore]
    generated_at: datetime
    google_doc_id: str | None = None
    entry_anchor_id: str | None = None


# ===== Feedback domain ========================================================

class FeedbackTarget(str, Enum):
    PROMPT = "prompt"
    ENTRY_TEXT = "entry_text"
    UNKNOWN = "unknown"


class CommentRecord(BaseModel):
    comment_id: str
    google_doc_id: str
    entry_anchor_id: str | None
    chat_id: str | None
    author_email: str
    author_name: str
    quoted_text: str | None
    body: str
    created_at: datetime
    resolved: bool = False


class ClassifiedFeedback(BaseModel):
    comment: CommentRecord
    target: FeedbackTarget
    sentiment: Literal["positive", "negative", "neutral", "mixed"]
    rule_ids_touched: list[str]
    suggested_weight_delta: dict[str, float]  # rule_id -> [-0.2, 0.2]
    actionable_takeaway: str
