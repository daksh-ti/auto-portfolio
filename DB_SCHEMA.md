# Portfolio Agent — Database Schema Reference

Hi! This document covers everything you need to know about the Postgres database
that the portfolio agent reads from and writes to. Please reach out if any column
names in the source tables differ from what's listed here — there's one file to
update and it's called out explicitly below.

---

## Connection

Both the source data and the agent's operational tables live in the **same Neon
Postgres instance**. The agent connects via two env vars that currently point to
the same DB:

| Env var | Purpose |
|---|---|
| `PA_SOURCE_DB_URL` | Reads `public.cursor_chats` and `public.users` |
| `PA_OPERATIONAL_DB_URL` | Reads/writes everything under the `portfolio_agent` schema |

Connection string format:
```
postgresql+psycopg://USER:PASSWORD@HOST/DBNAME?sslmode=require
```

---

## Part 1 — Source Tables (your responsibility)

> 🔴 **YOU OWN THESE.** Create the tables, run the migrations, and write the
> data pipeline that keeps them populated. The agent only ever **reads** from
> them — it will never insert, update, or delete a row.

### `public.users` — 🔴 YOUR TABLE

Stores the list of people whose Cursor chats should be evaluated.

```sql
CREATE TABLE public.users (
    user_id      TEXT    PRIMARY KEY,
    email        TEXT    NOT NULL,          -- used to look up Google config + send emails
    display_name TEXT,
    role         TEXT,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE
);
```

| Column | Notes |
|---|---|
| `user_id` | Arbitrary unique key. Must match `cursor_chats.user_id`. |
| `email` | **Required.** The agent groups portfolio entries and sends email notifications to this address. Must be a deliverable email. |
| `is_active` | Only rows where `is_active = TRUE` are processed. Set to `FALSE` to exclude a user without deleting them. |
| `display_name` | Optional. Not currently used by the agent but useful for future UI. |
| `role` | Optional. Not currently used. |

### `public.cursor_chats` — 🔴 YOUR TABLE

Stores the raw Cursor chat sessions.

```sql
CREATE TABLE public.cursor_chats (
    chat_id        TEXT        PRIMARY KEY,
    user_id        TEXT        NOT NULL REFERENCES public.users(user_id),
    project_id     TEXT,
    started_at     TIMESTAMPTZ NOT NULL,
    ended_at       TIMESTAMPTZ,
    messages_jsonb JSONB       NOT NULL,
    metadata_jsonb JSONB
);

CREATE INDEX ON public.cursor_chats(user_id, started_at);
```

| Column | Notes |
|---|---|
| `chat_id` | Arbitrary unique key (e.g. Cursor's internal chat UUID). |
| `user_id` | FK to `public.users.user_id`. |
| `project_id` | Optional. Not used for scoring but stored for provenance. |
| `started_at` | **Required.** The agent filters chats by this column. Must be UTC. |
| `ended_at` | Optional. |
| `messages_jsonb` | **Required.** Array of message objects (see format below). |
| `metadata_jsonb` | Optional. Any extra metadata you want to store alongside the chat. |

#### `messages_jsonb` format

```json
[
  {
    "role": "user",
    "content": "How do I add rate limiting to a FastAPI service?",
    "timestamp": "2026-05-19T09:01:00Z"
  },
  {
    "role": "assistant",
    "content": "There are two main approaches — in-memory via slowapi or Redis-backed...",
    "timestamp": "2026-05-19T09:01:05Z"
  }
]
```

- `role`: `"user"` or `"assistant"` (required)
- `content`: the message text (required)
- `timestamp`: ISO8601 UTC string (optional but useful)

#### ⚠️ One place to update if your column names differ

If your real table uses different column names (e.g. `created_at` instead of
`started_at`, or `body` instead of `messages_jsonb`), update **this one file**:

```
portfolio_agent/src/portfolio_agent/db/cursor_chats.py
```

The SQL query is at line 34. Column aliases mean the rest of the codebase never
needs to change.

---

## Part 2 — Operational Tables (agent-managed)

> **Colour key for this section:**
> - 🟢 Fully agent-managed — created by migration, written by the agent pipeline
> - 🔵 Created by migration, **written by the frontend** (OAuth flow), read+refreshed by the agent
>
> 🟢 **THE AGENT OWNS THESE.** You only need to run one command once and they
> are created automatically:
>
> ```bash
> cd portfolio_agent
> alembic upgrade head
> ```
>
> After that the agent populates and maintains all of them. They are documented
> here purely for observability — feel free to read/query them, but **do not
> write to them manually** unless explicitly noted.

---

### `portfolio_agent.gdoc_index` — 🟢 AGENT-MANAGED

Tracks which Google Doc holds which user's portfolio entries for a given month.

```sql
CREATE TABLE portfolio_agent.gdoc_index (
    user_email      TEXT        NOT NULL,
    year_month      TEXT        NOT NULL,   -- e.g. '2026-05'
    doc_id          TEXT        NOT NULL,   -- Google Docs document ID
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_checked_at TIMESTAMPTZ,
    PRIMARY KEY (user_email, year_month)
);
```

---

### `portfolio_agent.entry_index` — 🟢 AGENT-MANAGED

One row per portfolio entry written to a Google Doc. Used to map Drive comment
anchors back to the originating chat.

```sql
CREATE TABLE portfolio_agent.entry_index (
    chat_id       TEXT PRIMARY KEY,
    doc_id        TEXT        NOT NULL,
    anchor_id     TEXT        NOT NULL,   -- named range name, e.g. 'entry_chat-001'
    overall_score INT         NOT NULL,
    written_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON portfolio_agent.entry_index(doc_id);
```

---

### `portfolio_agent.analysis_runs` — 🟢 AGENT-MANAGED

Stores the scoring result for every chat × run combination. Useful for tracking
how scores change as the rules config evolves.

```sql
CREATE TABLE portfolio_agent.analysis_runs (
    chat_id           TEXT        NOT NULL,
    run_id            TEXT        NOT NULL,
    rules_version     INT         NOT NULL,
    rule_scores_jsonb JSONB       NOT NULL,
    overall_score     INT         NOT NULL,
    analyzed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, run_id)
);
```

`rule_scores_jsonb` shape:
```json
[
  { "rule_id": "culture.curiosity", "rule_name": "Curiosity", "score": 82, "weight": 1.5, "justification": "..." },
  ...
]
```

---

### `portfolio_agent.feedback_log` — 🟢 AGENT-MANAGED

The primary audit trail for every classified Google Doc comment. Human-readable
via SQL; replaces the old "feedback Google Doc" concept.

```sql
CREATE TABLE portfolio_agent.feedback_log (
    comment_id          TEXT PRIMARY KEY,
    doc_id              TEXT        NOT NULL,
    entry_anchor_id     TEXT,               -- null for doc-level comments
    chat_id             TEXT,               -- null if anchor couldn't be resolved
    author_email        TEXT        NOT NULL,
    author_name         TEXT        NOT NULL,
    quoted_text         TEXT,               -- text the comment is anchored to
    comment_body        TEXT        NOT NULL,
    target              TEXT        NOT NULL,   -- 'prompt' | 'entry_text' | 'unknown'
    sentiment           TEXT        NOT NULL,   -- 'positive' | 'negative' | 'neutral' | 'mixed'
    rule_ids_touched    JSONB       NOT NULL DEFAULT '[]',
    suggested_deltas    JSONB       NOT NULL DEFAULT '{}',
    actionable_takeaway TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL,
    seen_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Useful queries:
```sql
-- All feedback for a specific user's doc
SELECT * FROM portfolio_agent.feedback_log
WHERE doc_id = '<google-doc-id>'
ORDER BY created_at DESC;

-- Negative feedback that touched a specific rule
SELECT comment_body, actionable_takeaway
FROM portfolio_agent.feedback_log
WHERE sentiment = 'negative'
  AND rule_ids_touched ? 'culture.ownership';
```

---

### `portfolio_agent.analyzer_feedback` and `portfolio_agent.generator_feedback` — 🟢 AGENT-MANAGED

Calibration tables. The top 5 most recent rows from each are injected into the
LLM prompt on every portfolio run, so the model learns from past feedback.

```sql
-- Both tables have identical structure
CREATE TABLE portfolio_agent.analyzer_feedback (   -- comments about the chat itself
    feedback_id  TEXT PRIMARY KEY,
    chat_id      TEXT,
    comment_body TEXT        NOT NULL,
    sentiment    TEXT        NOT NULL,
    takeaway     TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE portfolio_agent.generator_feedback (  -- comments about the AI-written prose
    feedback_id  TEXT PRIMARY KEY,
    chat_id      TEXT,
    comment_body TEXT        NOT NULL,
    sentiment    TEXT        NOT NULL,
    takeaway     TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

### `portfolio_agent.rules_pending_changes` — 🟢 AGENT-MANAGED

Rule weight changes that exceeded the auto-apply threshold and require manual
approval before being written to `rules_config.yaml`.

```sql
CREATE TABLE portfolio_agent.rules_pending_changes (
    id                  SERIAL      PRIMARY KEY,
    proposed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    rule_id             TEXT        NOT NULL,
    current_weight      REAL        NOT NULL,
    proposed_weight     REAL        NOT NULL,
    driving_comment_ids JSONB       NOT NULL,   -- array of comment_ids that drove this change
    approved_by         TEXT,                   -- email of approver, null until approved
    applied             BOOLEAN     NOT NULL DEFAULT FALSE
);
```

To approve a pending change:
```bash
portfolio-agent approve-pending --id <id> --as your@email.com
```

---

### `portfolio_agent.user_google_config` — 🔵 FRONTEND WRITES, AGENT READS/REFRESHES

Stores per-user Google OAuth tokens and the Drive folder ID where their portfolio
docs will be created.

```sql
CREATE TABLE portfolio_agent.user_google_config (
    user_email        TEXT        PRIMARY KEY,
    google_folder_id  TEXT        NOT NULL,   -- Google Drive folder ID for this user's docs
    google_token_json TEXT,                   -- OAuth2 token JSON blob (treat as secret)
    configured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    token_updated_at  TIMESTAMPTZ
);
```

**Who writes this table:**

| Actor | What it does |
|---|---|
| **Frontend** | Runs the Google OAuth2 consent flow, receives the token, writes the initial row (or upserts on re-auth) |
| **Agent** | Reads `google_token_json` on every run, auto-refreshes the token if expired, and writes the refreshed token back via `UPDATE` |
| CLI `auth-user` | Dev/admin shortcut that does the same thing as the frontend flow — only for local testing, not for production use |

**What the frontend needs to write:**

```json
{
  "user_email":        "daksh.sangal@trilogy.com",
  "google_folder_id":  "<the Drive folder ID the user picked or you created for them>",
  "google_token_json": "<JSON string from google-auth-oauthlib Credentials.to_json()>"
}
```

`google_token_json` is a JSON string that looks like:
```json
{
  "token":         "<access_token>",
  "refresh_token": "<refresh_token>",
  "token_uri":     "https://oauth2.googleapis.com/token",
  "client_id":     "<client_id>",
  "client_secret": "<client_secret>",
  "scopes":        ["https://www.googleapis.com/auth/documents", "https://www.googleapis.com/auth/drive"]
}
```

The OAuth app (`client_id` / `client_secret`) is the same shared server-side
credential (`PA_GOOGLE_CLIENT_SECRETS_PATH`) — users authenticate to their own
Google accounts through it, but the app registration is yours.

> ⚠️ `google_token_json` contains a live OAuth2 refresh token. Treat it like a
> password — encrypt at rest, never log it, never expose it in API responses.

---

## Summary

| Table | Schema | Owner | Action required from you |
|---|---|---|---|
| `users` | `public` | 🔴 **You** | Create table + populate with user records |
| `cursor_chats` | `public` | 🔴 **You** | Create table + push chat sessions from Cursor |
| `gdoc_index` | `portfolio_agent` | 🟢 Agent | Nothing — auto-created by `alembic upgrade head` |
| `entry_index` | `portfolio_agent` | 🟢 Agent | Nothing |
| `analysis_runs` | `portfolio_agent` | 🟢 Agent | Nothing |
| `feedback_log` | `portfolio_agent` | 🟢 Agent | Nothing (read-only for dashboards/queries) |
| `analyzer_feedback` | `portfolio_agent` | 🟢 Agent | Nothing |
| `generator_feedback` | `portfolio_agent` | 🟢 Agent | Nothing |
| `rules_pending_changes` | `portfolio_agent` | 🟢 Agent | Nothing (approve via CLI if changes are queued) |
| `user_google_config` | `portfolio_agent` | 🔵 Frontend | Frontend writes on OAuth completion; agent refreshes token on each run |

**TL;DR — your checklist:**
1. ✅ Create `public.users` and `public.cursor_chats` on the shared Postgres instance
2. ✅ Populate `users` with at least one row per person (`is_active = true`, real `email`)
3. ✅ Push Cursor chat sessions into `cursor_chats` with `messages_jsonb` in the expected format (see above)
4. ✅ Share the DB connection string so it can be added to `PA_SOURCE_DB_URL`
5. ✅ *(Frontend team)* On Google OAuth callback: write `user_email`, `google_folder_id`, and `google_token_json` to `portfolio_agent.user_google_config`

Everything else is handled automatically by the agent.
