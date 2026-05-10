# Boomer Runbook

This document is for future-you. When something breaks at 9:15 AM with open positions,
this is where the answers are. Read it end-to-end before going live for the first time.

---

## First-time deployment (fresh VM)

### 1. VM setup (Ubuntu 22.04)
```bash
sudo adduser boomer --system --group --home /opt/boomer --shell /usr/sbin/nologin
sudo mkdir -p /var/lib/boomer/{archive,backups} /var/log/boomer /etc/boomer
sudo chown -R boomer:boomer /var/lib/boomer /var/log/boomer
sudo chmod 750 /var/lib/boomer /etc/boomer
```

### 2. Code deployment
```bash
sudo -u boomer git clone <repo-url> /opt/boomer
cd /opt/boomer
sudo -u boomer python3 -m venv .venv
sudo -u boomer .venv/bin/pip install -e ".[prod]"
```

### 3. Secrets file
Create `/etc/boomer/secrets.env` with mode 600, owned by boomer:
```
BOOMER_DB_PATH=/var/lib/boomer/boomer.db
BOOMER_ARCHIVE_DIR=/var/lib/boomer/archive
BOOMER_BACKUP_DIR=/var/lib/boomer/backups
KITE_API_KEY=...
KITE_API_SECRET=...
KITE_ACCESS_TOKEN=...
FYERS_APP_ID=...
FYERS_SECRET=...
FYERS_ACCESS_TOKEN=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
ALERT_SMTP_HOST=smtp.gmail.com
ALERT_SMTP_PORT=587
ALERT_SMTP_USER=...
ALERT_SMTP_PASSWORD=...
ALERT_EMAIL_FROM=...
ALERT_EMAIL_TO=...
BASIC_AUTH_USER=boomer
BASIC_AUTH_PASSWORD=<strong-password>
```

### 4. Run migrations
```bash
sudo -u boomer .venv/bin/python -c "from src.db.migrations import run_migrations; run_migrations('/var/lib/boomer/boomer.db')"
```

### 5. Install systemd services
```bash
sudo cp /opt/boomer/ops/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now boomer-orchestrator boomer-dashboard boomer-executor
```

### 6. Install 3 AM restart cron
```bash
sudo crontab -e
# Add: 0 3 * * * /opt/boomer/ops/restart_guard.sh >> /var/log/boomer/restart_guard.log 2>&1
```

### 7. Install Caddy reverse proxy
```
# /etc/caddy/Caddyfile
dashboard.yourdomain.com {
    reverse_proxy 127.0.0.1:8080
    basicauth / {
        boomer <bcrypt-hash>
    }
}
```

### 8. Verify everything is healthy
- Check `systemctl status boomer-orchestrator boomer-dashboard boomer-executor`
- Open dashboard in browser — should show AUTO mode, no alerts
- Check `/var/log/boomer/orchestrator.log` for startup message

---

## Daily operations

**What's normal:**
- 02:00 UTC: nightly_eod_collector starts (~20 min run)
- 06:30-07:15 UTC: morning batch chain runs
- 09:00-14:30 UTC: intraday cycles every 30 min
- 17:30 UTC: nightly backup
- 18:30 IST: daily INFO summary on Telegram

**What to check every day:**
1. Glance at Telegram daily summary — confirm all tasks ran
2. If any circuit breaker tripped, it will be in the summary (bold/red)
3. If in `PAUSED` mode, resume before 9 AM IST

---

## Incident response

### "Broker API is down"
1. Set bot_mode to `paused` via dashboard
2. Confirm all open positions have broker-side GTT stops (check Kite/Fyers app directly)
3. Wait for broker to restore. Check status pages.
4. Once restored, refresh tokens if expired, resume `auto`
5. If broker is down at open and you have intraday positions: manually close via broker app

### "Reconciliation failed"
1. Open System Health view — check reconciliation_alerts table (error details in task_runs)
2. Common causes:
   - Kite/Fyers position format change → check executor logs for parsing errors
   - Partial fill left order in unexpected state → manually resolve via broker app, then update orders table
   - Network timeout during reconciliation → usually self-heals on next cycle
3. Bot will not start tomorrow until EOD reconciliation succeeds (by design)
4. If confirmed false alarm (data quirk, not real mismatch): `UPDATE reconciliation_alerts SET resolved=1 WHERE id=?`

### "Database corruption"
```bash
# Check for corruption
sqlite3 /var/lib/boomer/boomer.db "PRAGMA integrity_check;"
# If corrupted, restore from backup (see rollback procedure below)
```

### "Bot is recommending trades I think are wrong"
1. Don't panic — all LT recommendations require manual approval anyway
2. Swing/intraday: set bot to `paused` to stop new entries
3. Check signal scores in approvals view — look at EV, RR, confidence
4. If systematic issue: set `emergency_stop`, investigate signals code, fix and redeploy

### "Bot in emergency_stop, want to resume"
Checklist before resuming:
- [ ] Understand why emergency_stop was triggered
- [ ] Verify all open positions are visible in broker app with active stops
- [ ] Run EOD reconciliation manually: `python -m src.executor.reconciliation --eod`
- [ ] Verify no reconciliation alerts are unresolved
- [ ] Check that the cause of emergency has been resolved (bug fixed, investigation complete)
- [ ] Set bot_mode to `paused` first, monitor for one hour, then `auto`

### "Emergency: can't access dashboard"
```bash
# Direct database write to set emergency_stop
sqlite3 /var/lib/boomer/boomer.db \
  "UPDATE bot_mode SET mode='emergency_stop', changed_at=datetime('now'), changed_by='manual_db'"
```

---

## Rollback procedure (schema migration included)

When a bad deployment includes a schema migration:

1. Set bot_mode to `emergency_stop`
2. Verify all intraday positions are closed (during market hours: check broker app)
3. Preserve current state:
   ```bash
   cp /var/lib/boomer/boomer.db /var/lib/boomer/rollback-attempt-$(date +%Y%m%d).db
   ```
4. Restore pre-deployment backup:
   ```bash
   cp /var/lib/boomer/backups/YYYY-MM-DD.db /var/lib/boomer/boomer.db
   ```
5. Revert code:
   ```bash
   cd /opt/boomer && git checkout <previous-commit>
   ```
6. Restart services:
   ```bash
   systemctl restart boomer-orchestrator boomer-dashboard boomer-executor
   ```
7. Verify schema version:
   ```bash
   sqlite3 /var/lib/boomer/boomer.db "SELECT * FROM schema_version ORDER BY version DESC LIMIT 5;"
   ```
8. Run reconciliation manually and verify positions match broker
9. Resume operations: paused → auto

---

## Maintenance

### Code deployment procedure (off-hours only, 18:00-22:00 IST)
1. Verify no open intraday positions
2. Set bot_mode to `paused`
3. Take DB backup: `cp boomer.db backups/pre-deploy-$(date +%Y%m%d).db`
4. `git pull && pip install -e ".[prod]"`
5. `systemctl restart boomer-orchestrator boomer-dashboard boomer-executor`
6. Verify services are healthy, no migration errors in logs
7. Resume to `auto`

### Database backup verification (quarterly)
Rebuild bot from scratch on a different VM using only the backup:
1. Spin up test VM
2. Copy backup DB
3. Run migrations (should show "all up to date")
4. Start orchestrator in `paused` mode
5. Verify dashboard loads and shows correct historical data
6. Record outcome in a note in this runbook

### Credential rotation
- Kite/Fyers tokens: expire daily (handled by pre_market_executor_setup task)
- Kite API key: rotate if compromised. Update secrets.env, restart executor
- Telegram bot token: rotate via BotFather if compromised
- SMTP credentials: rotate if compromised. Update secrets.env, restart services

### Log rotation
```bash
# /etc/logrotate.d/boomer
/var/log/boomer/*.log {
    daily
    rotate 30
    compress
    missingok
    notifempty
    postrotate
        systemctl reload boomer-orchestrator boomer-dashboard boomer-executor 2>/dev/null || true
    endscript
}
```

---

## Broker contacts

**Zerodha (Kite):** support.zerodha.com | +91-80-40402020
**Fyers:** support.fyers.in

Keep your account number and registered mobile number handy.
If you need to manually close all positions and can't access the dashboard:
1. Log into Kite / Fyers web app directly
2. Portfolio → Positions → Exit all

---

*Last updated: 2026-05-10*
