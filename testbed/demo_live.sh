#!/usr/bin/env bash
# testbed/demo_live.sh — end-to-end live proof: monitor → inject cpu fault → alert.
#
# Usage (from the project root, requires root):
#   sudo bash testbed/demo_live.sh [--interval SECONDS] [--timeout SECONDS]
#
# What it does:
#   1. Loads the persisted model (data/model/ocsvm.joblib or daemon model)
#   2. Starts rcaDaemon.py --monitor --max-cycles N in the background
#   3. Waits for the daemon to start, then injects a safe cpuStressor1 fault
#   4. Waits for an alert to appear in data/alerts.jsonl
#   5. Prints the alert and the latest RCA report excerpt
#   6. Cleans up the daemon
#
# The alert must appear within --timeout seconds (default: 120) after the
# fault starts (Plan 4.6).
#
# This is the safe fault: stress-ng --cpu 2 --timeout 45s (no disk or memory
# pressure; safe to run on any workstation).

set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# ---- defaults ----
INTERVAL=20          # daemon cycle interval (s)
TIMEOUT=120          # max seconds to wait for alert
MAX_CYCLES=8         # daemon stops after this many cycles
FAULT_DURATION=45    # cpuStressor1 duration

while [[ $# -gt 0 ]]; do
    case "$1" in
        --interval) INTERVAL="$2"; shift 2 ;;
        --timeout)  TIMEOUT="$2";  shift 2 ;;
        *)          echo "unknown arg: $1"; exit 2 ;;
    esac
done

MAX_CYCLES=$(( (TIMEOUT / INTERVAL) + 3 ))

PYTHON="${PYTHON:-.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="python3"

ok()   { printf '\033[32m  ok    %s\033[0m\n' "$*"; }
fail() { printf '\033[31m  FAIL  %s\033[0m\n' "$*"; }
info() { printf '\033[34m  ..    %s\033[0m\n' "$*"; }
header() { echo; printf '\033[1m== %s ==\033[0m\n' "$*"; }

header "RCA Live Demo"
echo "  interval=${INTERVAL}s  fault-duration=${FAULT_DURATION}s  timeout=${TIMEOUT}s"

# ---- guard: root ----
if [ "$(id -u)" -ne 0 ]; then
    fail "must run as root: sudo bash testbed/demo_live.sh"
    exit 1
fi
ok "running as root"

# ---- guard: model ----
MODEL=""
if [ -f data/model/ocsvm_daemon.joblib ]; then
    MODEL="data/model/ocsvm_daemon.joblib"
elif [ -f data/model/ocsvm.joblib ]; then
    MODEL="data/model/ocsvm.joblib"
else
    fail "no model found — run: python3 anomalyDetection.py --train"
    exit 1
fi
ok "model: $MODEL"

# ---- guard: disk ----
free_gib=$(df -BG --output=avail "$PROJECT_DIR" | tail -1 | tr -dc '0-9')
free_gib=${free_gib:-0}
if [ "$free_gib" -lt 2 ]; then
    fail "disk free ${free_gib} GiB < 2 GiB — clean up data/ first"
    exit 1
fi
ok "disk free: ${free_gib} GiB"

# ---- guard: tools ----
for t in bpftrace stress-ng stdbuf; do
    command -v "$t" > /dev/null 2>&1 || { fail "missing: $t"; exit 1; }
done
ok "tools present"

# ---- clean stale alerts to avoid leftover alerts counting ----
DEMO_ALERTS_LINES=0
if [ -f data/alerts.jsonl ]; then
    DEMO_ALERTS_LINES=$(wc -l < data/alerts.jsonl)
fi
info "existing alerts.jsonl lines: $DEMO_ALERTS_LINES (new alerts will appear after this)"

# ---- start the daemon ----
header "Starting daemon (monitor mode)"
DAEMON_LOG="data/demo_daemon.log"
"$PYTHON" rcaDaemon.py --monitor \
    --model "$MODEL" \
    --interval "$INTERVAL" \
    --max-cycles "$MAX_CYCLES" \
    --flag-fraction 0.5 \
    > "$DAEMON_LOG" 2>&1 &
DAEMON_PID=$!
info "daemon pid=$DAEMON_PID  log=$DAEMON_LOG"

# wait for collectors to start (probe/procfs processes visible)
deadline=$(( $(date +%s) + 30 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    if pgrep -f 'probeExtractor' > /dev/null 2>&1; then
        ok "collectors started"
        break
    fi
    sleep 1
done
if ! pgrep -f 'probeExtractor' > /dev/null 2>&1; then
    fail "collectors did not start in 30s — check $DAEMON_LOG"
    kill "$DAEMON_PID" 2>/dev/null || true
    exit 1
fi

sleep 3   # let one collection cycle warm up

# ---- inject fault ----
header "Injecting fault: cpuStressor1 (stress-ng --cpu 2 --timeout ${FAULT_DURATION}s)"
FAULT_START=$(date +%s)
stress-ng --cpu 2 --timeout "${FAULT_DURATION}s" > /dev/null 2>&1 &
FAULT_PID=$!
info "fault pid=$FAULT_PID  start=$(date -d @$FAULT_START '+%Y-%m-%d %H:%M:%S')"

# ---- wait for alert ----
header "Waiting for alert (timeout=${TIMEOUT}s) ..."
alert_found=0
deadline=$(( $(date +%s) + TIMEOUT ))

while [ "$(date +%s)" -lt "$deadline" ]; do
    if [ -f data/alerts.jsonl ]; then
        current_lines=$(wc -l < data/alerts.jsonl)
        if [ "$current_lines" -gt "$DEMO_ALERTS_LINES" ]; then
            alert_found=1
            break
        fi
    fi
    remaining=$(( deadline - $(date +%s) ))
    printf "\r  ..    waiting for alert ... %3ds remaining  " "$remaining"
    sleep 2
done
echo   # newline after the progress line

# ---- wait for fault to finish ----
wait "$FAULT_PID" 2>/dev/null || true

if [ "$alert_found" -eq 1 ]; then
    ALERT_TIME=$(date +%s)
    LATENCY=$(( ALERT_TIME - FAULT_START ))
    ok "alert detected within ${LATENCY}s of fault injection!"

    echo
    echo "--- Latest alert(s) ---"
    tail -n +"$(( DEMO_ALERTS_LINES + 1 ))" data/alerts.jsonl | \
        "$PYTHON" -c "
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    a = json.loads(line)
    print(f\"  time:       {a.get('time_human')}\")
    print(f\"  window:     {a.get('window_time_human')}\")
    print(f\"  cause:      {a.get('cause')}\")
    print(f\"  family:     {a.get('cause_family')}\")
    print(f\"  culprit:    {a.get('culprit')}\")
    print(f\"  basis:      {a.get('basis')}\")
    print()
" 2>/dev/null

    echo "--- Latest RCA report excerpt ---"
    if [ -f data/rca_report.txt ]; then
        tail -40 data/rca_report.txt
    fi
else
    fail "no alert within ${TIMEOUT}s"
fi

# ---- stop daemon ----
header "Stopping daemon"
kill "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true

if pgrep -f 'bpftrace|probeExtractor|procExtractor' > /dev/null 2>&1; then
    pkill -INT -f 'bpftrace|probeExtractor|procExtractor' 2>/dev/null || true
    sleep 3
    pkill -f 'bpftrace' 2>/dev/null || true
fi
ok "daemon stopped"

echo
if [ "$alert_found" -eq 1 ]; then
    ok "DEMO LIVE: end-to-end alert in ${LATENCY}s  ✓"
    exit 0
else
    fail "DEMO LIVE: no alert within timeout"
    exit 1
fi
