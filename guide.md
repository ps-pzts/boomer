# Boomer — Local Development Guide

This file is **not tracked by git** (listed in `.gitignore`).
It is a personal reference for running the full stack locally.

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | 3.11+ | `brew install python@3.11` |
| SQLite | 3.35+ | Ships with macOS |
| Git | any | Ships with macOS |

No external database, message queue, or server required.
SQLite is the only datastore. All data lives in a single `.db` file.

---

## 1. First-time setup

```bash
# Clone and enter the repo
cd ~/code_pr/boomer

# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install all dependencies (including dev tools)
pip install -e ".[dev]"
```

---

## 2. Environment variables

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

Open `.env` and set at minimum:

```env
BOOMER_DB_PATH=data/boomer.db
BOOMER_ARCHIVE_DIR=data/archive
BOOMER_BACKUP_DIR=data/backups

# Dashboard auth (any username/password you like locally)
BASIC_AUTH_USER=boomer
BASIC_AUTH_PASSWORD=changeme

# Leave broker tokens blank for now if you just want the dashboard
KITE_API_KEY=
KITE_ACCESS_TOKEN=
FYERS_CLIENT_ID=
FYERS_ACCESS_TOKEN=

# Leave alerts blank locally — the bot will log instead of sending
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ALERT_EMAIL_TO=
```

---

## 3. Initialize the database

Run all migrations in order to create all tables:

```bash
mkdir -p data/archive data/backups

PYTHONPATH=src python - <<'EOF'
from src.db.migrations import run_migrations
run_migrations("data/boomer.db")
print("All migrations applied.")
EOF
```

You should see output like:
```
Applied migration 0001_initial_schema.sql
Applied migration 0002_collector_schema.sql
Applied migration 0003_brain_schema.sql
Applied migration 0004_executor_schema.sql
Applied migration 0005_orchestrator_schema.sql
All migrations applied.
```

Running it a second time is safe — it skips already-applied migrations.

---

## 4. Seed test data (optional but recommended for the dashboard)

The `simulate.py` script in the project root seeds one day of realistic data:

```bash
PYTHONPATH=src python simulate.py
```

This inserts:
- A capital ledger entry
- A few open positions
- Sample pending recommendations
- A risk config row
- Sample task run records

After seeding, the dashboard will show non-empty views.

---

## 5. Run the dashboard

```bash
BOOMER_DB_PATH=data/boomer.db \
BASIC_AUTH_USER=boomer \
BASIC_AUTH_PASSWORD=changeme \
PYTHONPATH=src \
.venv/bin/uvicorn src.dashboard.app:app --port 8000 --reload
```

Open http://localhost:8000 and log in with `boomer` / `changeme`.

Five views are available:
- `/` — Today's P&L snapshot
- `/approvals` — Pending trade recommendations
- `/positions` — Open positions
- `/capital` — Capital & risk breakdown
- `/system` — Task run history and error log

The `--reload` flag auto-reloads on code changes — useful during development.

---

## 6. Run the orchestrator (locally, no market data)

The orchestrator is normally run as a systemd service in production.
Locally you can start it for testing:

```bash
BOOMER_DB_PATH=data/boomer.db \
BOOMER_ARCHIVE_DIR=data/archive \
BOOMER_BACKUP_DIR=data/backups \
PYTHONPATH=src \
python -c "
from src.orchestrator.orchestrator import Orchestrator
import asyncio
o = Orchestrator('data/boomer.db', 'data/archive', 'data/backups')
asyncio.run(o.run())
"
```

The orchestrator runs scheduled tasks based on IST cron expressions.
Outside market hours most tasks will idle. Use Ctrl+C to stop.

---

## 7. Broker token refresh (daily, before 9 AM IST)

Both Zerodha (Kite) and Fyers tokens expire every day.
You must run the login scripts before the pre_market_executor_setup task fires at 9:00 UTC.

### Kite (Zerodha)

```bash
PYTHONPATH=src python scripts/kite_login.py
```

This opens your browser to the Kite login page.
After logging in, paste the redirect URL into the terminal.
The script prints your new `KITE_ACCESS_TOKEN`.
Copy it into `.env` (or `/etc/boomer/secrets.env` in production).

### Fyers

```bash
PYTHONPATH=src python scripts/fyers_login.py
```

Same flow — opens browser, you paste back the auth code, script prints the token.
Copy the new `FYERS_ACCESS_TOKEN` into `.env`.

After updating `.env`, restart the orchestrator to pick up the new tokens.

---

## 8. Lint and tests

Run these before every commit (the CI pipeline runs the same checks):

```bash
# Lint
.venv/bin/ruff check .

# Tests
.venv/bin/pytest tests/ -x --tb=short

# Syntax check on changed files
python -m py_compile $(git diff --name-only HEAD | grep '\.py$')
```

All three must pass before pushing.

---

## 9. Docker (optional — mirrors production)

If you want to run the full stack in containers locally:

```bash
# Build the image
docker build -t boomer:local .

# Run just the dashboard against your local DB
docker run --rm \
  -v $(pwd)/data:/var/lib/boomer \
  -e BOOMER_DB_PATH=/var/lib/boomer/boomer.db \
  -e BASIC_AUTH_USER=boomer \
  -e BASIC_AUTH_PASSWORD=changeme \
  -p 8000:8000 \
  boomer:local \
  uvicorn src.dashboard.app:app --host 0.0.0.0 --port 8000

# Or use docker-compose to run everything
docker-compose up
```

The `docker-compose.yml` file runs both the orchestrator and dashboard as separate services,
sharing a named volume for the SQLite database.

---

## 10. Project structure quick reference

```
src/
  capital/       — Capital state, circuit breakers, harvest logic
  collector/     — NSE/BSE data fetchers, health checks
  brain/         — Feature computation, signals, recommendations
  executor/      — Order management, Kite/Fyers broker abstractions
  orchestrator/  — Cron scheduler, task runner, task definitions
  dashboard/     — FastAPI app, Jinja2 templates, WebSocket
  alerts/        — Telegram + email notifications
  db/            — Migration runner
migrations/      — Forward-only SQL migration files
ops/             — Systemd services, restart guard, runbook
scripts/         — Token refresh scripts for Kite and Fyers
tests/           — Mirrors src/ structure
designs/         — Phase design documents and open questions
```

---

## 11. Common problems

### "no such table" on startup
Run migrations: `PYTHONPATH=src python -c "from src.db.migrations import run_migrations; run_migrations('data/boomer.db')"`

### Dashboard shows empty views after seeding
Check that `BOOMER_DB_PATH` points to the same file the seed script used.

### "401 Unauthorized" on the dashboard
Check that `BASIC_AUTH_USER` and `BASIC_AUTH_PASSWORD` env vars are set correctly.

### Broker API returning 401
Token has expired. Run the relevant login script (Step 7) and update the env var.

### Orchestrator crashes on missing instrument symbols
The `instruments` table is populated by the nightly EOD collector.
Locally you can seed a few rows manually:
```bash
sqlite3 data/boomer.db "INSERT OR IGNORE INTO instruments
  (nse_symbol, bse_code, company_name, series, sector)
  VALUES ('RELIANCE', '500325', 'Reliance Industries', 'EQ', 'Energy');"
```
