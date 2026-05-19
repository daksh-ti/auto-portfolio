# Auto Portfolio Agent

An autonomous agent that reads Cursor chat sessions from a shared Postgres
database, evaluates each conversation against a configurable set of engineering
culture rules, and writes qualifying entries into each engineer's personal Google
Doc portfolio. Comments left on those documents are periodically fetched,
classified by an LLM, and used to automatically calibrate the scoring rules over
time.

---

## What it does

Engineers write code with AI assistants every day. Most of that thinking — the
tradeoffs explored, the root causes uncovered, the edge cases caught — disappears
the moment the chat window closes. This agent captures it automatically.

**Portfolio pipeline** (runs daily at 18:00 IST by default)

1. Fetches all Cursor chat sessions from the database within a configurable time
   window.
2. Filters out inactive users and low-signal conversations (filler messages,
   very short exchanges).
3. Scores each conversation against a set of culture rules — curiosity, ownership,
   engineering rigour, communication — using an LLM.
4. Generates a "Why this matters" narrative for each qualifying conversation.
5. Writes the entries to the engineer's personal Google Doc, grouped by month.
6. Sends the engineer an email digest summarising what was added and why.

**Feedback pipeline** (runs every 6 hours by default)

1. Reads unresolved comments from every known portfolio doc.
2. Classifies each comment: does it relate to the original conversation or to
   the AI-written prose? What is the sentiment? Which rules does it touch?
3. Persists all classified feedback to the database as a queryable audit trail.
4. Applies small, safe weight adjustments to the culture rules automatically.
   Larger adjustments are queued for manual approval.
5. Sends the commenter an email digest explaining how their feedback was
   interpreted and what (if anything) changed.

The scoring rules evolve: consistent reviewer feedback shifts rule weights within
configurable guardrails, closing the loop between human judgment and automated
scoring.

---

## Architecture

```
Cursor chats DB (Postgres)
        |
        v
   PortfolioGraph (LangGraph)
        |-- extract_node         reads cursor_chats + users
        |-- filter_users_node    drops inactive users
        |-- preprocess_node      LLM: filter low-signal, extract summary
        |-- analyze_node         LLM: score against culture rules
        |-- threshold_gate_node  drop chats below score threshold
        |-- generate_entries_node LLM: write "Why this matters" prose
        |-- write_to_gdoc_node   writes to Google Doc, sends email
        |
   FeedbackGraph (LangGraph)
        |-- fetch_comments_node   reads Drive comments via per-user OAuth
        |-- classify_comments_node LLM: target, sentiment, weight deltas
        |-- route_feedback_node   writes to calibration tables
        |-- persist_feedback_node writes to feedback_log audit table
        |-- update_rules_config_node applies deltas, sends email
        |
   APScheduler  (runs both graphs on cron)
   PostgreSQL   (Neon or any Postgres; stores operational state)
   Google Drive (one personal folder + doc per user per month)
```

---

## Prerequisites

- Docker and Docker Compose v2
- A Postgres database (Neon free tier works; connection string with SSL)
- An OpenAI API key (GPT-4.1 / GPT-4.1-mini)
- A Google Cloud project with the Docs and Drive APIs enabled, and an OAuth 2.0
  Desktop client credential JSON downloaded
- A Gmail account (or any SMTP-accessible account) for outbound email

---

## Quick start

### 1. Clone the repository

```bash
git clone https://github.com/daksh-ti/auto-portfolio.git
cd auto-portfolio
```

### 2. Place secrets

Copy the Google OAuth client credential file into the `portfolio_agent/`
directory:

```bash
cp /path/to/your/client_secret.json portfolio_agent/client_secret.json
```

This file is listed in `.gitignore` and will never be committed.

### 3. Configure environment variables

```bash
cp portfolio_agent/.env.example portfolio_agent/.env
```

Open `portfolio_agent/.env` and fill in every value:

```
# OpenAI
PA_OPENAI_API_KEY=sk-...

# Postgres — same instance, same DB for source and operational tables
PA_SOURCE_DB_URL=postgresql+psycopg://user:pass@host/dbname?sslmode=require
PA_OPERATIONAL_DB_URL=postgresql+psycopg://user:pass@host/dbname?sslmode=require

# Google OAuth (path is relative to the container working directory)
PA_GOOGLE_CLIENT_SECRETS_PATH=./client_secret.json

# Email notifications
PA_NOTIFICATIONS_ENABLED=true
PA_SMTP_HOST=smtp.gmail.com
PA_SMTP_PORT=587
PA_SMTP_USER=you@gmail.com
PA_SMTP_PASSWORD=<16-char Gmail App Password>
PA_EMAIL_FROM=Portfolio Agent <you@gmail.com>
```

See the full settings reference at the end of this document for all available
variables.

### 4. Set the host path for compose

The root `.env` tells Docker Compose where to find the client secret on your
host machine. The default value points to `portfolio_agent/client_secret.json`
which is where you placed it in step 2, so no change is needed unless you placed
the file elsewhere:

```bash
# root .env (already correct by default)
HOST_GOOGLE_CLIENT_SECRETS_PATH=./portfolio_agent/client_secret.json
```

### 5. Set up source tables

Before the agent can run, your database needs a `public.users` table and a
`public.cursor_chats` table populated with data. See `DB_SCHEMA.md` for the
exact schema and column requirements.

### 6. Authenticate each user with Google

The agent writes to each user's personal Google Drive folder. Each user must
complete a one-time OAuth consent flow so their token can be stored in the
database. Run this for each user:

```bash
docker compose run --rm portfolio-agent portfolio-agent auth-user \
  --email user@company.com \
  --folder-id <their-google-drive-folder-id>
```

A URL will be printed. Open it in a browser, sign in as the user, and click
Allow. The token is stored automatically.

In production this step is handled by the frontend OAuth flow — see `DB_SCHEMA.md`
for the exact row the frontend must write to `portfolio_agent.user_google_config`.

### 7. Start the agent

```bash
docker compose up -d
```

The container will:
1. Run `alembic upgrade head` to create all operational tables (safe to run on
   every start — migrations are idempotent).
2. Start the APScheduler loop with both cron jobs active.

Check logs:

```bash
docker compose logs -f
```

Expected output on a healthy start:

```
[entrypoint] Running database migrations...
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
[entrypoint] Migrations complete.
scheduler.started  portfolio_cron=18:00  feedback_every_h=6  tz=Asia/Kolkata
```

---

## Manual pipeline runs

You can trigger either pipeline on demand without waiting for the cron schedule.
Both commands run inside the already-running container and share its environment.

```bash
# Run the portfolio pipeline over an explicit time window
docker compose exec portfolio-agent portfolio-agent run-portfolio \
  --start 2026-05-19T00:00:00Z \
  --end   2026-05-20T00:00:00Z

# Run the portfolio pipeline over the last portfolio_window_hours (default: 24)
docker compose exec portfolio-agent portfolio-agent run-portfolio-now

# Run the feedback pipeline from a given timestamp
docker compose exec portfolio-agent portfolio-agent run-feedback \
  --since 2026-05-18T00:00:00Z

# Analyse a single chat without writing anything (dry run)
docker compose exec portfolio-agent portfolio-agent dry-run --chat-id <chat-id>

# Validate the rules config file
docker compose exec portfolio-agent portfolio-agent validate-rules

# Print current settings with secrets redacted
docker compose exec portfolio-agent portfolio-agent show-config

# Approve a pending rule weight change that exceeded the auto-apply threshold
docker compose exec portfolio-agent portfolio-agent approve-pending \
  --id 7 --as reviewer@company.com
```

---

## Updating culture rules

Rules are stored in `portfolio_agent/config/rules_config.yaml`. The file is
mounted as a Docker volume so changes made by the agent (automatic weight
adjustments) persist across container restarts.

To add, remove, or manually edit a rule, edit the file directly and restart the
container. To change the scoring threshold, update the `threshold` field (0–100).

---

## Settings reference

All settings are read from environment variables with the `PA_` prefix.

| Variable | Default | Description |
|---|---|---|
| `PA_OPENAI_API_KEY` | required | OpenAI API key |
| `PA_OPENAI_MODEL_ANALYZE` | `gpt-4.1` | Model for scoring |
| `PA_OPENAI_MODEL_GENERATE` | `gpt-4.1` | Model for entry generation |
| `PA_OPENAI_MODEL_PREPROCESS` | `gpt-4.1-mini` | Model for preprocessing |
| `PA_OPENAI_MODEL_FEEDBACK` | `gpt-4.1` | Model for comment classification |
| `PA_OPENAI_MAX_CONCURRENCY` | `8` | Max parallel LLM calls |
| `PA_SOURCE_DB_URL` | required | Postgres URL for `cursor_chats` + `users` tables |
| `PA_OPERATIONAL_DB_URL` | required | Postgres URL for `portfolio_agent.*` tables |
| `PA_GOOGLE_CLIENT_SECRETS_PATH` | required | Path to OAuth 2.0 client secret JSON |
| `PA_NOTIFICATIONS_ENABLED` | `false` | Master switch for email notifications |
| `PA_SMTP_HOST` | `smtp.gmail.com` | SMTP server hostname |
| `PA_SMTP_PORT` | `587` | SMTP port (STARTTLS) |
| `PA_SMTP_USER` | — | SMTP login username |
| `PA_SMTP_PASSWORD` | — | SMTP login password or app password |
| `PA_EMAIL_FROM` | — | From address, e.g. `Agent <you@gmail.com>` |
| `PA_SCHEDULE_TIMEZONE` | `Asia/Kolkata` | Timezone for cron schedules |
| `PA_PORTFOLIO_CRON_HOUR` | `18` | Hour to run the portfolio pipeline |
| `PA_PORTFOLIO_CRON_MINUTE` | `0` | Minute to run the portfolio pipeline |
| `PA_FEEDBACK_CRON_EVERY_HOURS` | `6` | Feedback sweep interval in hours |
| `PA_PORTFOLIO_WINDOW_HOURS` | `24` | How many hours back the portfolio pipeline looks |
| `PA_RULES_CONFIG_PATH` | `config/rules_config.yaml` | Path to the rules YAML |
| `PA_PROMPTS_DIR` | `config/prompts` | Directory containing prompt templates |
| `PA_MAX_CHATS_PER_RUN` | `5000` | Safety cap on chats fetched per run |
| `PA_MAX_WEIGHT_DELTA_PER_RUN` | `0.15` | Max rule weight change per feedback run |
| `PA_MANUAL_APPROVAL_THRESHOLD` | `0.30` | Cumulative delta above which approval is required |
| `PA_WEIGHT_MIN` | `0.1` | Minimum allowed rule weight |
| `PA_WEIGHT_MAX` | `3.0` | Maximum allowed rule weight |
| `PA_SLACK_WEBHOOK_URL` | — | Optional Slack webhook for run summaries |
| `PA_LANGSMITH_API_KEY` | — | Optional LangSmith API key for tracing |
| `PA_RULES_GIT_REPO_PATH` | — | Optional path; if set, rule updates are git-committed |

---

## Project structure

```
auto_portfolio/
├── docker-compose.yml          Compose file for production deployment
├── .env                        Root compose env (HOST_GOOGLE_CLIENT_SECRETS_PATH)
├── DB_SCHEMA.md                Database schema reference for the data team
├── portfolio_agent/
│   ├── Dockerfile              Multi-stage production image
│   ├── docker/
│   │   └── entrypoint.sh       Runs migrations then starts the scheduler
│   ├── pyproject.toml          Python package definition and dependencies
│   ├── alembic.ini             Alembic migration configuration
│   ├── migrations/             Alembic migration scripts
│   ├── config/
│   │   ├── rules_config.yaml   Culture rules and scoring threshold
│   │   └── prompts/            Jinja2 and plain-text LLM prompt templates
│   ├── src/portfolio_agent/
│   │   ├── agents/             LangGraph node implementations
│   │   ├── db/                 Database repositories
│   │   ├── gdocs/              Google Docs/Drive client and OAuth helpers
│   │   ├── graphs/             LangGraph graph definitions
│   │   ├── cli.py              Typer CLI entry point
│   │   ├── scheduler.py        APScheduler loop
│   │   ├── notifications.py    Email digest sender
│   │   ├── rules.py            Rules config loader, diff, and weight logic
│   │   ├── settings.py         Pydantic settings (reads PA_* env vars)
│   │   └── types.py            Shared Pydantic models
│   ├── .env.example            Template for portfolio_agent/.env
│   └── client_secret.json      Google OAuth credentials (gitignored)
└── tests/                      Unit and integration tests (in progress)
```

---

## Database

The agent connects to a single Postgres instance but uses two logical layers:

- `public.users` and `public.cursor_chats` — owned and populated by your data
  pipeline. The agent only reads from these.
- `portfolio_agent.*` — owned by the agent. Created automatically on first
  startup via `alembic upgrade head`.

See `DB_SCHEMA.md` for the complete schema, column descriptions, and the
frontend integration guide for `user_google_config`.

---

## License

Internal use only.
