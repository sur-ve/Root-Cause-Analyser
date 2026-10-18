#!/usr/bin/env bash
# testbed/run_fault.sh — one guarded fault/skeleton run through parentProcess.py,
# with pre-checks and post-run verification of the collected data.
#
# Usage:
#   sudo bash testbed/run_fault.sh <run_id>
#   DRY_RUN=1 bash testbed/run_fault.sh cpuStressor1     # print plan, no root needed
#
# Guards (PLAN.md phase 4.1):
#   - disk >= 4 GiB free, MemAvailable >= 2 GiB before starting
#   - memory stressors run inside systemd-run --scope with MemoryMax/MemorySwapMax
#     so the host cannot be OOM-killed (memStressor2 becomes a cgroup OOM kill)
#   - after the run: probe/procfs/edges/meta present and non-empty, meta has
#     stressor start/end, ground truth appended, no collector leftovers
#
# Exits non-zero when the run is partial or verification fails (run_all.sh stops).

set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

RUN_ID="${1:-}"
if [ -z "$RUN_ID" ]; then
    echo "usage: sudo bash testbed/run_fault.sh <run_id>"
    exit 2
fi

DRY_RUN="${DRY_RUN:-0}"
PYTHON="${PYTHON:-.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="python3"

ok()   { printf '  ok    %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; }
info() { printf '  ..    %s\n' "$*"; }

echo "== run_fault: $RUN_ID (dry=$DRY_RUN) =="

# ---------------------------------------------------------------- guard: root
if [ "$DRY_RUN" != "1" ] && [ "$(id -u)" -ne 0 ]; then
    bad "must run as root: sudo bash testbed/run_fault.sh $RUN_ID"
    exit 1
fi

# ------------------------------------------------------- guard: disk & memory
free_gib=$(df -BG --output=avail "$PROJECT_DIR" | tail -1 | tr -dc '0-9')
free_gib=${free_gib:-0}
if [ "$free_gib" -lt 4 ]; then
    bad "disk free ${free_gib} GiB < 4 GiB — clean data/ first"
    exit 1
fi
ok "disk free: ${free_gib} GiB"

avail_mib=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
avail_mib=${avail_mib:-0}
if [ "$avail_mib" -lt 2048 ]; then
    bad "MemAvailable ${avail_mib} MiB < 2048 MiB"
    exit 1
fi
ok "MemAvailable: ${avail_mib} MiB"

# ------------------------------------------------- cgroup-guarded stressor cmds
# Memory stressors get a cgroup: the stressor hits its own memory cap instead of
# the host OOM killer (this is what makes memStressor2 demo-safe on a workstation).
MEM_GUARD="systemd-run --scope -p MemoryMax=60% -p MemorySwapMax=1G"
case "$RUN_ID" in
    memStressor1)
        STRESSOR_CMD="$MEM_GUARD stress-ng --vm 2 --vm-bytes 70% --vm-keep --timeout 60s"
        ;;
    memStressor2)
        # ~150%-of-cgroup-limit allocation, repeated: each stress-ng worker is
        # reclaimed against briefly and then OOM-killed *inside* the 2G cgroup
        # (MemorySwapMax=256M bounds host swap use). The loop respawns it so the
        # run shows repeated OOM kills + reclaim pressure instead of one 2s blip.
        STRESSOR_CMD="systemd-run --scope -p MemoryMax=2G -p MemorySwapMax=256M bash -c 'for i in {1..15}; do stress-ng --vm 1 --vm-bytes 2500M --timeout 4s; done'"
        ;;
    memStressor3)
        STRESSOR_CMD="$MEM_GUARD stress-ng --brk 2 --timeout 60s"
        ;;
    *)
        STRESSOR_CMD=""
        ;;
esac

if [ -n "$STRESSOR_CMD" ]; then
    info "guarded stressor: $STRESSOR_CMD"
    CMD=("$PYTHON" parentProcess.py "$RUN_ID" "$STRESSOR_CMD")
else
    CMD=("$PYTHON" parentProcess.py "$RUN_ID")
fi

if [ "$DRY_RUN" = "1" ]; then
    info "would execute: ${CMD[*]}"
    ok "dry run complete"
    exit 0
fi

# ---------------------------------------------------------------- run it
info "collecting (~70s)..."
"${CMD[@]}"
rc=$?
if [ "$rc" -ne 0 ]; then
    bad "parentProcess.py exited with rc=$rc"
    exit 1
fi
ok "parentProcess.py finished"

# ---------------------------------------------------------------- verify
verify_fail=0
for f in "data/probeData_${RUN_ID}.jsonl" "data/procfsData_${RUN_ID}.jsonl" \
         "data/edges_${RUN_ID}.jsonl" "data/meta_${RUN_ID}.json"; do
    if [ -s "$f" ]; then
        ok "$f ($(stat -c %s "$f") bytes)"
    else
        bad "missing or empty: $f"
        verify_fail=1
    fi
done

if [ "$verify_fail" -eq 0 ]; then
    "$PYTHON" - "$RUN_ID" <<'PY' || verify_fail=1
import json, sys
run_id = sys.argv[1]
meta = json.load(open(f'data/meta_{run_id}.json'))
assert meta.get('label'), 'meta missing label'
assert meta.get('stressor_start_time') and meta.get('stressor_end_time'), \
    'meta missing stressor start/end'
assert meta['stressor_end_time'] >= meta['stressor_start_time'], 'stressor end before start'
gt = [json.loads(l) for l in open('data/ground_truth.jsonl') if l.strip()]
hit = [g for g in gt if g.get('run_id') == run_id]
assert hit, f'run {run_id} not appended to ground_truth.jsonl'
print(f"  ok    meta sane (label={meta['label']}, "
      f"duration={meta['stressor_end_time'] - meta['stressor_start_time']:.1f}s)")
PY
fi

sleep 2
if pgrep -f 'bpftrace|procExtractor|probeExtractor|parentProcess' >/dev/null 2>&1; then
    bad "collector processes still running after the run:"
    pgrep -af 'bpftrace|procExtractor|probeExtractor|parentProcess' | sed 's/^/        /'
    verify_fail=1
else
    ok "no collector leftovers"
fi

echo
if [ "$verify_fail" -eq 0 ]; then
    echo "== run_fault OK: $RUN_ID =="
else
    echo "== run_fault FAILED: $RUN_ID =="
fi
exit "$verify_fail"
