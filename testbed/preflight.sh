#!/usr/bin/env bash
# testbed/preflight.sh — environment & safety checks before fault injection,
# collection, or daemon monitoring.
#
# Usage:
#   sudo bash testbed/preflight.sh [--clean]
#
#   --clean   kill stale RCA collectors / bpftrace processes first
#
# Exits non-zero when a hard requirement fails (missing tools, low disk, etc.).

set -u

CLEAN=0
[ "${1:-}" = "--clean" ] && CLEAN=1

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

fail=0
ok()   { printf '  ok    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; fail=1; }

echo "== RCA preflight ($PROJECT_DIR) =="

# ---------------------------------------------------------------- privilege
if [ "$(id -u)" -ne 0 ]; then
    bad "must run as root: sudo bash testbed/preflight.sh"
    echo
    exit 1
fi
ok "running as root"

# ---------------------------------------------------------------- tools
for t in bpftrace stress-ng fio systemd-run stdbuf; do
    if command -v "$t" >/dev/null 2>&1; then
        ok "tool: $t"
    else
        bad "missing tool: $t (install it before fault injection)"
    fi
done

# ---------------------------------------------------------------- PSI
for f in /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io; do
    if [ -r "$f" ]; then
        ok "$f readable"
    else
        bad "$f missing (kernel >= 4.20 required)"
    fi
done

# ------------------------------------------- scheduler stats (rq_time probes)
SCHEDSTATS=/proc/sys/kernel/sched_schedstats
if [ -f "$SCHEDSTATS" ]; then
    if [ "$(cat "$SCHEDSTATS")" = "1" ]; then
        ok "kernel.sched_schedstats=1 (sched_stat_wait/iowait probes active)"
    else
        if sysctl -w kernel.sched_schedstats=1 >/dev/null 2>&1; then
            ok "kernel.sched_schedstats enabled for this boot"
        else
            warn "could not enable kernel.sched_schedstats — rq_time/iowait_time will be missing"
        fi
    fi
fi

# ---------------------------------------------------------------- disk space
free_gib=$(df -BG --output=avail "$PROJECT_DIR" | tail -1 | tr -dc '0-9')
free_gib=${free_gib:-0}
if [ "$free_gib" -ge 8 ]; then
    ok "disk free: ${free_gib} GiB"
elif [ "$free_gib" -ge 4 ]; then
    warn "disk free: ${free_gib} GiB (the 10-fault matrix wants >= 8 GiB)"
else
    bad "disk free: ${free_gib} GiB — clean data/ (old *.jsonl) before collecting"
fi

# ---------------------------------------------------------------- memory
avail_mib=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
avail_mib=${avail_mib:-0}
if [ "$avail_mib" -ge 2048 ]; then
    ok "MemAvailable: ${avail_mib} MiB"
else
    bad "MemAvailable: ${avail_mib} MiB — too low for memory stressors"
fi

# ---------------------------------------------------------------- stale procs
stale=$(pgrep -af 'bpftrace|procExtractor|probeExtractor|rcaDaemon|parentProcess' || true)
if [ -n "$stale" ]; then
    if [ "$CLEAN" = "1" ]; then
        pkill -INT -f 'bpftrace|procExtractor|probeExtractor|rcaDaemon|parentProcess' || true
        sleep 2
        pkill -f 'bpftrace' || true
        ok "stale collectors cleaned (--clean)"
    else
        warn "stale collector-like processes are running (use --clean to stop them):"
        echo "$stale" | sed 's/^/        /'
    fi
else
    ok "no stale collectors"
fi

# ---------------------------------------------------------------- bpftrace smoke
echo "  ..    bpftrace smoke test (5s)"
smoke=$(mktemp)
if timeout 5 bpftrace -q -e 'BEGIN { printf("smoke-ok\n"); exit(); }' >"$smoke" 2>&1 && grep -q 'smoke-ok' "$smoke"; then
    ok "bpftrace attaches and runs"
else
    bad "bpftrace smoke test failed:"
    sed 's/^/        /' "$smoke" | head -5
fi
rm -f "$smoke"

# ---------------------------------------------------------------- probe scripts smoke
check_script() {
    # usage: check_script <script.bt> [expected-marker] [extra bpftrace flags]
    local script="$1" marker="${2:-}" extra="${3:-}"
    local out; out=$(mktemp)
    echo "  ..    smoke test: $script (8s)"
    timeout 8 stdbuf -oL bpftrace -q $extra "$script" >"$out" 2>&1
    local rc=$?
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 124 ]; then
        bad "$script failed to run (rc=$rc):"
        head -3 "$out" | sed 's/^/        /'
    elif grep -q 'ERROR' "$out"; then
        bad "$script reported an error:"
        head -3 "$out" | sed 's/^/        /'
    elif [ -n "$marker" ] && ! grep -q "$marker" "$out"; then
        bad "$script produced no '$marker' marker (check script syntax / probes)"
        head -3 "$out" | sed 's/^/        /'
    else
        if [ -n "$marker" ]; then
            ok "$script runs and emits '$marker'"
        else
            ok "$script runs"
        fi
    fi
    rm -f "$out"
}

if [ -f probes.bt ]; then
    check_script probes.bt "BEGIN_NS" "-f json"
else
    bad "missing script: probes.bt"
fi

if [ -f wakeupTracker.bt ]; then
    check_script wakeupTracker.bt
else
    bad "missing script: wakeupTracker.bt"
fi

# Daemon edge tracker: same schema, 1-in-1000 sampling (bounded disk growth)
if [ -f wakeupTrackerMonitor.bt ]; then
    check_script wakeupTrackerMonitor.bt
else
    bad "missing script: wakeupTrackerMonitor.bt (used by rcaDaemon.py)"
fi

echo
if [ "$fail" -eq 0 ]; then
    echo "== preflight OK =="
else
    echo "== preflight FAILED — fix the items above =="
fi
exit "$fail"
