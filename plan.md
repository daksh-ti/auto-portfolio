# Portfolio Agent Module — Implementation Plan (v2)

> LangGraph + OpenAI pipeline. Ingests Cursor chats from a shared Postgres DB (chats + users in the same instance) on a schedule, filters/scores them against company culture rules, writes a portfolio entry to a personal Google Doc (OAuth2) when score ≥ threshold, and learns from Google Doc comments via a feedback loop — classified feedback is persisted to a `portfolio_agent.feedback_log` DB table (no separate feedback doc).

This document is meant to be implemented top-to-bottom with no further design decisions required.

---

## 0. Decisions Locked In v2

| # | Item | Decision |
|---|---|---|
| 1 | LLM provider | **OpenAI** via `langchain-openai`. Default `gpt-4.1` for analyze/generate/feedback, `gpt-4.1-mini` for preprocess. |
| 2 | Structured output | OpenAI native JSON schema via `with_structured_output(schema, method="json_schema", strict=True)`. |
| 3 | DB | Postgres. Cursor chats and users tables live in the **same DB** (single `source_db_url`). Operational schema (`portfolio_agent.*`) may share that instance or use a separate `operational_db_url`. |
| 4 | Chat schema | Assumed shape kept from v1; see §4.1. **Still pending real sample — flagged as `SCHEMA_TODO`.** |
| 5 | Portfolio doc model | One Google Doc per `(user_email, year_month)`. Entries prepended (newest first). The doc is owned by / shared with the user, so standard OAuth2 credentials (personal token) are sufficient. |
| 6 | Feedback storage | All classified feedback (PROMPT / ENTRY_TEXT / UNKNOWN targets) is persisted to `portfolio_agent.feedback_log` DB table. No separate feedback Google Doc. |
| 7 | Google auth | **Per-user OAuth2 credentials** stored in `portfolio_agent.user_google_config`. Each user's `token_json` + `google_folder_id` is stored in the operational DB. The agent reads the user's token, writes to their own Drive folder, and saves the refreshed token back. One `client_secret.json` (OAuth app registration) lives server-side. No service account. No shared folder. |
| 8 | Schedule | Portfolio daily 18:00 Asia/Kolkata. Feedback every 6h. |
| 9 | Score range | Per-rule `0–100`, overall `10–100` (clamped). |
| 10 | Default threshold | `60`. Configurable in `rules_config.yaml`. |
| 11 | Operational DB | Postgres, separate schema `portfolio_agent`. May share the source DB instance. |

---

## 1. Architecture

```
                           ┌──────────────────────┐
                           │  APScheduler (async) │
                           └──────────┬───────────┘
                                      │
                ┌─────────────────────┼─────────────────────┐
                │ 18:00 daily                       every 6h│
                ▼                                           ▼
   ┌─────────────────────────┐                ┌──────────────────────────┐
   │   PortfolioGraph        │                │   FeedbackGraph          │
   │  extract                │                │  fetch_comments          │
   │  → filter_users         │                │  → classify_comments     │
   │  → preprocess           │                │  → route_feedback        │
   │  → analyze              │                │  → persist_feedback      │
   │  → threshold_gate       │                │  → update_rules_config   │
   │  → generate_entries     │                └──────────────────────────┘
   │  → write_to_gdoc        │
   └─────────────────────────┘
```

Both graphs are compiled `StateGraph` instances with `PostgresSaver` checkpointing so partial runs are resumable.

---

## 2. Project Layout (exact files)

```
portfolio_agent/
├── README.md
├── plan.md
├── pyproject.toml
├── .env.example
├── alembic.ini
├── migrations/
│   └── versions/
│       └── 0001_initial.py
├── config/
│   ├── rules_config.yaml
│   ├── reviewers.yaml
│   ├── schedule.yaml
│   └── prompts/
│       ├── preprocess_system.txt
│       ├── preprocess_user.j2
│       ├── analyze_system.txt
│       ├── analyze_user.j2
│       ├── generate_system.txt
│       ├── generate_user.j2
│       ├── feedback_system.txt
│       └── feedback_user.j2
├── src/portfolio_agent/
│   ├── __init__.py
│   ├── settings.py
│   ├── types.py
│   ├── state.py
│   ├── llm.py
│   ├── deps.py
│   ├── secrets_scrub.py
│   ├── prompts.py             # template loader
│   ├── rules.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── engine.py
│   │   ├── cursor_chats.py
│   │   ├── users.py
│   │   └── operational.py
│   ├── gdocs/
│   │   ├── __init__.py
│   │   ├── auth.py            # OAuth2 token load/refresh (no service account)
│   │   ├── client.py
│   │   ├── portfolio_writer.py
│   │   └── comments_reader.py
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── extractor.py
│   │   ├── preprocessor.py
│   │   ├── analyzer.py
│   │   ├── generator.py
│   │   └── feedback.py
│   ├── graphs/
│   │   ├── __init__.py
│   │   ├── portfolio_graph.py
│   │   └── feedback_graph.py
│   ├── scheduler.py
│   └── cli.py
├── tests/
│   ├── conftest.py
│   ├── unit/...
│   ├── integration/...
│   └── fixtures/sample_chats.json
└── docker/
    ├── Dockerfile
    └── docker-compose.yml
```

---

## 3. Exact Dependencies (`pyproject.toml`)

```toml
[project]
name = "portfolio-agent"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "langgraph==0.2.60",
  "langgraph-checkpoint-postgres==2.0.7",
  "langchain-core==0.3.21",
  "langchain-openai==0.2.10",
  "openai==1.55.0",
  "pydantic==2.9.2",
  "pydantic-settings==2.6.1",
  "sqlalchemy==2.0.36",
  "psycopg[binary,pool]==3.2.3",
  "alembic==1.14.0",
  "google-api-python-client==2.151.0",
  "google-auth==2.36.0",
  "google-auth-httplib2==0.2.0",
  "apscheduler==3.10.4",
  "pyyaml==6.0.2",
  "jinja2==3.1.4",
  "tenacity==9.0.0",
  "structlog==24.4.0",
  "typer==0.13.0",
  "httpx==0.27.2",
]

[project.optional-dependencies]
dev = [
  "pytest==8.3.3",
  "pytest-asyncio==0.24.0",
  "respx==0.21.1",
  "ruff==0.7.4",
  "mypy==1.13.0",
  "hypothesis==6.119.4",
]

[project.scripts]
portfolio-agent = "portfolio_agent.cli:app"
```

---

## 4. Types (`src/portfolio_agent/types.py`)

```python
from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Literal, NotRequired, TypedDict
from pydantic import BaseModel, ConfigDict, Field


# ===== Cursor DB inputs   (SCHEMA_TODO: confirm against real sample) ========

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
    ended_at: datetime
    messages: list[ChatMessage]
    metadata: dict


class ActiveUser(TypedDict):
    user_id: str
    email: str
    display_name: str
    role: str
    is_active: bool


# ===== Pipeline artifacts ==================================================

class PreprocessedChat(BaseModel):
    model_config = ConfigDict(frozen=True)
    chat_id: str
    user_id: str
    user_email: str
    started_at: datetime
    ended_at: datetime
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
    conversation_markdown: str
    why_it_matters: str
    citation: str
    rule_scores: list[RuleScore]
    generated_at: datetime
    google_doc_id: str | None = None
    entry_anchor_id: str | None = None


# ===== Feedback domain ====================================================

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
    suggested_weight_delta: dict[str, float]   # rule_id -> [-0.2, 0.2]
    actionable_takeaway: str
```

---

## 5. LangGraph State (`src/portfolio_agent/state.py`)

```python
from typing import Annotated, TypedDict
from operator import add
from portfolio_agent.types import (
    ActiveUser, ChatRecord, PreprocessedChat, AnalyzedChat,
    PortfolioEntry, CommentRecord, ClassifiedFeedback,
)


class PortfolioState(TypedDict, total=False):
    run_id: str
    window_start: str            # ISO8601 datetime, inclusive
    window_end: str              # ISO8601 datetime, exclusive
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
    since: str                   # ISO8601 timestamp
    fetched_comments: list[CommentRecord]
    classified: list[ClassifiedFeedback]
    persisted_to_db: list[str]   # comment_ids written to feedback_log table
    rules_updated: bool
    new_rules_version: int | None
    errors: Annotated[list[str], add]
    metrics: dict
```

---

## 6. Settings (`src/portfolio_agent/settings.py`)

```python
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="PA_")

    # ---- OpenAI ----
    openai_api_key: str
    openai_model_analyze: str = "gpt-4.1"
    openai_model_generate: str = "gpt-4.1"
    openai_model_preprocess: str = "gpt-4.1-mini"
    openai_model_feedback: str = "gpt-4.1"
    openai_temperature_analyze: float = 0.2
    openai_temperature_generate: float = 0.4
    openai_temperature_preprocess: float = 0.0
    openai_temperature_feedback: float = 0.1
    openai_max_tokens: int = 4096
    openai_request_timeout_s: float = 60.0
    openai_max_concurrency: int = 8

    # ---- Databases ----
    # Cursor chats and users tables share the same DB instance.
    source_db_url: str           # postgresql+psycopg://... (chats + users tables)
    operational_db_url: str      # portfolio_agent schema; may equal source_db_url

    # ---- Google (per-user OAuth2 — no service account, no shared folder) ----
    google_client_secrets_path: Path     # client_secret.json (OAuth app registration, shared)
    # Per-user token + folder_id are stored in portfolio_agent.user_google_config

    # ---- Scheduling ----
    schedule_timezone: str = "Asia/Kolkata"
    portfolio_cron_hour: int = 18
    portfolio_cron_minute: int = 0
    feedback_cron_every_hours: int = 6
    portfolio_window_hours: int = 24

    # ---- Config files ----
    rules_config_path: Path = Path("config/rules_config.yaml")
    reviewers_path: Path = Path("config/reviewers.yaml")
    prompts_dir: Path = Path("config/prompts")

    # ---- Optional integrations ----
    rules_git_repo_path: Path | None = None
    slack_webhook_url: str | None = None
    langsmith_api_key: str | None = None

    # ---- Safety ----
    max_chats_per_run: int = 5000
    max_weight_delta_per_run: float = 0.15
    manual_approval_threshold: float = 0.30
    weight_min: float = 0.1
    weight_max: float = 3.0


_settings: Settings | None = None
def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
```

`.env.example`:
```
PA_OPENAI_API_KEY=
PA_SOURCE_DB_URL=postgresql+psycopg://user:pass@localhost:5432/cursor
PA_OPERATIONAL_DB_URL=postgresql+psycopg://user:pass@localhost:5432/cursor
PA_GOOGLE_CLIENT_SECRETS_PATH=/secrets/client_secret.json
# Per-user token + Google folder stored in portfolio_agent.user_google_config
PA_SLACK_WEBHOOK_URL=
PA_LANGSMITH_API_KEY=
```

---

## 7. LLM Client (`src/portfolio_agent/llm.py`)

```python
from typing import Type, TypeVar
import asyncio, structlog
from pydantic import BaseModel
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from tenacity import retry, stop_after_attempt, wait_exponential_jitter, retry_if_exception_type
from openai import RateLimitError, APIError, APITimeoutError

T = TypeVar("T", bound=BaseModel)
log = structlog.get_logger()


class LLMClient:
    """One client per (model, temperature) pair."""

    def __init__(self, *, model: str, temperature: float, max_tokens: int,
                 api_key: str, timeout_s: float, concurrency: int):
        self._chat = ChatOpenAI(
            model=model, temperature=temperature, max_tokens=max_tokens,
            api_key=api_key, timeout=timeout_s, max_retries=0,
        )
        self._sem = asyncio.Semaphore(concurrency)
        self._model = model

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(initial=1, max=20),
        retry=retry_if_exception_type((RateLimitError, APIError, APITimeoutError)),
        reraise=True,
    )
    async def invoke_structured(self, *, system: str, user: str, schema: Type[T]) -> T:
        async with self._sem:
            structured = self._chat.with_structured_output(
                schema, method="json_schema", strict=True,
            )
            return await structured.ainvoke([
                SystemMessage(content=system),
                HumanMessage(content=user),
            ])  # type: ignore[return-value]
```

Held in `Deps` as four separate instances so per-model concurrency limits don't fight.

---

## 8. Dependencies Container (`src/portfolio_agent/deps.py`)

```python
from dataclasses import dataclass
from portfolio_agent.settings import Settings, get_settings
from portfolio_agent.llm import LLMClient
from portfolio_agent.gdocs.client import GoogleDocsClient
from portfolio_agent.gdocs.auth import build_google_services  # OAuth2 token-based
from portfolio_agent.db.engine import build_engines
from portfolio_agent.db.cursor_chats import ChatRepo
from portfolio_agent.db.users import UsersRepo
from portfolio_agent.db.operational import OperationalRepo


@dataclass
class Deps:
    settings: Settings
    chat_repo: ChatRepo
    users_repo: UsersRepo
    ops_repo: OperationalRepo
    gdocs: GoogleDocsClient
    llm_preprocess: LLMClient
    llm_analyze: LLMClient
    llm_generate: LLMClient
    llm_feedback: LLMClient


async def build_deps() -> Deps:
    s = get_settings()
    source_eng, ops_eng = await build_engines(s)
    docs_svc, drive_svc = build_google_services(s)   # OAuth2 token-based

    def _llm(model: str, temp: float) -> LLMClient:
        return LLMClient(
            model=model, temperature=temp,
            max_tokens=s.openai_max_tokens, api_key=s.openai_api_key,
            timeout_s=s.openai_request_timeout_s, concurrency=s.openai_max_concurrency,
        )

    return Deps(
        settings=s,
        chat_repo=ChatRepo(source_eng),
        users_repo=UsersRepo(source_eng),  # same engine — different tables, same DB
        ops_repo=OperationalRepo(ops_eng),
        gdocs=GoogleDocsClient(docs_svc, drive_svc),
        llm_preprocess=_llm(s.openai_model_preprocess, s.openai_temperature_preprocess),
        llm_analyze=_llm(s.openai_model_analyze, s.openai_temperature_analyze),
        llm_generate=_llm(s.openai_model_generate, s.openai_temperature_generate),
        llm_feedback=_llm(s.openai_model_feedback, s.openai_temperature_feedback),
    )
```

---

## 9. `rules_config.yaml` — Complete Starter

```yaml
version: 1
threshold: 60
preprocess:
  min_chat_length_chars: 200
  min_messages_after_filter: 2
  drop_phrases_exact:
    - "continue"
    - "go on"
    - "go ahead"
    - "let's go"
    - "lets go"
    - "ok"
    - "okay"
    - "thanks"
    - "thank you"
    - "ty"
    - "cool"
    - "nice"
    - "perfect"
    - "yes"
    - "no"
    - "y"
    - "n"
  drop_if_shorter_than_chars: 8
  keep_if_contains_code_block: true
rules:
  - id: culture.curiosity
    name: "Curiosity & Learning"
    weight: 1.0
    description: >
      Engineer explores tradeoffs, asks "why", reads docs, or learns a new
      pattern instead of pattern-matching past solutions.
    positive_signals:
      - "asks clarifying questions before coding"
      - "explores multiple approaches"
      - "investigates root cause"
    negative_signals:
      - "copies blindly without reading"
      - "stops at first plausible answer"
  - id: culture.ownership
    name: "Ownership"
    weight: 1.2
    description: "Engineer takes responsibility end-to-end, including edge cases and follow-ups."
    positive_signals:
      - "considers rollback / monitoring"
      - "follows through on cleanup"
    negative_signals:
      - "leaves TODOs without context"
      - "stops at happy path"
  - id: craft.rigor
    name: "Engineering Rigor"
    weight: 1.0
    description: "Tests, edge cases, error handling, observability."
    positive_signals:
      - "adds or discusses tests"
      - "handles errors deliberately"
      - "adds logging/metrics"
    negative_signals:
      - "ignores errors"
      - "no test discussion in non-trivial change"
  - id: collaboration.communication
    name: "Communication"
    weight: 0.8
    description: "Clear, well-structured prompts and explanations."
    positive_signals:
      - "concrete, scoped prompts"
      - "summarizes decisions"
    negative_signals:
      - "vague, one-word prompts on complex tasks"
changelog:
  - version: 1
    at: "2026-05-19T00:00:00Z"
    reason: "Initial ruleset."
    changes: []
```

`src/portfolio_agent/rules.py` exposes:

```python
from pathlib import Path
from datetime import datetime, timezone
import os, tempfile, yaml
from pydantic import BaseModel, Field


class PreprocessCfg(BaseModel):
    min_chat_length_chars: int
    min_messages_after_filter: int
    drop_phrases_exact: list[str]
    drop_if_shorter_than_chars: int
    keep_if_contains_code_block: bool


class RuleDef(BaseModel):
    id: str
    name: str
    weight: float = Field(ge=0.0, le=5.0)
    description: str
    positive_signals: list[str]
    negative_signals: list[str]


class ChangelogChange(BaseModel):
    rule_id: str
    old_weight: float
    new_weight: float
    driving_comment_ids: list[str]


class ChangelogEntry(BaseModel):
    version: int
    at: str
    reason: str
    changes: list[ChangelogChange]


class RulesConfig(BaseModel):
    version: int
    threshold: int = Field(ge=10, le=100)
    preprocess: PreprocessCfg
    rules: list[RuleDef]
    changelog: list[ChangelogEntry]


def load_rules(path: Path) -> RulesConfig:
    return RulesConfig.model_validate(yaml.safe_load(path.read_text()))


def save_rules(path: Path, rc: RulesConfig) -> None:
    data = yaml.safe_dump(rc.model_dump(), sort_keys=False)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=".rules_", suffix=".yaml", delete=False,
    ) as tf:
        tf.write(data); tf.flush(); os.fsync(tf.fileno())
        tmp_name = tf.name
    os.replace(tmp_name, path)


def clamp_weight(w: float, *, weight_min: float, weight_max: float) -> float:
    return max(weight_min, min(weight_max, w))


def apply_weight_deltas(rc: RulesConfig, deltas: dict[str, float], *,
                        driving_comment_ids: dict[str, list[str]],
                        reason: str, weight_min: float, weight_max: float) -> RulesConfig:
    new_rules = []
    changes: list[ChangelogChange] = []
    for r in rc.rules:
        if r.id in deltas:
            new_w = clamp_weight(r.weight + deltas[r.id], weight_min=weight_min, weight_max=weight_max)
            if abs(new_w - r.weight) > 1e-9:
                changes.append(ChangelogChange(
                    rule_id=r.id, old_weight=r.weight, new_weight=new_w,
                    driving_comment_ids=driving_comment_ids.get(r.id, []),
                ))
                new_rules.append(r.model_copy(update={"weight": new_w}))
            else:
                new_rules.append(r)
        else:
            new_rules.append(r)
    if not changes:
        return rc  # no-op
    new_version = rc.version + 1
    entry = ChangelogEntry(
        version=new_version,
        at=datetime.now(timezone.utc).isoformat(),
        reason=reason, changes=changes,
    )
    return rc.model_copy(update={"version": new_version,
                                  "rules": new_rules,
                                  "changelog": [entry] + rc.changelog})
```

---

## 10. Database Layer

### 10.1 Engine (`db/engine.py`)

Cursor chats and users are in the **same DB** (`source_db_url`), so a single engine serves both repos. Operational may share that instance or use a dedicated URL.

```python
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


async def build_engines(s):
    source_eng = create_async_engine(s.source_db_url, pool_size=10, max_overflow=5)
    ops_eng = (source_eng if s.operational_db_url == s.source_db_url
               else create_async_engine(s.operational_db_url, pool_size=10))
    return source_eng, ops_eng
```

### 10.2 Cursor Chats (`db/cursor_chats.py`)

```python
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from portfolio_agent.types import ChatRecord


class ChatRepo:
    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def fetch_chats(self, *, start: datetime, end: datetime,
                          limit: int) -> list[ChatRecord]:
        sql = text("""
            SELECT c.chat_id, c.user_id, u.email AS user_email, c.project_id,
                   c.started_at, c.ended_at,
                   c.messages_jsonb AS messages,
                   c.metadata_jsonb AS metadata
            FROM cursor_chats c
            JOIN users u ON u.user_id = c.user_id
            WHERE c.started_at >= :start AND c.started_at < :end
            ORDER BY c.user_id, c.started_at
            LIMIT :limit
        """)
        async with self._engine.connect() as conn:
            result = await conn.execute(sql, {"start": start, "end": end, "limit": limit})
            return [dict(row._mapping) for row in result]  # type: ignore[return-value]
```

**SCHEMA_TODO:** real column names may differ. Mapping happens here and only here.

### 10.3 Users (`db/users.py`)

```python
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from portfolio_agent.types import ActiveUser


class UsersRepo:
    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def fetch_active(self) -> list[ActiveUser]:
        sql = text("""
            SELECT user_id, email, display_name, role, is_active
            FROM users WHERE is_active = TRUE
        """)
        async with self._engine.connect() as conn:
            result = await conn.execute(sql)
            return [dict(row._mapping) for row in result]  # type: ignore[return-value]
```

### 10.4 Operational Schema — Migration `0001_initial.py`

```sql
CREATE SCHEMA IF NOT EXISTS portfolio_agent;

CREATE TABLE portfolio_agent.gdoc_index (
  user_email      TEXT NOT NULL,
  year_month      TEXT NOT NULL,           -- 'YYYY-MM'
  doc_id          TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_checked_at TIMESTAMPTZ,
  PRIMARY KEY (user_email, year_month)
);

CREATE TABLE portfolio_agent.entry_index (
  chat_id        TEXT PRIMARY KEY,
  doc_id         TEXT NOT NULL,
  anchor_id      TEXT NOT NULL,
  overall_score  INT  NOT NULL,
  written_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON portfolio_agent.entry_index(doc_id);

CREATE TABLE portfolio_agent.analysis_runs (
  chat_id           TEXT NOT NULL,
  run_id            TEXT NOT NULL,
  rules_version     INT  NOT NULL,
  rule_scores_jsonb JSONB NOT NULL,
  overall_score     INT  NOT NULL,
  analyzed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (chat_id, run_id)
);

-- Calibration tables (used to seed LLM prompts for analyzer / generator)
CREATE TABLE portfolio_agent.analyzer_feedback (
  feedback_id   TEXT PRIMARY KEY,
  chat_id       TEXT,
  comment_body  TEXT NOT NULL,
  sentiment     TEXT NOT NULL,
  takeaway      TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON portfolio_agent.analyzer_feedback(created_at DESC);

CREATE TABLE portfolio_agent.generator_feedback (
  feedback_id   TEXT PRIMARY KEY,
  chat_id       TEXT,
  comment_body  TEXT NOT NULL,
  sentiment     TEXT NOT NULL,
  takeaway      TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON portfolio_agent.generator_feedback(created_at DESC);

-- Human-reviewable log of every classified comment (replaces the feedback Google Doc)
CREATE TABLE portfolio_agent.feedback_log (
  comment_id         TEXT PRIMARY KEY,
  doc_id             TEXT NOT NULL,
  entry_anchor_id    TEXT,
  chat_id            TEXT,
  author_email       TEXT NOT NULL,
  author_name        TEXT NOT NULL,
  quoted_text        TEXT,
  comment_body       TEXT NOT NULL,
  target             TEXT NOT NULL,   -- 'prompt' | 'entry_text' | 'unknown'
  sentiment          TEXT NOT NULL,
  rule_ids_touched   JSONB NOT NULL DEFAULT '[]',
  suggested_deltas   JSONB NOT NULL DEFAULT '{}',
  actionable_takeaway TEXT NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL,
  seen_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON portfolio_agent.feedback_log(created_at DESC);
CREATE INDEX ON portfolio_agent.feedback_log(doc_id);

CREATE TABLE portfolio_agent.rules_pending_changes (
  id                  SERIAL PRIMARY KEY,
  proposed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  rule_id             TEXT NOT NULL,
  current_weight      REAL NOT NULL,
  proposed_weight     REAL NOT NULL,
  driving_comment_ids JSONB NOT NULL,
  approved_by         TEXT,
  applied             BOOLEAN NOT NULL DEFAULT FALSE
);
```

### 10.5 `OperationalRepo` interface (`db/operational.py`)

```python
class OperationalRepo:
    def __init__(self, engine: AsyncEngine): ...

    async def get_or_create_doc(self, *, user_email: str, year_month: str,
                                creator) -> str: ...
    async def record_entry(self, *, chat_id: str, doc_id: str,
                           anchor_id: str, overall_score: int) -> None: ...
    async def list_all_doc_ids(self) -> list[str]: ...
    async def entries_in_doc(self, doc_id: str) -> dict[str, str]: ...   # anchor_id -> chat_id
    async def update_last_checked(self, doc_id: str) -> None: ...

    async def record_analysis(self, *, chat_id: str, run_id: str,
                              rules_version: int, rule_scores: list[dict],
                              overall_score: int) -> None: ...

    async def recent_analyzer_feedback(self, limit: int = 5) -> list[dict]: ...
    async def recent_generator_feedback(self, limit: int = 5) -> list[dict]: ...

    async def insert_analyzer_feedback(self, *, feedback_id: str, chat_id: str | None,
                                       comment_body: str, sentiment: str,
                                       takeaway: str) -> None: ...
    async def insert_generator_feedback(self, *, feedback_id: str, chat_id: str | None,
                                        comment_body: str, sentiment: str,
                                        takeaway: str) -> None: ...

    async def is_comment_seen(self, comment_id: str) -> bool: ...

    async def insert_feedback_log(self, *, comment_id: str, doc_id: str,
                                  entry_anchor_id: str | None, chat_id: str | None,
                                  author_email: str, author_name: str,
                                  quoted_text: str | None, comment_body: str,
                                  target: str, sentiment: str,
                                  rule_ids_touched: list[str],
                                  suggested_deltas: dict[str, float],
                                  actionable_takeaway: str,
                                  created_at) -> None: ...

    async def record_pending_change(self, *, rule_id: str, current_weight: float,
                                     proposed_weight: float,
                                     driving_comment_ids: list[str]) -> None: ...
    async def approve_pending_change(self, *, change_id: int, approved_by: str) -> dict: ...
```

`get_or_create_doc` uses a single transaction with a SELECT, and on miss calls `creator()` (the Google Docs API call) then inserts. Idempotent under race via `INSERT ... ON CONFLICT DO NOTHING RETURNING doc_id` fallback.

---

## 11. PortfolioGraph — Node-by-Node

### 11.1 `extract_node` (`agents/extractor.py`)

```python
import structlog
from datetime import datetime
from portfolio_agent.state import PortfolioState
from portfolio_agent.deps import Deps

log = structlog.get_logger()


async def extract_node(state: PortfolioState, deps: Deps) -> PortfolioState:
    start = datetime.fromisoformat(state["window_start"])
    end = datetime.fromisoformat(state["window_end"])
    chats = await deps.chat_repo.fetch_chats(
        start=start, end=end, limit=deps.settings.max_chats_per_run,
    )
    log.info("extract.done", run_id=state["run_id"], count=len(chats))
    return {
        "raw_chats": chats,
        "metrics": {**state.get("metrics", {}), "extracted_count": len(chats)},
    }
```

### 11.2 `filter_users_node` (`agents/extractor.py`)

```python
async def filter_users_node(state: PortfolioState, deps: Deps) -> PortfolioState:
    active = await deps.users_repo.fetch_active()
    active_ids = {u["user_id"] for u in active}
    kept = [c for c in state["raw_chats"] if c["user_id"] in active_ids]
    log.info("filter_users.done",
             active=len(active_ids), kept=len(kept),
             dropped=len(state["raw_chats"]) - len(kept))
    return {
        "active_users": active,
        "raw_chats": kept,
        "metrics": {**state["metrics"], "after_user_filter": len(kept)},
    }
```

### 11.3 `preprocess_node` (`agents/preprocessor.py`)

**Stage 1 — deterministic filter (pure Python):**

```python
from portfolio_agent.rules import PreprocessCfg


def deterministic_filter(messages: list[dict],
                         cfg: PreprocessCfg) -> tuple[list[dict], list[str]]:
    kept: list[dict] = []
    reasons: list[str] = []
    drops_lower = {p.lower() for p in cfg.drop_phrases_exact}
    for i, m in enumerate(messages):
        content = (m.get("content") or "").strip()
        has_code = "```" in content
        if not content:
            reasons.append(f"msg[{i}]:empty"); continue
        if content.lower() in drops_lower:
            reasons.append(f"msg[{i}]:filler_phrase"); continue
        if (len(content) < cfg.drop_if_shorter_than_chars
                and not (cfg.keep_if_contains_code_block and has_code)):
            reasons.append(f"msg[{i}]:too_short"); continue
        kept.append(m)
    return kept, reasons
```

**Stage 2 — LLM filter schema:**

```python
from typing import Literal
from pydantic import BaseModel, Field


class PreprocessDecision(BaseModel):
    message_index: int = Field(ge=0)
    action: Literal["KEEP", "DROP"]
    reason: str = Field(max_length=120)


class PreprocessOutput(BaseModel):
    decisions: list[PreprocessDecision]
```

**System prompt — `config/prompts/preprocess_system.txt`:**

```
You filter Cursor chat messages for portfolio review.

For each message, decide:
- KEEP if it contains technical content, reasoning, decisions, debugging
  insight, design intent, or substantive instruction.
- DROP if it is filler: acknowledgements ("ok", "continue", "thanks"), pure
  small talk, or restating with no new content.

Rules:
- When uncertain, KEEP.
- Code blocks are almost always KEEP unless trivially small (e.g. a single
  empty function).
- Return exactly one decision per input message, in the same order.
- Reason must be <= 120 chars.
```

**User prompt — `config/prompts/preprocess_user.j2`:**

```
Messages:
{% for i, m in messages %}
[{{ i }}] ({{ m.role }}): {{ m.content[:600] }}{% if m.content|length > 600 %}…{% endif %}
{% endfor %}
```

**Node body:**

```python
import asyncio
from portfolio_agent.secrets_scrub import scrub
from portfolio_agent.prompts import read_text, render
from portfolio_agent.rules import load_rules
from portfolio_agent.types import PreprocessedChat, ChatRecord


async def preprocess_node(state: PortfolioState, deps: Deps) -> PortfolioState:
    cfg = load_rules(deps.settings.rules_config_path).preprocess
    tasks = [_preprocess_one(c, cfg, deps) for c in state["raw_chats"]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    kept: list[PreprocessedChat] = []
    errors: list[str] = []
    for chat, res in zip(state["raw_chats"], results):
        if isinstance(res, Exception):
            errors.append(f"preprocess:{chat['chat_id']}:{res!r}")
        elif res is not None:
            kept.append(res)
    return {
        "preprocessed_chats": kept,
        "errors": errors,
        "metrics": {**state["metrics"], "after_preprocess": len(kept)},
    }


async def _preprocess_one(chat: ChatRecord, cfg: PreprocessCfg,
                          deps: Deps) -> PreprocessedChat | None:
    # 0. Scrub secrets before anything touches an LLM.
    scrubbed = [{**m, "content": scrub(m.get("content", ""))} for m in chat["messages"]]
    # 1. Deterministic.
    msgs, reasons = deterministic_filter(scrubbed, cfg)
    if len(msgs) < cfg.min_messages_after_filter:
        return None
    # 2. LLM.
    out: PreprocessOutput = await deps.llm_preprocess.invoke_structured(
        system=read_text("preprocess_system.txt"),
        user=render("preprocess_user.j2", messages=list(enumerate(msgs))),
        schema=PreprocessOutput,
    )
    drop_idx = {d.message_index for d in out.decisions if d.action == "DROP"}
    for d in out.decisions:
        if d.action == "DROP":
            reasons.append(f"llm_msg[{d.message_index}]:{d.reason}")
    final = [m for i, m in enumerate(msgs) if i not in drop_idx]
    if len(final) < cfg.min_messages_after_filter:
        return None
    if sum(len(m.get("content", "")) for m in final) < cfg.min_chat_length_chars:
        return None
    return PreprocessedChat(
        chat_id=chat["chat_id"], user_id=chat["user_id"], user_email=chat["user_email"],
        started_at=chat["started_at"], ended_at=chat["ended_at"],
        project_id=chat.get("project_id"),
        original_message_count=len(chat["messages"]),
        filtered_message_count=len(final),
        messages=final, removal_reasons=reasons, metadata=chat.get("metadata", {}),
    )
```

### 11.4 `analyze_node` (`agents/analyzer.py`)

**Output schema:**

```python
class AnalyzeOutput(BaseModel):
    rule_scores: list[RuleScore]
    overall_summary: str = Field(min_length=20, max_length=600)
```

**System prompt — `config/prompts/analyze_system.txt`:**

```
You evaluate Cursor chats for portfolio quality against company culture rules.

Output requirements:
- Score every rule provided, 0-100.
- Each justification: 1-2 sentences citing a specific moment from the chat
  (e.g. "When the engineer asked X before writing code…").
- overall_summary: 2-3 sentences naming the chat's strongest and weakest
  dimensions.

Calibration:
- 90-100: clearly exemplary; would be shown to new hires.
- 70-89:  solidly above bar.
- 50-69:  ordinary work.
- 30-49:  below bar; missing rigor or curiosity.
- 0-29:   counterproductive (anti-pattern).

If calibration examples are provided below, weight them heavily.
```

**User prompt — `config/prompts/analyze_user.j2`:**

```
Rules:
{% for r in rules %}
- id: {{ r.id }}
  name: {{ r.name }}
  weight: {{ r.weight }}
  description: {{ r.description }}
  positive_signals: {{ r.positive_signals }}
  negative_signals: {{ r.negative_signals }}
{% endfor %}

{% if calibration_examples %}
Calibration examples (admin feedback on prior analyses):
{% for ex in calibration_examples %}
- chat {{ ex.chat_id }}: "{{ ex.comment_body }}" ({{ ex.sentiment }}) — takeaway: {{ ex.takeaway }}
{% endfor %}
{% endif %}

Chat:
{{ chat_text }}
```

**Helpers:**

```python
from datetime import datetime
from portfolio_agent.types import RuleScore, AnalyzedChat, PreprocessedChat


def render_chat_text(c: PreprocessedChat) -> str:
    lines = []
    for m in c.messages:
        ts = m["timestamp"]
        ts_str = ts.isoformat() if isinstance(ts, datetime) else str(ts)
        lines.append(f"[{m['role']} @ {ts_str}]")
        lines.append(m["content"])
        lines.append("")
    return "\n".join(lines)


def compute_overall(rule_scores: list[RuleScore]) -> int:
    total_w = sum(r.weight for r in rule_scores) or 1.0
    weighted = sum(r.score * r.weight for r in rule_scores) / total_w
    return max(10, min(100, round(weighted)))


def ensure_all_rules(rule_scores: list[RuleScore], rules) -> list[RuleScore]:
    by_id = {r.rule_id: r for r in rule_scores}
    fixed = []
    for r in rules:
        if r.id in by_id:
            fixed.append(by_id[r.id])
        else:
            log.warning("analyze.missing_rule", rule_id=r.id)
            fixed.append(RuleScore(
                rule_id=r.id, rule_name=r.name, score=50, weight=r.weight,
                justification="(missing in LLM output; defaulted to 50)",
            ))
    return fixed
```

**Node body:**

```python
async def analyze_node(state: PortfolioState, deps: Deps) -> PortfolioState:
    rc = load_rules(deps.settings.rules_config_path)
    calibration = await deps.ops_repo.recent_analyzer_feedback(limit=5)
    tasks = [_analyze_one(c, rc, calibration, state["run_id"], deps)
             for c in state["preprocessed_chats"]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    analyzed, errors = [], []
    for c, res in zip(state["preprocessed_chats"], results):
        if isinstance(res, Exception):
            errors.append(f"analyze:{c.chat_id}:{res!r}")
        else:
            analyzed.append(res)
    return {
        "analyzed_chats": analyzed, "errors": errors,
        "rules_version": rc.version,
        "metrics": {**state["metrics"], "analyzed_count": len(analyzed)},
    }


async def _analyze_one(c: PreprocessedChat, rc, calibration, run_id: str,
                       deps: Deps) -> AnalyzedChat:
    chat_text = render_chat_text(c)
    out: AnalyzeOutput = await deps.llm_analyze.invoke_structured(
        system=read_text("analyze_system.txt"),
        user=render("analyze_user.j2",
                    rules=rc.rules, calibration_examples=calibration,
                    chat_text=chat_text),
        schema=AnalyzeOutput,
    )
    rule_scores = ensure_all_rules(out.rule_scores, rc.rules)
    overall = compute_overall(rule_scores)
    analyzed = AnalyzedChat(
        chat_id=c.chat_id, user_id=c.user_id, user_email=c.user_email,
        preprocessed=c, rule_scores=rule_scores,
        overall_score=overall, analysis_summary=out.overall_summary,
        rules_version=rc.version, analyzed_at=datetime.utcnow(),
    )
    await deps.ops_repo.record_analysis(
        chat_id=c.chat_id, run_id=run_id, rules_version=rc.version,
        rule_scores=[r.model_dump() for r in rule_scores],
        overall_score=overall,
    )
    return analyzed
```

### 11.5 `threshold_gate_node` (`agents/analyzer.py`)

```python
def threshold_gate_node(state: PortfolioState, deps: Deps) -> PortfolioState:
    rc = load_rules(deps.settings.rules_config_path)
    qualifying = [c for c in state["analyzed_chats"]
                  if c.overall_score >= rc.threshold]
    return {
        "qualifying_chats": qualifying,
        "metrics": {**state["metrics"],
                    "threshold": rc.threshold,
                    "qualifying_count": len(qualifying)},
    }
```

### 11.6 `generate_entries_node` (`agents/generator.py`)

**Output schema:**

```python
class GenerateOutput(BaseModel):
    conversation_title: str = Field(min_length=6, max_length=80)
    why_it_matters: str = Field(min_length=80, max_length=800)
```

**System prompt — `config/prompts/generate_system.txt`:**

```
You write portfolio entries for a Cursor chat review system.

The portfolio entry shows the verbatim conversation above your text — DO NOT
repeat or summarize the conversation. Write only:

1. conversation_title: a specific 6-10 word title naming the engineering work.
2. why_it_matters: 2-4 sentences for an admin reviewer. Cite the strongest
   rule(s) by NAME and reference at least one concrete moment in the chat.
   Avoid generic praise like "great work" or "nicely done".

If calibration examples are provided, follow their guidance on tone and depth.
```

**User prompt — `config/prompts/generate_user.j2`:**

```
Chat (verbatim):
{{ chat_text }}

Rule scores (highest weighted-contribution first):
{% for s in top_rule_scores %}
- {{ s.rule_name }} ({{ s.score }}/100, weight {{ s.weight }}): {{ s.justification }}
{% endfor %}

Overall score: {{ overall_score }}/100

{% if calibration_examples %}
Calibration examples (admin feedback on prior entries):
{% for ex in calibration_examples %}
- "{{ ex.comment_body }}" ({{ ex.sentiment }}) — takeaway: {{ ex.takeaway }}
{% endfor %}
{% endif %}
```

**Node body:**

```python
from portfolio_agent.types import PortfolioEntry


async def generate_entries_node(state: PortfolioState, deps: Deps) -> PortfolioState:
    calibration = await deps.ops_repo.recent_generator_feedback(limit=5)
    tasks = [_generate_one(c, calibration, deps) for c in state["qualifying_chats"]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    entries, errors = [], []
    for c, res in zip(state["qualifying_chats"], results):
        if isinstance(res, Exception):
            errors.append(f"generate:{c.chat_id}:{res!r}")
        else:
            entries.append(res)
    return {
        "generated_entries": entries, "errors": errors,
        "metrics": {**state["metrics"], "generated_count": len(entries)},
    }


async def _generate_one(c, calibration, deps: Deps) -> PortfolioEntry:
    top = sorted(c.rule_scores, key=lambda r: r.score * r.weight, reverse=True)[:3]
    chat_text = render_chat_text(c.preprocessed)
    out: GenerateOutput = await deps.llm_generate.invoke_structured(
        system=read_text("generate_system.txt"),
        user=render("generate_user.j2",
                    chat_text=chat_text, top_rule_scores=top,
                    overall_score=c.overall_score,
                    calibration_examples=calibration),
        schema=GenerateOutput,
    )
    return PortfolioEntry(
        chat_id=c.chat_id, user_id=c.user_id, user_email=c.user_email,
        overall_score=c.overall_score,
        conversation_title=out.conversation_title,
        conversation_markdown=chat_text,
        why_it_matters=out.why_it_matters,
        citation=(f"chat_id={c.chat_id} · started "
                  f"{c.preprocessed.started_at.isoformat()} · "
                  f"project={c.preprocessed.project_id or 'n/a'}"),
        rule_scores=c.rule_scores,
        generated_at=datetime.utcnow(),
    )
```

### 11.7 `write_to_gdoc_node` (`gdocs/portfolio_writer.py`)

**Google Docs client (`gdocs/client.py`):**

```python
from googleapiclient.errors import HttpError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, HttpError) and exc.resp.status in (429, 500, 502, 503, 504)


_RETRY = retry(stop=stop_after_attempt(5),
               wait=wait_exponential_jitter(initial=1, max=30),
               retry=retry_if_exception(_is_retryable), reraise=True)


class GoogleDocsClient:
    def __init__(self, docs_service, drive_service):
        self.docs = docs_service
        self.drive = drive_service

    @_RETRY
    def create_doc(self, *, title: str, parent_folder_id: str) -> str:
        body = {"name": title,
                "mimeType": "application/vnd.google-apps.document",
                "parents": [parent_folder_id]}
        return self.drive.files().create(body=body, fields="id").execute()["id"]

    @_RETRY
    def share(self, doc_id: str, emails: list[str], role: str = "commenter") -> None:
        for email in emails:
            try:
                self.drive.permissions().create(
                    fileId=doc_id,
                    body={"type": "user", "role": role, "emailAddress": email},
                    sendNotificationEmail=False,
                ).execute()
            except HttpError as e:
                if e.resp.status != 409:  # already shared
                    raise

    @_RETRY
    def batch_update(self, doc_id: str, requests: list[dict]) -> dict:
        return self.docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}).execute()

    @_RETRY
    def list_comments(self, *, doc_id: str, since) -> list[dict]:
        resp = self.drive.comments().list(
            fileId=doc_id,
            fields="comments(id,author,content,quotedFileContent,createdTime,resolved,anchor)",
            startModifiedTime=since.isoformat(),
            pageSize=100,
        ).execute()
        return resp.get("comments", [])

    @_RETRY
    def list_named_ranges(self, *, doc_id: str) -> dict[str, dict]:
        doc = self.docs.documents().get(
            documentId=doc_id, fields="namedRanges").execute()
        return doc.get("namedRanges", {})  # name -> namedRangeGroup
```

**Block renderer:**

```python
def render_entry_block(e: PortfolioEntry) -> str:
    rules_lines = "\n".join(
        f"  • {r.rule_name} ({r.score}/100): {r.justification}"
        for r in sorted(e.rule_scores,
                        key=lambda x: x.score * x.weight, reverse=True)[:3]
    )
    return (
        f"{e.conversation_title}     [Score: {e.overall_score}/100]\n"
        f"{'─' * 60}\n"
        f"Conversation:\n"
        f"{e.conversation_markdown}\n"
        f"{'─' * 60}\n"
        f"Why this matters:\n"
        f"{e.why_it_matters}\n\n"
        f"Top signals:\n{rules_lines}\n\n"
        f"Source: {e.citation}\n"
        f"{'═' * 60}\n\n"
    )
```

**Building `batchUpdate` requests** — each entry inserted at index 1 (top), styled as Heading 2 on title line, and wrapped in a named range so feedback comments can be located later:

```python
def build_entry_requests(entries: list[PortfolioEntry]) -> list[dict]:
    """Newest-first ordering: insert in reverse so the last inserted ends at top."""
    requests: list[dict] = []
    for e in reversed(entries):
        block = render_entry_block(e)
        end_idx = 1 + len(block)
        requests.append({"insertText": {"location": {"index": 1}, "text": block}})
        requests.append({
            "createNamedRange": {
                "name": f"entry_{e.chat_id}",
                "range": {"startIndex": 1, "endIndex": end_idx},
            }
        })
        # Heading style on the title line (first newline)
        title_end = 1 + len(e.conversation_title) + 1
        requests.append({
            "updateParagraphStyle": {
                "range": {"startIndex": 1, "endIndex": title_end},
                "paragraphStyle": {"namedStyleType": "HEADING_2"},
                "fields": "namedStyleType",
            }
        })
    return requests
```

**Node body:**

```python
import yaml
from googleapiclient.errors import HttpError


def load_reviewers(path) -> list[str]:
    data = yaml.safe_load(path.read_text())
    return [r["email"] for r in data.get("reviewers", [])]


async def write_to_gdoc_node(state: PortfolioState, deps: Deps) -> PortfolioState:
    groups: dict[tuple[str, str], list[PortfolioEntry]] = {}
    for e in state["generated_entries"]:
        ym = e.generated_at.strftime("%Y-%m")
        groups.setdefault((e.user_email, ym), []).append(e)

    written: list[PortfolioEntry] = []
    errors: list[str] = []
    reviewers = load_reviewers(deps.settings.reviewers_path)

    for (email, ym), entries in groups.items():
        try:
            def _creator(email=email, ym=ym):
                return deps.gdocs.create_doc(
                    title=f"Portfolio - {email} - {ym}",
                    parent_folder_id=deps.settings.portfolio_drive_folder_id,
                )
            doc_id = await deps.ops_repo.get_or_create_doc(
                user_email=email, year_month=ym, creator=_creator,
            )
            try:
                deps.gdocs.share(doc_id, reviewers)
            except HttpError as ex:
                log.warning("write.share_failed", doc_id=doc_id, err=repr(ex))

            requests = build_entry_requests(entries)
            deps.gdocs.batch_update(doc_id, requests)

            for e in entries:
                anchor = f"entry_{e.chat_id}"
                e.google_doc_id = doc_id
                e.entry_anchor_id = anchor
                await deps.ops_repo.record_entry(
                    chat_id=e.chat_id, doc_id=doc_id,
                    anchor_id=anchor, overall_score=e.overall_score,
                )
                written.append(e)
        except Exception as ex:
            errors.append(f"write:{email}/{ym}:{ex!r}")

    return {
        "written_entries": written, "errors": errors,
        "metrics": {**state["metrics"], "written_count": len(written)},
    }
```

### 11.8 Graph wiring (`graphs/portfolio_graph.py`)

```python
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from portfolio_agent.state import PortfolioState
from portfolio_agent.agents.extractor import extract_node, filter_users_node
from portfolio_agent.agents.preprocessor import preprocess_node
from portfolio_agent.agents.analyzer import analyze_node, threshold_gate_node
from portfolio_agent.agents.generator import generate_entries_node
from portfolio_agent.gdocs.portfolio_writer import write_to_gdoc_node


def _curry(fn, deps):
    async def _async_inner(state): return await fn(state, deps)
    def _sync_inner(state): return fn(state, deps)
    import asyncio
    return _async_inner if asyncio.iscoroutinefunction(fn) else _sync_inner


def build_portfolio_graph(deps):
    g = StateGraph(PortfolioState)
    g.add_node("extract", _curry(extract_node, deps))
    g.add_node("filter_users", _curry(filter_users_node, deps))
    g.add_node("preprocess", _curry(preprocess_node, deps))
    g.add_node("analyze", _curry(analyze_node, deps))
    g.add_node("threshold_gate", _curry(threshold_gate_node, deps))
    g.add_node("generate_entries", _curry(generate_entries_node, deps))
    g.add_node("write_to_gdoc", _curry(write_to_gdoc_node, deps))

    g.add_edge(START, "extract")
    g.add_edge("extract", "filter_users")
    g.add_edge("filter_users", "preprocess")
    g.add_edge("preprocess", "analyze")
    g.add_edge("analyze", "threshold_gate")
    g.add_conditional_edges(
        "threshold_gate",
        lambda s: "generate_entries" if s.get("qualifying_chats") else END,
        {"generate_entries": "generate_entries", END: END},
    )
    g.add_edge("generate_entries", "write_to_gdoc")
    g.add_edge("write_to_gdoc", END)

    checkpointer = AsyncPostgresSaver.from_conn_string(deps.settings.operational_db_url)
    return g.compile(checkpointer=checkpointer)
```

---

## 12. FeedbackGraph — Node-by-Node

### 12.1 `fetch_comments_node` (`gdocs/comments_reader.py`)

```python
from datetime import datetime


def resolve_anchor(anchor_blob: str | None,
                   named_ranges: dict[str, dict],
                   entries_in_doc: dict[str, str]) -> tuple[str | None, str | None]:
    """
    Map a Drive comment 'anchor' to (anchor_name, chat_id).
    Drive returns anchors like '{"r":"...","a":[{"kix.r":...}]}' — we extract
    the integer offsets and find the named range whose range overlaps.
    """
    if not anchor_blob:
        return None, None
    # Parse anchor; fall back to None if format unfamiliar.
    try:
        import json
        a = json.loads(anchor_blob)
        offsets = [seg.get("kix.r") or seg.get("kix.s")
                   for seg in a.get("a", []) if isinstance(seg, dict)]
        offsets = [o for o in offsets if isinstance(o, int)]
        if not offsets:
            return None, None
        start = min(offsets); end = max(offsets)
    except Exception:
        return None, None
    for name, group in named_ranges.items():
        for nr in group.get("namedRanges", []):
            for rng in nr.get("ranges", []):
                s, e = rng.get("startIndex", -1), rng.get("endIndex", -1)
                if s <= start and end <= e:
                    return name, entries_in_doc.get(name)
    return None, None


async def fetch_comments_node(state: FeedbackState, deps: Deps) -> FeedbackState:
    since = datetime.fromisoformat(state["since"])
    doc_ids = await deps.ops_repo.list_all_doc_ids()
    all_comments = []
    for doc_id in doc_ids:
        try:
            raw = deps.gdocs.list_comments(doc_id=doc_id, since=since)
            if not raw:
                await deps.ops_repo.update_last_checked(doc_id); continue
            named = deps.gdocs.list_named_ranges(doc_id=doc_id)
            entries = await deps.ops_repo.entries_in_doc(doc_id)
            for r in raw:
                if r.get("resolved"): continue
                if await deps.ops_repo.is_comment_seen(r["id"]): continue
                anchor_id, chat_id = resolve_anchor(r.get("anchor"), named, entries)
                all_comments.append(CommentRecord(
                    comment_id=r["id"],
                    google_doc_id=doc_id,
                    entry_anchor_id=anchor_id, chat_id=chat_id,
                    author_email=r["author"].get("emailAddress", ""),
                    author_name=r["author"].get("displayName", ""),
                    quoted_text=(r.get("quotedFileContent") or {}).get("value"),
                    body=r["content"],
                    created_at=datetime.fromisoformat(
                        r["createdTime"].replace("Z", "+00:00")),
                    resolved=False,
                ))
            await deps.ops_repo.update_last_checked(doc_id)
        except Exception as ex:
            log.warning("fetch_comments.doc_failed", doc_id=doc_id, err=repr(ex))
    return {
        "fetched_comments": all_comments,
        "metrics": {**state.get("metrics", {}), "fetched_count": len(all_comments)},
    }
```

### 12.2 `classify_comments_node` (`agents/feedback.py`)

**Output schema:**

```python
class ClassifyOutput(BaseModel):
    target: FeedbackTarget
    sentiment: Literal["positive", "negative", "neutral", "mixed"]
    rule_ids_touched: list[str]
    suggested_weight_delta: dict[str, float]   # server-clamped to [-0.2, 0.2]
    actionable_takeaway: str = Field(min_length=10, max_length=300)
```

**System prompt — `config/prompts/feedback_system.txt`:**

```
You classify admin feedback on portfolio entries.

A portfolio entry has TWO parts:
1. The verbatim Cursor chat (the conversation).
2. The "Why this matters" prose written by an AI.

Decide:
- target: PROMPT if the comment is about the conversation itself,
          ENTRY_TEXT if it is about the AI-written prose,
          UNKNOWN if you cannot tell.
- sentiment: positive | negative | neutral | mixed.
- rule_ids_touched: subset of the provided rule ids the comment relates to.
  May be empty.
- suggested_weight_delta: per rule, a delta in [-0.2, +0.2]. Use 0 (omit)
  unless the admin clearly signals over- or under-weighting.
- actionable_takeaway: ONE sentence (10-300 chars) the system should learn
  for next time.

Be conservative. Most comments do NOT justify weight changes.
```

**User prompt — `config/prompts/feedback_user.j2`:**

```
Available rules:
{% for r in rules %} - {{ r.id }}: {{ r.name }} (weight {{ r.weight }})
{% endfor %}

Comment metadata:
- author: {{ author_name }} <{{ author_email }}>
- created_at: {{ created_at }}
- attached_to: {{ attached_to_hint }}

quoted_text (what the comment is anchored to):
{{ quoted_text or "(none)" }}

comment body:
{{ body }}
```

**`attached_to_hint`** is computed from the byte ranges of the conversation block vs. the why-it-matters block within the named range. It's a heavy hint to the LLM:
- `"ENTRY_TEXT_block"` if anchor overlaps the why-it-matters block,
- `"PROMPT_block"` if anchor overlaps the conversation block,
- `"UNKNOWN"` otherwise.

To make this hint computable, `render_entry_block` is paired with `entry_block_offsets()` that returns the relative byte ranges of each section; these are persisted with the named range at write time (added to `entry_index` as JSON).

**Node body:**

```python
async def classify_comments_node(state: FeedbackState, deps: Deps) -> FeedbackState:
    rc = load_rules(deps.settings.rules_config_path)
    tasks = [_classify_one(c, rc, deps) for c in state["fetched_comments"]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out, errors = [], []
    for c, res in zip(state["fetched_comments"], results):
        if isinstance(res, Exception):
            errors.append(f"classify:{c.comment_id}:{res!r}")
        else:
            out.append(res)
    return {
        "classified": out, "errors": errors,
        "metrics": {**state.get("metrics", {}), "classified_count": len(out)},
    }


async def _classify_one(c: CommentRecord, rc, deps: Deps) -> ClassifiedFeedback:
    hint = await _attached_to_hint(c, deps)
    raw: ClassifyOutput = await deps.llm_feedback.invoke_structured(
        system=read_text("feedback_system.txt"),
        user=render("feedback_user.j2",
                    rules=rc.rules, author_name=c.author_name,
                    author_email=c.author_email, created_at=c.created_at.isoformat(),
                    attached_to_hint=hint, quoted_text=c.quoted_text, body=c.body),
        schema=ClassifyOutput,
    )
    # Server-side clamp: ignore any rule_id not in current rules, clamp to [-0.2, 0.2].
    valid_ids = {r.id for r in rc.rules}
    deltas = {rid: max(-0.2, min(0.2, d))
              for rid, d in raw.suggested_weight_delta.items() if rid in valid_ids}
    return ClassifiedFeedback(
        comment=c, target=raw.target, sentiment=raw.sentiment,
        rule_ids_touched=[r for r in raw.rule_ids_touched if r in valid_ids],
        suggested_weight_delta=deltas,
        actionable_takeaway=raw.actionable_takeaway,
    )
```

### 12.3 `route_feedback_node` (`agents/feedback.py`)

Routes each classified comment into the appropriate **calibration** table (`analyzer_feedback` / `generator_feedback`). UNKNOWN targets skip the calibration tables but are still captured in the `feedback_log` in the next node.

```python
async def route_feedback_node(state: FeedbackState, deps: Deps) -> FeedbackState:
    for f in state["classified"]:
        c = f.comment
        kwargs = dict(feedback_id=c.comment_id, chat_id=c.chat_id,
                      comment_body=c.body, sentiment=f.sentiment,
                      takeaway=f.actionable_takeaway)
        if f.target == FeedbackTarget.PROMPT:
            await deps.ops_repo.insert_analyzer_feedback(**kwargs)
        elif f.target == FeedbackTarget.ENTRY_TEXT:
            await deps.ops_repo.insert_generator_feedback(**kwargs)
        # UNKNOWN: skips calibration tables; captured in feedback_log below.
    return {}
```

### 12.4 `persist_feedback_node` (`agents/feedback.py`)

All classified comments — regardless of target — are written to `portfolio_agent.feedback_log`. This replaces the former feedback Google Doc and provides a queryable, durable audit trail that operations can inspect via SQL or any BI tool.

```python
async def persist_feedback_node(state: FeedbackState, deps: Deps) -> FeedbackState:
    if not state.get("classified"):
        return {"persisted_to_db": []}

    persisted = []
    for f in state["classified"]:
        c = f.comment
        if await deps.ops_repo.is_comment_seen(c.comment_id):
            continue
        await deps.ops_repo.insert_feedback_log(
            comment_id=c.comment_id,
            doc_id=c.google_doc_id,
            entry_anchor_id=c.entry_anchor_id,
            chat_id=c.chat_id,
            author_email=c.author_email,
            author_name=c.author_name,
            quoted_text=c.quoted_text,
            comment_body=c.body,
            target=f.target.value,
            sentiment=f.sentiment,
            rule_ids_touched=f.rule_ids_touched,
            suggested_deltas=f.suggested_weight_delta,
            actionable_takeaway=f.actionable_takeaway,
            created_at=c.created_at,
        )
        persisted.append(c.comment_id)

    return {"persisted_to_db": persisted}
```

`is_comment_seen` now checks `feedback_log` (primary key on `comment_id`) instead of the old `feedback_seen` table. The `insert_feedback_log` implementation uses `INSERT ... ON CONFLICT DO NOTHING` to be idempotent.

### 12.5 `update_rules_config_node` (`agents/feedback.py`)

```python
from portfolio_agent.rules import apply_weight_deltas, save_rules, load_rules, clamp_weight


async def update_rules_config_node(state: FeedbackState, deps: Deps) -> FeedbackState:
    rc = load_rules(deps.settings.rules_config_path)

    sums: dict[str, float] = {}
    driving: dict[str, list[str]] = {}
    for f in state.get("classified", []):
        for rid, d in f.suggested_weight_delta.items():
            sums[rid] = sums.get(rid, 0.0) + d
            driving.setdefault(rid, []).append(f.comment.comment_id)

    applied: dict[str, float] = {}
    pending: dict[str, float] = {}
    for rid, total in sums.items():
        capped = max(-deps.settings.max_weight_delta_per_run,
                     min(deps.settings.max_weight_delta_per_run, total))
        if abs(total) >= deps.settings.manual_approval_threshold:
            pending[rid] = capped
        else:
            applied[rid] = capped

    for rid, w in pending.items():
        current = next((r.weight for r in rc.rules if r.id == rid), None)
        if current is None: continue
        proposed = clamp_weight(current + w,
                                weight_min=deps.settings.weight_min,
                                weight_max=deps.settings.weight_max)
        await deps.ops_repo.record_pending_change(
            rule_id=rid, current_weight=current,
            proposed_weight=proposed, driving_comment_ids=driving[rid],
        )

    if not applied:
        return {"rules_updated": False, "new_rules_version": None}

    new_rc = apply_weight_deltas(
        rc, applied,
        driving_comment_ids=driving,
        reason=f"Auto-applied from feedback run {state['run_id']}.",
        weight_min=deps.settings.weight_min, weight_max=deps.settings.weight_max,
    )
    save_rules(deps.settings.rules_config_path, new_rc)

    if deps.settings.rules_git_repo_path:
        from portfolio_agent.rules import git_commit  # implemented as a thin subprocess call
        git_commit(deps.settings.rules_git_repo_path,
                   message=f"rules: v{new_rc.version}; deltas={applied}")
    if deps.settings.slack_webhook_url:
        from portfolio_agent.scheduler import notify_slack
        notify_slack(deps.settings.slack_webhook_url,
                     text=f"rules_config v{new_rc.version} applied: {applied}")

    return {"rules_updated": True, "new_rules_version": new_rc.version}
```

### 12.6 Graph wiring (`graphs/feedback_graph.py`)

```python
def build_feedback_graph(deps):
    g = StateGraph(FeedbackState)
    g.add_node("fetch_comments", _curry(fetch_comments_node, deps))
    g.add_node("classify_comments", _curry(classify_comments_node, deps))
    g.add_node("route_feedback", _curry(route_feedback_node, deps))
    g.add_node("persist_feedback", _curry(persist_feedback_node, deps))
    g.add_node("update_rules_config", _curry(update_rules_config_node, deps))

    g.add_edge(START, "fetch_comments")
    g.add_conditional_edges(
        "fetch_comments",
        lambda s: "classify_comments" if s.get("fetched_comments") else END,
        {"classify_comments": "classify_comments", END: END},
    )
    g.add_edge("classify_comments", "route_feedback")
    g.add_edge("route_feedback", "persist_feedback")
    g.add_edge("persist_feedback", "update_rules_config")
    g.add_edge("update_rules_config", END)

    checkpointer = AsyncPostgresSaver.from_conn_string(deps.settings.operational_db_url)
    return g.compile(checkpointer=checkpointer)
```

---

## 13. Scheduler (`scheduler.py`)

```python
import asyncio, structlog
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from portfolio_agent.deps import build_deps
from portfolio_agent.graphs.portfolio_graph import build_portfolio_graph
from portfolio_agent.graphs.feedback_graph import build_feedback_graph

log = structlog.get_logger()


async def main() -> None:
    deps = await build_deps()
    portfolio_graph = build_portfolio_graph(deps)
    feedback_graph = build_feedback_graph(deps)

    scheduler = AsyncIOScheduler(timezone=deps.settings.schedule_timezone)
    scheduler.add_job(
        run_portfolio, "cron",
        hour=deps.settings.portfolio_cron_hour,
        minute=deps.settings.portfolio_cron_minute,
        id="portfolio_daily", misfire_grace_time=3600, coalesce=True,
        kwargs={"graph": portfolio_graph, "deps": deps},
    )
    scheduler.add_job(
        run_feedback, "cron",
        hour=f"*/{deps.settings.feedback_cron_every_hours}",
        id="feedback_sweep", misfire_grace_time=1800, coalesce=True,
        kwargs={"graph": feedback_graph, "deps": deps},
    )
    scheduler.start()
    log.info("scheduler.started")
    await asyncio.Event().wait()


async def run_portfolio(graph, deps):
    run_id = str(uuid4())
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=deps.settings.portfolio_window_hours)
    state = {"run_id": run_id, "window_start": start.isoformat(),
             "window_end": now.isoformat(), "metrics": {}}
    final = await graph.ainvoke(state, config={"configurable": {"thread_id": run_id}})
    log.info("portfolio.run.done", run_id=run_id, metrics=final.get("metrics"))


async def run_feedback(graph, deps):
    run_id = str(uuid4())
    since = (datetime.now(timezone.utc)
             - timedelta(hours=deps.settings.feedback_cron_every_hours + 1))
    state = {"run_id": run_id, "since": since.isoformat(), "metrics": {}}
    final = await graph.ainvoke(state, config={"configurable": {"thread_id": run_id}})
    log.info("feedback.run.done", run_id=run_id, metrics=final.get("metrics"))


def notify_slack(webhook_url: str, *, text: str) -> None:
    import httpx
    try:
        httpx.post(webhook_url, json={"text": text}, timeout=5.0)
    except Exception:
        log.warning("slack.notify_failed")


if __name__ == "__main__":
    asyncio.run(main())
```

---

## 14. CLI (`cli.py`)

```bash
portfolio-agent run-portfolio --start 2026-05-10T00:00:00Z --end 2026-05-11T00:00:00Z
portfolio-agent run-feedback --since 2026-05-15T00:00:00Z
portfolio-agent dry-run --chat-id abc-123    # extract→analyze only; prints report
portfolio-agent validate-rules
portfolio-agent show-config                  # redacted settings
portfolio-agent approve-pending --id 17 --as alice@company.com
```

Implemented with `typer`. Each command calls `build_deps()`, invokes the relevant graph or repo method, prints a JSON summary, and exits non-zero on failure.

---

## 15. Secret Scrubbing (`secrets_scrub.py`)

Applied to every message **before** any LLM call.

```python
import re

PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:AWS_ACCESS_KEY]"),
    (re.compile(r"(?i)aws.{0,20}secret.{0,3}['\"]([0-9a-zA-Z/+=]{40})['\"]"),
     "[REDACTED:AWS_SECRET]"),
    (re.compile(
        r"-----BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY-----[\s\S]+?-----END[^-]+-----"),
     "[REDACTED:PRIVATE_KEY]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
     "[REDACTED:JWT]"),
    (re.compile(r"ghp_[A-Za-z0-9]{36,}"), "[REDACTED:GITHUB_TOKEN]"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED:OPENAI_KEY]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED:SLACK_TOKEN]"),
]


def scrub(text: str) -> str:
    for pat, repl in PATTERNS:
        text = pat.sub(repl, text)
    return text
```

Called in `_preprocess_one` (raw chat messages) and again on the rendered `chat_text` in `_analyze_one`/`_generate_one` (defense in depth).

---

## 16. Prompts loader (`prompts.py`)

```python
from pathlib import Path
from functools import lru_cache
from jinja2 import Environment, FileSystemLoader, select_autoescape
from portfolio_agent.settings import get_settings


@lru_cache(maxsize=None)
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(get_settings().prompts_dir)),
        autoescape=select_autoescape(disabled_extensions=("j2", "txt")),
        keep_trailing_newline=True, trim_blocks=False, lstrip_blocks=False,
    )


def render(template_name: str, **ctx) -> str:
    return _env().get_template(template_name).render(**ctx)


def read_text(filename: str) -> str:
    return (get_settings().prompts_dir / filename).read_text()
```

---

## 17. Observability

### Logging

`structlog` with JSON renderer in prod, key-value in dev. Required fields on every event: `run_id`, `node`, `chat_id` (where applicable), `duration_ms`, `model`, `tokens_in`, `tokens_out`.

### Metrics in state

Every node appends to `state["metrics"]`. Final dict is logged at run end (and posted to Slack if configured).

### Optional: LangSmith

If `PA_LANGSMITH_API_KEY` is set, the scheduler exports `LANGCHAIN_TRACING_V2=true` and `LANGCHAIN_API_KEY=$PA_LANGSMITH_API_KEY` before building graphs.

### Slack daily summary

After portfolio run:
```
Portfolio run abc-123 complete.
• Extracted: 482   • After user filter: 431
• After preprocess: 408   • Analyzed: 408
• Qualifying (≥60): 79   • Entries written: 79
• Errors: 2   • Tokens: 1.4M in / 312k out
```

---

## 18. Testing Strategy

### Unit tests (`tests/unit/`)

| File | Coverage |
|---|---|
| `test_preprocessor.py` | deterministic filter cases (empty, filler, code blocks, mixed case). LLM mocked. |
| `test_analyzer.py` | score clamping; missing-rule injection; weighted-avg math. |
| `test_threshold.py` | edge cases exactly at threshold. |
| `test_generator.py` | top-3 rule sorting; citation formatting. |
| `test_rules.py` | yaml round-trip; `apply_weight_deltas`; clamping; version bump. |
| `test_secrets_scrub.py` | every regex against positive/negative cases. |
| `test_feedback_aggregation.py` | sums + clamps + pending-vs-applied split. |
| `test_anchor_resolve.py` | anchor → named_range → chat_id mapping. |
| `test_render_entry_block.py` | snapshot. |
| `test_render_feedback_block.py` | removed (no feedback doc rendered). |

### Integration tests (`tests/integration/`)

- `test_portfolio_e2e.py`: docker-compose Postgres + `respx` OpenAI cassette + `httpx.MockTransport` fake Google Docs. Loads 10 fixture chats, runs the graph, asserts counts and rows.
- `test_feedback_e2e.py`: seeds entries + comments, runs feedback graph, asserts `feedback_log` row count, calibration rows, `rules_config.yaml` diff.

### Property tests (`hypothesis`)

- `overall_score` ∈ [10, 100] for any valid `RuleScore` list.
- After `apply_weight_deltas`, all weights ∈ [weight_min, weight_max].
- Per-run net magnitude on any rule ≤ `max_weight_delta_per_run`.

---

## 19. Concurrency & Failure Model

| Boundary | Concurrency | Retry |
|---|---|---|
| OpenAI calls | `LLMClient._sem`, default 8 per model | tenacity 4× on `RateLimitError`/`APIError`/`APITimeoutError`, exp jitter |
| Postgres reads | SQLAlchemy pool size 10 | none at call-site (pool reconnects) |
| Google Docs writes | sequential per doc, parallel across docs | tenacity 5× on `HttpError` 429/5xx |
| LangGraph checkpointing | `AsyncPostgresSaver` | resumes from last checkpoint on same `thread_id` |

Top level: unhandled exceptions in a node are persisted to the checkpoint and surfaced to the scheduler, which logs and lets the next cron tick re-run; `coalesce=True` prevents pile-up.

---

## 20. Security

- All secrets in env (`PA_*`). `.env.example` committed with empty values.
- Google: **Per-user OAuth2**. Each user runs `portfolio-agent auth-user --email USER` once; the interactive consent flow stores their token JSON in `portfolio_agent.user_google_config`. On each run the writer fetches the token from DB, auto-refreshes if expired, writes to the user's own Drive folder, and persists the updated token back. One `client_secret.json` is shared server-side. No service account, no shared folder, no `reviewers.yaml`.
- Secret scrubbing applied before every LLM call.
- `rules_config.yaml` writes are atomic (`tempfile + os.replace`) and optionally git-committed.
- Checkpoint table contains chat content — encrypt at rest like the source DB.
- `google_token_json` contains OAuth refresh tokens — encrypt the `user_google_config` column at rest; never log it.

---

## 21. Rollout Phases

| Phase | Scope | Gate to next |
|---|---|---|
| 1 | extract → preprocess → analyze. Local JSON output. No Google writes. | Admin spot-check on 50 chats. |
| 2 | + `generate_entries` + `write_to_gdoc`. `dry-run` CLI on one test user. | Reviewer confirms format. |
| 3 | Full portfolio cron. Feedback graph in **read-only** mode (`update_rules_config` is a no-op). | One week of feedback collected; deltas look sane. |
| 4 | Enable `update_rules_config` with default guardrails. | Two weeks of clean auto-updates. |
| 5 | Lower `manual_approval_threshold` to 0.5 (almost always auto). Or keep current guardrails. | n/a |

---

## 22. Out of Scope for v1

- Fine-tuning. Learning is via few-shot calibration only.
- Auto-creating or auto-deleting rules. Weight nudges only.
- Per-user dashboards.
- Real-time / event-driven processing.
- Multi-tenant isolation.
- Auto-tuning the threshold.

---

## 23. Definition of Done

1. A Cursor chat created today is in a Google Doc by tomorrow 18:05 local if its score ≥ threshold.
2. A reviewer comment posted now is reflected as a row in `portfolio_agent.feedback_log` within `feedback_cron_every_hours + 1` hours.
3. Consistent reviewer feedback signaling over/under-weighting moves that weight within 1–2 feedback runs (subject to clamps and manual approval).
4. `rules_config.yaml` git history shows each version bump with driving comment IDs.
5. Re-running yesterday's portfolio with the same `rules_version` and seeded LLM produces byte-identical `overall_score` values (deterministic Python math).
6. All unit, integration, and property tests pass in CI.
7. `portfolio-agent validate-rules` exits 0.

---

## 24. Implementation Order

1. `settings.py`, `types.py`, `state.py`, `rules.py` + starter `rules_config.yaml`.
2. `db/engine.py`, `db/users.py`, `db/cursor_chats.py`, Alembic migration.
3. `db/operational.py`.
4. `secrets_scrub.py` + tests.
5. `llm.py` + tests with mocked OpenAI (`respx`).
6. `agents/preprocessor.py` + unit tests.
7. `agents/analyzer.py` + unit tests.
8. `agents/generator.py` + unit tests.
9. `gdocs/auth.py` (OAuth2 token flow), `gdocs/client.py`.
10. `gdocs/portfolio_writer.py` + integration test against fake Docs server.
11. `graphs/portfolio_graph.py` + e2e test.
12. `gdocs/comments_reader.py`.
13. `agents/feedback.py` (`route_feedback_node`, `persist_feedback_node`, `update_rules_config_node`) + tests.
14. `graphs/feedback_graph.py` + e2e test.
15. `scheduler.py`, `cli.py`.
16. Docker + CI.

Estimate: ~5–6 engineer-weeks for v1.

---

## 25. Open Items (must close before code starts)

- **SCHEMA_TODO** — confirm the real Cursor chat row shape and update §4.1 `ChatRecord` + SQL in §10.2.
- Confirm `users` table column names (`is_active`, `email`, `role`).
- Confirm Drive folder structure: one folder for all user-month docs, or sub-folders per user?
- Confirm reviewer list source: `config/reviewers.yaml` vs Google Group.
- Confirm exact OpenAI model names at implementation time (`gpt-4.1` vs `gpt-4o` vs newer).
- **OAuth2 first-run flow** — `token.json` must be generated via a one-time interactive login (`InstalledAppFlow.run_local_server`). Decide where/how this bootstrap step happens in the deployment runbook (developer machine vs CI vs production server).
