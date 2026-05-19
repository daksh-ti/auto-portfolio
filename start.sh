#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# start.sh — run the portfolio agent directly on the host (no Docker needed).
#
# Usage:
#   ./start.sh          first run: installs everything, runs migrations, starts server
#   ./start.sh --skip-install   skip pip install (faster restarts after first run)
#
# Requirements:
#   - Python 3.11+ (or 3.10)
#   - portfolio_agent/.env file with all PA_* vars filled in
# ---------------------------------------------------------------------------
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
AGENT_DIR="$REPO_ROOT/portfolio_agent"
VENV_DIR="$REPO_ROOT/.venv"

SKIP_INSTALL=false
for arg in "$@"; do
  [[ "$arg" == "--skip-install" ]] && SKIP_INSTALL=true
done

# ---------------------------------------------------------------------------
# 1. System dependencies (Debian/Ubuntu)
# ---------------------------------------------------------------------------
if [[ "$SKIP_INSTALL" == false ]]; then
  echo "[start] Installing system dependencies..."
  if command -v apt-get &>/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv \
      libpq-dev gcc 2>/dev/null || true
  fi
fi

# ---------------------------------------------------------------------------
# 2. Python virtual environment
# ---------------------------------------------------------------------------
if [[ ! -d "$VENV_DIR" ]]; then
  echo "[start] Creating virtual environment at $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# ---------------------------------------------------------------------------
# 3. Install Python dependencies
# ---------------------------------------------------------------------------
if [[ "$SKIP_INSTALL" == false ]]; then
  echo "[start] Installing Python dependencies..."
  pip install --upgrade pip -q
  pip install -e "$AGENT_DIR" -q
  echo "[start] Dependencies installed."
fi

# ---------------------------------------------------------------------------
# 4. Verify .env exists
# ---------------------------------------------------------------------------
ENV_FILE="$AGENT_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo ""
  echo "ERROR: $ENV_FILE not found."
  echo "  Copy the example and fill in your values:"
  echo "    cp $AGENT_DIR/.env.example $ENV_FILE"
  echo "    nano $ENV_FILE"
  exit 1
fi

# ---------------------------------------------------------------------------
# 5. Run Alembic migrations (idempotent — safe to run every time)
# ---------------------------------------------------------------------------
echo "[start] Running database migrations..."
cd "$AGENT_DIR"
alembic upgrade head
echo "[start] Migrations complete."

# ---------------------------------------------------------------------------
# 6. Start the scheduler + trigger API server
# ---------------------------------------------------------------------------
echo "[start] Starting portfolio agent (scheduler + API on port 8000)..."
exec portfolio-agent serve
