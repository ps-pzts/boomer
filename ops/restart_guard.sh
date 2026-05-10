#!/bin/bash
# 3 AM orchestrator restart guard.
# Called by cron: 0 3 * * * /opt/boomer/ops/restart_guard.sh >> /var/log/boomer/restart_guard.log 2>&1
#
# Checks for running tasks before killing the orchestrator.
# If any task is RUNNING, waits 20 minutes before restarting.
# This prevents killing nightly_eod_collector mid-run (see Phase 5 Loophole 6).

set -euo pipefail

DB="/var/lib/boomer/boomer.db"
LOG_TAG="restart_guard"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [$LOG_TAG] $*"; }

RUNNING=$(sqlite3 "$DB" "SELECT COUNT(*) FROM task_runs WHERE status='RUNNING'" 2>/dev/null || echo "0")

if [ "$RUNNING" -gt "0" ]; then
    log "Tasks running (count=$RUNNING) — delaying restart by 20 minutes"
    sleep 1200
    RUNNING_AFTER=$(sqlite3 "$DB" "SELECT COUNT(*) FROM task_runs WHERE status='RUNNING'" 2>/dev/null || echo "0")
    if [ "$RUNNING_AFTER" -gt "0" ]; then
        log "Tasks still running after 20 min (count=$RUNNING_AFTER) — restarting anyway; tasks will be marked INTERRUPTED"
    else
        log "Tasks completed within delay window — proceeding with restart"
    fi
else
    log "No running tasks — proceeding with restart"
fi

systemctl restart boomer-orchestrator.service
log "boomer-orchestrator.service restarted"
