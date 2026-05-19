# ── Stage 1: dependency builder ──────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# System deps needed by psycopg (libpq) and google-api-python-client
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# Install into an isolated prefix so we can COPY just the site-packages
RUN pip install --upgrade pip
COPY pyproject.toml .
# Dummy src so pip can resolve the package metadata
RUN mkdir -p src/portfolio_agent && touch src/portfolio_agent/__init__.py
RUN pip install --prefix=/install ".[dev]" 2>/dev/null || pip install --prefix=/install .


# ── Stage 2: runtime image ───────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Runtime system deps (libpq only — no gcc needed at runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY src/            ./src/
COPY config/         ./config/
COPY migrations/     ./migrations/
COPY alembic.ini     ./alembic.ini

# Install the package in editable-ish mode (makes `portfolio-agent` CLI available)
COPY pyproject.toml .
RUN pip install --no-deps -e .

# Copy entrypoint
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# config/rules_config.yaml is potentially written by the agent — mount as a volume.
# Declaring VOLUME here documents the intent; docker-compose overrides it.
VOLUME ["/app/config"]

# Secrets mounted at runtime (Google OAuth client secret JSON)
VOLUME ["/secrets"]

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["serve"]
