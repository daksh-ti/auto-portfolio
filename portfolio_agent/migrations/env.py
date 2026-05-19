import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# Load .env from the project root so PA_* vars are available to Alembic.
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file, override=False)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Pull DB URL from env if not set in alembic.ini.
# psycopg v3 supports synchronous mode with postgresql+psycopg:// directly.
db_url = os.environ.get("PA_OPERATIONAL_DB_URL", "")
if db_url:
    # ConfigParser uses % for interpolation; escape literal % so it isn't misread.
    config.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))

target_metadata = None


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url")
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
