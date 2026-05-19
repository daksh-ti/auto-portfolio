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
    # Each user's token + folder_id stored in portfolio_agent.user_google_config.
    google_client_secrets_path: Path     # OAuth app registration JSON (shared, server-side)

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

    # ---- Email notifications ----
    notifications_enabled: bool = False
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    email_from: str | None = None   # e.g. "Portfolio Agent <you@gmail.com>"

    # ---- Trigger API ----
    api_port: int = 8000
    # Static API key checked via X-API-Key header.
    # Leave empty to disable auth (dev only — always set in production).
    api_key: str = ""

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
