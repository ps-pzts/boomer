# Phase 5 — Orchestrator + Dashboard + Operations

This phase is about how the system you've designed actually *runs* day to day. Three components.

---

## Component 1 — Orchestrator

### Goal

The orchestrator decides what runs when, manages dependencies between tasks, handles failures gracefully, and exposes the state of the system to the dashboard.

It is the single thing that knows "the time is 7 AM, run the morning batch" and "the collector failed, don't run the analyser."

### What it isn't

- Not a workflow engine like Airflow or Prefect (overkill for 6-12 scheduled tasks)
- Not a general task queue like Celery (overkill for sequential dependencies)
- Not a microservices coordinator (single monolith)

It's **a handful of cron entries plus a small state machine.** Boring, by design.

### Task taxonomy

Every scheduled task has a defined contract:

| Field | Purpose |
|-------|---------|
| `task_id` | Unique name (e.g., `morning_batch`, `intraday_cycle`) |
| `schedule` | Cron expression or trigger event |
| `dependencies` | Other task_ids that must succeed first |
| `timeout_seconds` | Max runtime |
| `retry_policy` | Retries and backoff |
| `failure_action` | What to do if all retries fail |

### Scheduled tasks

| Task | Schedule | Depends on | Timeout |
|------|----------|------------|---------|
| `nightly_eod_collector` | 02:00 daily | none | 30 min |
| `early_morning_data_check` | 06:30 weekday | `nightly_eod_collector` | 5 min |
| `morning_batch_features` | 06:45 weekday | `early_morning_data_check` | 10 min |
| `morning_batch_signals` | 07:00 weekday | `morning_batch_features` | 15 min |
| `morning_batch_recommendations` | 07:15 weekday | `morning_batch_signals` | 5 min |
| `pre_market_executor_setup` | 09:00 weekday | `morning_batch_recommendations` | 5 min |
| `intraday_cycle` | every 30 min, 09:30-14:30 weekday | none | 3 min |
| `position_review` | every 60 min, 09:30-15:00 weekday | none | 2 min |
| `intraday_squareoff` | 15:14 weekday | none | 5 min — calls executor's `square_off_all_intraday()` method; executor does not self-trigger this |
| `eod_reconciliation` | 16:00 weekday | none | 10 min |
| `weekly_harvest_check` | 16:30 Friday | `eod_reconciliation` | 2 min |
| `nightly_backup` | 23:00 daily | none | 15 min |

12 scheduled tasks. Within reach of cron + small Python supervisor. No orchestration framework needed.

### State machine

```
PENDING → RUNNING → SUCCESS
                  → FAILED → RETRYING → SUCCESS
                                      → FAILED_FINAL
                  → TIMEOUT → FAILED_FINAL
```

`task_runs` table — every run logged with start, end, status, error. Source of truth for "is the system healthy?"

### Dependency handling

Most tasks have linear dependencies. Morning batch:

```
data_check → features → signals → recommendations → executor_setup
```

Failed `data_check` → `features` doesn't run.

**Decision: dependencies checked but not enforced as hard locks.** If `data_check` fails but operator manually fixes it and force-runs `features`, orchestrator allows with `manual_override=true` flag logged. Don't block legitimate manual recovery.

### Failure handling per task

**`nightly_eod_collector` fails** — retry 3 times with backoff (5min, 15min, 45min). All fail → alert, mark `early_morning_data_check` as blocked. Morning batch will not run until intervention. Correct behaviour: trading on stale/partial data is worse than not trading.

**`morning_batch_signals` fails** — retry 2 times with 10min gap. Still fails → alert, `not_generated_today`. Existing positions monitored (separate task), but no new entries today.

**`intraday_cycle` fails** — no retry (next cycle 30 min away). Log error, increment failure counter. **3 cycles in a row failed → intraday auto-disabled rest of day.**

**`eod_reconciliation` fails** — retry 5 times across an hour. Still fails → emergency. Bot cannot start tomorrow until reconciliation succeeds. Loud alert.

**`nightly_backup` fails** — retry once. Still fails → alert but don't block other tasks.

### Manual mode switch

One global flag: `bot_mode`. Three values:

- `auto` — orchestrator runs everything on schedule
- `paused` — scheduled tasks don't run; manual triggers still work
- `emergency_stop` — no tasks run, no orders placed, no actions taken

**Operator should be able to flip to `emergency_stop` in under 5 seconds.** Non-negotiable operational requirement.

`paused` use cases:
- Operator traveling
- Weird market conditions (election, budget, war)
- Code update deploy

`emergency_stop` use case: suspect critical bug. Open positions sit with broker stops as only protection.

### Loopholes and decisions

**Loophole 1 — Clock drift:** task scheduled for 7:00 might run at 7:00:01 or 6:59:58. What does "today" mean?

**Decision:** Every task uses single `run_id` and `run_date` parameter set at orchestrator dispatch. All "today" queries within the task use that anchor, not wall-clock.

**Loophole 2 — Stuck tasks:** task hangs (network deadlock, bad query). Timeout fires but cleanup might not.

**Decision:** Every task wraps work in context manager that always writes `task_runs` row on exit, even on hard kill. Aggressive try/finally. Orphaned database transactions rolled back automatically by connection close.

**Loophole 3 — Holiday handling:** trading holidays mean no market data, no trades.

**Decision:** Maintain `trading_calendar` table updated yearly. Every task checks `is_trading_day(today)` first. If not, log skip, exit cleanly. Some tasks (backup) run on holidays anyway.

**Loophole 4 — Daylight saving:** India doesn't have DST but international data sources might.

**Decision:** Store all timestamps as UTC. Convert to IST only at display. Cron expressions use IST explicitly.

**Loophole 5 — Orchestrator itself crashes:**

**Decision:** Orchestrator is a long-running supervisor process managed by systemd. If it dies, systemd restarts within seconds. On restart, reads `task_runs` to figure out what it was doing — either resumes or marks as `interrupted`. **Idempotent task design is critical** — re-running a "completed" task should be safe.

**Loophole 6 — 3 AM cron restart can kill nightly collector mid-run:**

`nightly_eod_collector` runs at 2:00 AM with a 30-minute timeout. The 3:00 AM orchestrator restart cron fires unconditionally. If the collector is still running at 3:00 AM (delayed by slow NSE response, retries, or large backfill), systemd kills it mid-run and marks it `interrupted`.

**Decision:** The cron restart script checks for running tasks before killing the orchestrator:

```bash
#!/bin/bash
# Check if any task is RUNNING in task_runs
RUNNING=$(sqlite3 /var/lib/boomer/boomer.db \
  "SELECT COUNT(*) FROM task_runs WHERE status='RUNNING'")
if [ "$RUNNING" -gt "0" ]; then
  echo "Tasks running, delaying restart by 20 minutes"
  sleep 1200
fi
systemctl restart boomer-orchestrator.service
```

If a task is still running at 3 AM, the restart is delayed by 20 minutes (3:20 AM), after which all normal tasks have long completed. In the unlikely event the collector is still running at 3:20 AM, it will be killed — an interrupted collector triggers the `early_morning_data_check` failure path and the operator is alerted.

---

## Component 2 — Dashboard

### Goal

The dashboard is the operator's only interface to the system in normal operation. Shows what the bot is doing, why, and what needs attention.

### Scope

**For:**
- Reviewing pending recommendations (especially long-term, where approval required)
- Monitoring open positions and their health
- Today's bot activity at a glance
- Risk state (drawdown, breakers, capital)
- Investigating issues
- Toggling bot mode (auto/paused/emergency_stop)

**Not for:**
- Configuring strategies (code commits)
- Setting risk parameters (config file change with audit trail)
- Manual trading (use Kite directly)
- Deep performance analysis (separate analytics tool, post-v1)

Scope discipline matters. **Dashboards trying to do everything become bloated.**

### The five views

#### View 1 — Today (landing page)

**Most important fact must be visible without scrolling.**

Layout top to bottom:
1. **Bot mode indicator** — single big indicator (green AUTO, yellow PAUSED, red EMERGENCY) with one-click toggle
2. **Active alerts banner** — circuit breaker tripped or critical issue. Empty if all clear.
3. **Today's snapshot** — 4 numbers: total P&L today, # signals generated, # trades placed, # positions opened
4. **Per-track quick view** — long-term, swing, intraday side by side
5. **Approvals waiting** — count of long-term recommendations needing decision

Glance-able in 5 seconds. Page tells you when attention needed; calm when fine.

#### View 2 — Approvals (workflow)

The approval queue. Each pending recommendation shows:

- **Header:** stock name, exchange, current price
- **Trade plan:** entry zone, stop, target, position size in shares and rupees
- **Signal score and confidence** with attribution chain
- **Supporting evidence:** actual data points ("Promoter X bought 0.5% on April 22 in open market")
- **Portfolio impact:** "Adding takes financials sector from 18% to 22%"
- **Three buttons:** Approve · Modify · Reject

Modify allows adjusting entry zone, stop, target, position size before approval. Original preserved alongside modifications, both logged.

**Modification re-validation:** When the operator changes any parameter, the dashboard calls the server to re-run Stage 3 gates (RR check, EV check with new parameters) and Stage 4 concentration checks (with new position size). Results appear inline — a green/red indicator per gate as the operator edits. The "Confirm modification" button remains disabled until all gates pass. This prevents the operator from approving modifications that the executor's pre-trade checks would later silently reject. The HTMX call debounces at 400ms to avoid hammering the server on every keystroke.

Reject prompts for reason: *bad timing / disagree with signal / portfolio constraint / other*. Logged as training data for future signal weighting.

**Bulk approval safeguard:** if more than 3 recommendations pending, no "approve all" button. Each requires individual action. Friction is feature, not bug.

#### View 3 — Positions

Every open position with health scores. Sorted by health score ascending (attention-needed first).

Per-row:
- Stock, track, entry date, days held
- Entry price, current price, P&L (₹ and %)
- Stop-loss, target, distance to each
- Health score (color-coded)
- Exit recommendation banner if exists

Click position → detail view with full history, original signal, every related order.

Filtering: by track, health bucket, sector. Default: all open.

#### View 4 — Capital & risk

Macro state. The risk command centre.

**Top — capital state:**
- Total, HWM, current drawdown (% from HWM with sparkline)
- Each bucket: allocated, deployed, available, week's P&L

**Middle — concentration:**
- Sector heat map: tiles sized by exposure %, colored by performance
- Correlation cluster view: stocks grouped by correlation
- Top 5 single-stock concentrations with bar showing distance to 5% cap

**Bottom — circuit breakers:**
- All 8 breakers with status (armed / tripped / cooldown)
- For tripped: what tripped, when auto-resets

For weekly review, not daily.

#### View 5 — System health

Diagnostic view. Mostly empty when things work, detailed when broken.

- **Task run timeline:** last 24h as horizontal timeline. Green/red/yellow markers. Click for details.
- **Data freshness:** per source, last successful fetch, age, status
- **Broker status:** connected, reconciliation lag, last successful order time
- **Recent errors:** last 50 errors with timestamps and stack traces
- **Reconciliation alerts:** positions/orders where bot view ≠ broker view

### Tech stack

FastAPI backend. Jinja2 templates. HTMX for interactivity. No React build pipeline. Live updates via FastAPI WebSocket. Charts via Chart.js from CDN.

**Entire dashboard: single Python file + 5 HTML templates + small CSS file.** Not 100 React components. Simplicity is intentional.

### Scope exclusions

- No charts of historical performance (Phase 6+ analytics concern)
- No backtest results browsing (CLI-driven, results in flat files)
- No "explore signals" interface (code/notebooks)
- No multi-user features (solo system)
- No mobile-optimised layout for v1 (desktop only)

### Loopholes and decisions

**Loophole 1 — WebSocket disconnects:** dashboard left open overnight, WebSocket dies, stale data.

**Decision:** WebSocket auto-reconnects with exponential backoff. Page header shows small "live" indicator (green/yellow/red).

**Loophole 2 — Approval expiry:** recommendation pending overnight, market opens, stale.

**Decision:** Every recommendation has `valid_until` shown prominently. After expiry, approve disabled with reason. Auto-clear from queue on expiry.

**Loophole 3 — Concurrent approval:** operator approves; bot already auto-rejected for risk in parallel.

**Decision:** Every action checked against current state at click moment. If recommendation is in final state, action rejected with clear message. Optimistic UI updates revert.

**Loophole 4 — Authentication for emergency stop:** must be reachable in 5 seconds, but can't be wide-open.

**Decision:** Dashboard is HTTP basic auth at reverse proxy level, plus emergency_stop and approval actions require session cookie. No SSO. For solo system on private VM, sufficient.

**Loophole 5 — Performance with many positions:** 50 positions makes page slow.

**Decision:** Pagination at 25 per view, sorted by health score. Most-needing-attention always on top. Unlikely to matter for v1 (positions capped at 15 by design).

**Loophole 6 — Dashboard process dies:** doesn't matter for trading.

**Decision:** Dashboard is separate systemd service. If it crashes, restarts. Bot's trading operations completely independent. **Never let dashboard bugs affect trading.**

---

## Component 3 — Operations

### Deployment shape

```
Single Linux VM (Ubuntu 22.04 LTS)
├── /opt/boomer/              — code (git repo)
│   └── migrations/           — forward-only SQL migration files
├── /var/lib/boomer/          — data
│   ├── boomer.db             — SQLite primary database
│   ├── archive/              — raw scraped data, gzipped
│   └── backups/              — daily backups
├── /var/log/boomer/          — logs
└── /etc/boomer/
    ├── config.yaml           — non-secret config
    └── secrets.env           — encrypted secrets file

systemd services:
├── boomer-orchestrator.service  — supervisor
├── boomer-dashboard.service     — FastAPI app
└── boomer-websocket.service     — broker live data feed

reverse proxy:
└── Caddy or nginx → HTTPS to dashboard, IP allowlist

cron:
└── Only one external cron: orchestrator restart at 3 AM (with active-task guard — see Loophole 6 fix)
   (orchestrator manages all task schedules internally)
```

Total processes: 3-4. Disk under 50 GB for a year. RAM 1-2 GB. Comfortably fits Oracle Cloud free tier or ₹500/month VPS.

### Database schema migrations — forward-only pattern

Schema changes are managed via a **forward-only migration script pattern**. Never use `ALTER TABLE` manually on the live database. Never modify an existing migration file after it has been applied.

**Directory structure:**

```
/opt/boomer/migrations/
├── 0001_initial_schema.sql
├── 0002_add_fo_oi_data.sql
├── 0003_add_shares_outstanding.sql
├── 0004_add_quarterly_financials.sql
├── 0005_add_instruments_table.sql
├── 0006_add_gtt_orders.sql
...
```

**`schema_version` table (created by migration 0001):**

```sql
CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

**Migration runner** (called at application startup, before any other database access):

```python
def run_migrations(db_path):
    conn = sqlite3.connect(db_path)
    # Create schema_version if it doesn't exist yet
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version ...")
    current = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
    for migration_file in sorted(glob("migrations/*.sql")):
        version = int(migration_file.split("_")[0])
        if version > current:
            conn.executescript(open(migration_file).read())
            conn.execute("INSERT INTO schema_version VALUES (?, ?, ?)", ...)
            conn.commit()
            log(f"Applied migration {version}")
```

**Rules:**
1. Each migration file is immutable once applied — never edit it.
2. Migrations are additive only: `CREATE TABLE`, `CREATE INDEX`, `ALTER TABLE ... ADD COLUMN`. No `DROP TABLE`, no `ALTER COLUMN`, no `DELETE`.
3. If a migration has a mistake, write a new migration (e.g., `0007_fix_instruments_index.sql`) to correct it.
4. The runner runs at startup — if a migration fails, the application does not start. This surfaces schema problems immediately.
5. Before any deployment, back up `boomer.db`. The backup is the rollback — if a migration goes wrong, restore the backup and revert the code commit.

**Forward-only rationale:** SQLite's `ALTER TABLE` support is limited (cannot modify column types or drop columns). Trying to manage rollbacks across SQLite schema versions produces more complexity than the risk justifies. The discipline of "backup before deploy, restore to rollback" is simpler and safer at this scale.

### Monitoring philosophy

For a solo system, you don't need Prometheus + Grafana + alerting platforms. You need:

1. Logs that are searchable when something breaks (structured JSON to files, daily rotation)
2. A dashboard that tells you what's wrong (designed above)
3. Alerts that reach you when not at the dashboard (Telegram or email)

That's it. No metric pipelines, no dashboards-on-dashboards, no SRE patterns.

### Alert layer

**Three severity levels:**

**INFO** — daily summary, weekly performance recap. Sent regardless. Telegram channel. *"Bot ran today. 3 trades placed, 2 exits. Daily P&L: +₹450. No alerts."*

**WARN** — needs attention but not urgent. *"Data freshness: NSE filings 4h stale. Consider checking."* Sent immediately, doesn't wake you. Telegram only.

**CRITICAL** — needs immediate attention. *"Reconciliation failure: bot shows 3 positions, broker shows 2."* **Sent on two independent channels simultaneously** — Telegram (primary) and email (mandatory fallback). Relying on a single channel for critical alerts is a reliability risk: Telegram has periodic outages and can be blocked on some networks. When a position is unprotected or a circuit breaker has tripped, the operator must be notified regardless of Telegram availability.

**Tooling decision:**
- **Telegram** — primary channel for all severities. Create bot, add to channel, send via API. Free, fast, works on phone/laptop/tablet.
- **Email** — required secondary channel, CRITICAL-only. Use a transactional email provider with a free tier (SendGrid free, or Gmail SMTP with app password). The email is a fallback, not a monitoring destination — the operator shouldn't need to check it in normal operation.

If both channels fail for a CRITICAL event, the condition is logged to `critical_notification_failures` table. On next dashboard load, a prominent banner shows any missed critical alerts.

### Backup strategy

Database is the only thing that matters (code in git, raw archive can be regenerated though painfully).

**Daily backup:** SQLite copy with WAL mode is safe while system runs. 11 PM backup copies DB to `/var/lib/boomer/backups/YYYY-MM-DD.db`.

**Off-machine backup:** weekly upload to a separate location.
- Backblaze B2 (cheap, good for backups)
- Cloudflare R2 (free tier, 10 GB)
- External git repo (only if DB stays small)
- Home machine via rsync over SSH

Off-machine is critical. Cloud VMs can die. Oracle's free tier specifically has terminated instances. **History must survive cloud catastrophes.**

**Retention:** 30 daily, 12 weekly, 12 monthly. ~3 GB total.

### Security baseline

For solo system on private VM:

- **SSH:** key-only, no passwords, root login disabled, port 22 firewalled to home IP if static, otherwise `fail2ban`
- **Dashboard:** HTTPS via Caddy auto-cert, basic auth with strong password, IP allowlist if possible
- **Broker credentials:** encrypted file, decrypted at startup with passphrase from environment, never in git
- **Database:** chmod 600, owned by service user
- **No public-facing services other than dashboard** (firewall everything else)
- **System updates:** weekly `apt upgrade` Sunday, monitored

Good enough for solo system protecting your own money. Not enterprise-grade. Enterprise security adds operational pain not justified at this scale.

### Runbook (the document for future-you)

Lives next to code as markdown. Sections:

- **First-time deployment:** step-by-step bringing up fresh VM
- **Daily operations:** what to expect, what's normal
- **Incident response:**
  - "Broker API is down" — verification, what to disable
  - "Reconciliation failed" — diagnostic steps, common causes
  - "Database corruption" — restore procedure, recovery time
  - "Bot is recommending trades I think are wrong" — investigation, when to override
  - "Bot in emergency_stop, want to resume" — checklist
- **Maintenance:**
  - Code deployment procedure (off-hours only)
  - Database backup verification
  - Log rotation and cleanup
  - Token/credential rotation

**The runbook is part of the system.** Not optional. It's what stops you from forgetting how to recover.

### Loopholes and decisions

**Loophole 1 — VM dies during market hours, open positions exist.**

**Decision:** ALL positions have broker-side stop-loss orders. If bot dies, broker still enforces stops. Bot's monitoring is *second* layer; broker is *first* layer of protection. **Non-negotiable design.** Verify weekly that all open positions have active stop orders at broker.

**Loophole 2 — Operator loses access to laptop/phone/Telegram.**

**Decision:** Maintain printed/USB emergency procedure with: (a) how to log into VM from any device, (b) how to flip emergency_stop via direct database write if dashboard unreachable, (c) Zerodha customer service number to manually close positions if bot non-responsive.

**Loophole 3 — Code deployment with open positions:** push at 11 AM, dashboard restarts.

**Decision:** Code deployments only during 18:00-22:00 IST window (after market close, before nightly tasks). Outside emergencies. Orchestrator has "deployment in progress" mode that pauses task scheduling but keeps reconciliation running.

**Loophole 4 — Monitoring blindness:** alerts noisy, channel muted, real critical missed.

**Decision:** Discipline alert volume. INFO daily summary at 18:30 only — no per-event INFO alerts. WARN batched (one summary every 6 hours). CRITICAL always immediate. If WARN firing constantly, it's a code bug; fix the code, don't mute the channel.

**Loophole 5 — "Trust spiral":** bot profitable for a month, operator stops checking, breaker trips, missed for a week.

**Decision:** Daily INFO summary always includes circuit breaker states. If anything tripped, summary is bold/red. Glance at summary every day even when profitable. **Discipline rule, not just system rule.**

**Loophole 6 — Free tier suspension:** Oracle has suspended free accounts for "high resource usage."

**Decision:** Weekly off-machine backup non-negotiable. Test restore procedure once a quarter — actually rebuild bot from scratch on different VM using only backups. **If you can't restore in <4 hours, your backup strategy is broken.**

**Loophole 7 — SQLite write contention during peak market hours.**

During market hours, multiple components write to the same SQLite database concurrently: the executor reconciliation loop (every 60s), the intraday continuous pipeline (every 30 min), the position review task (every 60 min). SQLite WAL mode allows one writer at a time. A slow reconciliation query during a volatile session (many positions × many orders) could hold the write lock and delay other writers, causing intraday cycles to queue.

**Decision:**
1. Enable WAL mode and set `busy_timeout = 5000ms` on all database connections. Writers wait up to 5 seconds for the lock before raising an error — long enough to survive brief contention without silently blocking.
2. Every database query in the reconciliation loop must complete within 3 seconds. Enforce with SQLite `pragma busy_timeout` and a Python-level query timeout wrapper. If a reconciliation query exceeds 3s, it is cancelled and logged — never allowed to hold the lock indefinitely.
3. Reconciliation loop queries use targeted lookups (indexed on `position_id`, `order_id`) rather than full-table scans. No unindexed joins in the hot path.
4. The dashboard (read-only) uses a separate read connection in WAL mode — it never contends with writers.

At v1 scale (15 max positions, 30-50 orders/day), this is unlikely to be a problem in practice. The constraint is architectural insurance against gradual data growth degrading query latency over time.

---

## Stop conditions for Phase 5 (all met)

- Orchestrator: 12 scheduled tasks defined
- Orchestrator: intraday square-off triggered by orchestrator only (executor provides method, doesn't self-trigger)
- Orchestrator: state machine with retry policies
- Orchestrator: bot_mode flag (auto/paused/emergency_stop)
- Dashboard: 5 views with explicit scope
- Dashboard: modification re-validation (Stage 3 + Stage 4 checks run inline before confirm)
- Dashboard: tech stack confirmed (FastAPI + Jinja2 + HTMX)
- Operations: alert layer (Telegram primary + email mandatory secondary for CRITICAL)
- Operations: SQLite WAL mode, busy_timeout, query time-bound constraints documented
- Operations: deployment shape (1 VM, 3 services, SQLite)
- Operations: forward-only migration pattern (migrations/ dir, schema_version table, runner at startup)
- Operations: 3 AM restart guard checks for running tasks before kill
- Operations: backup strategy (daily local, weekly off-machine)
- Operations: security baseline
- Operations: runbook outline
- 20 loopholes identified across three components

## What this design buys

1. **Operationally simple.** One VM, a few services, cron-style scheduling. Anyone who's run a Linux server can manage this.
2. **Failure-isolated.** Dashboard crash doesn't affect trading. Bot crash doesn't lose data. VM death doesn't lose stops (broker holds them).
3. **Recoverable.** Backups exist, restore procedure exists, runbook exists. Can rebuild from scratch.
4. **Observable.** Glance at dashboard tells you health. Telegram tells you when away.
5. **Boring.** No fancy infrastructure. No exotic monitoring stack. Comprehensible in 6 months when revisiting.
