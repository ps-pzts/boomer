#!/usr/bin/env bash
# dev.sh — start orchestrator + dashboard locally as independent background processes
# Usage: ./dev.sh
# Stop:  ./dev.sh stop   (kills both processes)
set -euo pipefail

PIDFILE_ORC=".pids/orchestrator.pid"
PIDFILE_DASH=".pids/dashboard.pid"
LOG_ORC="data/logs/orchestrator.log"
LOG_DASH="data/logs/dashboard.log"

# ── helpers ──────────────────────────────────────────────────────────────────

die() { echo "ERROR: $*" >&2; exit 1; }

stop_all() {
    local stopped=0
    for pf in "$PIDFILE_ORC" "$PIDFILE_DASH"; do
        if [[ -f "$pf" ]]; then
            local pid; pid=$(<"$pf")
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid"
                echo "Stopped pid $pid ($(basename "$pf" .pid))"
                stopped=$((stopped + 1))
            fi
            rm -f "$pf"
        fi
    done
    [[ $stopped -eq 0 ]] && echo "No running processes found."
    exit 0
}

# ── stop mode ────────────────────────────────────────────────────────────────

[[ "${1:-}" == "stop" ]] && stop_all

# ── guards ───────────────────────────────────────────────────────────────────

[[ -f .venv/bin/activate ]] || die ".venv not found — run: python3.11 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'"
if [[ ! -f .env ]]; then
    echo "No .env found — creating one with local defaults (edit to add broker tokens later)."
    cat > .env <<'ENVEOF'
BOOMER_DB_PATH=data/boomer.db
BOOMER_ARCHIVE_DIR=data/archive
BOOMER_BACKUP_DIR=data/backups

BASIC_AUTH_USER=boomer
BASIC_AUTH_PASSWORD=changeme
DASHBOARD_PORT=8000

# Broker credentials — fill these in to enable EOD capital sync
# Get KITE_API_KEY + KITE_API_SECRET from https://developers.kite.trade
# Get FYERS_CLIENT_ID + FYERS_SECRET_KEY from https://myapi.fyers.in
KITE_API_KEY=
KITE_API_SECRET=
KITE_ACCESS_TOKEN=
FYERS_CLIENT_ID=
FYERS_SECRET_KEY=
FYERS_REDIRECT_URI=https://127.0.0.1
FYERS_ACCESS_TOKEN=

# Alerts — leave blank to log instead of sending
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ALERT_EMAIL_TO=
ENVEOF
fi

# ── env ──────────────────────────────────────────────────────────────────────

# shellcheck source=/dev/null
source .venv/bin/activate
set -o allexport; source .env; set +o allexport

: "${BOOMER_DB_PATH:=data/boomer.db}"
: "${BOOMER_ARCHIVE_DIR:=data/archive}"
: "${BOOMER_BACKUP_DIR:=data/backups}"
: "${BASIC_AUTH_USER:=boomer}"
: "${BASIC_AUTH_PASSWORD:=changeme}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8000}"

# ── dirs ─────────────────────────────────────────────────────────────────────

mkdir -p .pids data/logs "$BOOMER_ARCHIVE_DIR" "$BOOMER_BACKUP_DIR" "$(dirname "$BOOMER_DB_PATH")"

# ── orchestrator (runs migrations on startup automatically) ──────────────────

BOOMER_DB_PATH="$BOOMER_DB_PATH" \
BOOMER_ARCHIVE_DIR="$BOOMER_ARCHIVE_DIR" \
BOOMER_BACKUP_DIR="$BOOMER_BACKUP_DIR" \
nohup python -m src.orchestrator.orchestrator >> "$LOG_ORC" 2>&1 &
echo $! > "$PIDFILE_ORC"
echo "Orchestrator started (pid $!, log: $LOG_ORC)"

# ── dashboard ────────────────────────────────────────────────────────────────

BOOMER_DB_PATH="$BOOMER_DB_PATH" \
BASIC_AUTH_USER="$BASIC_AUTH_USER" \
BASIC_AUTH_PASSWORD="$BASIC_AUTH_PASSWORD" \
nohup uvicorn src.dashboard.app:app --host 0.0.0.0 --port "$DASHBOARD_PORT" >> "$LOG_DASH" 2>&1 &
echo $! > "$PIDFILE_DASH"
echo "Dashboard started  (pid $!, log: $LOG_DASH)"

# ── done ─────────────────────────────────────────────────────────────────────

echo ""
echo "Dashboard → http://localhost:${DASHBOARD_PORT}"
echo ""
echo "Tail logs:  tail -f $LOG_ORC $LOG_DASH"
echo "Stop both:  ./dev.sh stop"
