#!/usr/bin/env bash
# gen-rep.sh - Records the current traffic in the log and regenerates the HTML report.
# Meant to run every hour (cron or Windows Task Scheduler).
#
# Usage: ./gen-rep.sh [file.html]     (default: report.html in the project folder)
# Output and errors are appended to data/gen-rep.log

set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$DIR/venv/bin/python"
REPORT="${1:-$DIR/report.html}"
LOG="$DIR/data/gen-rep.log"
LOCK="$DIR/data/.gen-rep.lock"

mkdir -p "$DIR/data"
cd "$DIR" || exit 1

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

if [[ ! -x "$PY" ]]; then
    log "ERROR: $PY not found"
    exit 1
fi

# Prevents two simultaneous runs if one takes longer than usual.
exec 9> "$LOCK"
if ! flock -n 9; then
    log "WARNING: a run is already in progress; skipping this one."
    exit 0
fi

log "Start"
"$PY" get-traffic.py --check-traffic --generate-graph "$REPORT" >> "$LOG" 2>&1
rc=$?
log "End (exit code $rc)"

# Publishes the report to GitHub Pages (settings in publish.conf; a no-op if disabled).
if [[ $rc -eq 0 ]]; then
    "$DIR/publish.sh" "$REPORT" >> "$LOG" 2>&1
    log "Publish (exit code $?)"
fi

# Keeps the log at a reasonable size (last 5000 lines).
if [[ $(wc -l < "$LOG") -gt 5000 ]]; then
    tail -n 5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

exit $rc
