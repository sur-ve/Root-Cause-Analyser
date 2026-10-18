#!/usr/bin/env bash
# testbed/run_all.sh — collect the full fault matrix, least -> most destructive.
#
# Usage:
#   sudo bash testbed/run_all.sh              # resume: skips already-collected runs
#   DRY_RUN=1 bash testbed/run_all.sh         # show the plan, no root needed
#   FORCE=1 sudo bash testbed/run_all.sh      # recollect everything
#
# Runs are skipped when data/probeData_<id>.jsonl AND data/edges_<id>.jsonl exist
# and are non-empty, so the script can be re-run after an interruption.

set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

DRY_RUN="${DRY_RUN:-0}"
FORCE="${FORCE:-0}"

# Least -> most destructive: lock, then CPU, then memory (cgroup-guarded), then I/O
ORDER="lockStressor1 lockStressor2 cpuStressor3 cpuStressor1 cpuStressor2 memStressor3 memStressor1 memStressor2 ioStressor1 ioStressor2"

if [ "$DRY_RUN" != "1" ] && [ "$(id -u)" -ne 0 ]; then
    echo "must run as root: sudo bash testbed/run_all.sh"
    exit 1
fi

total=0 passed=0 skipped=0
for rid in $ORDER; do
    total=$((total + 1))
    if [ "$FORCE" != "1" ] && [ -s "data/probeData_${rid}.jsonl" ] && [ -s "data/edges_${rid}.jsonl" ]; then
        echo "== [$rid] already collected — skipping =="
        skipped=$((skipped + 1))
        continue
    fi
    if DRY_RUN="$DRY_RUN" bash testbed/run_fault.sh "$rid"; then
        passed=$((passed + 1))
    else
        echo
        echo "== ABORT: $rid failed — fix the issue and re-run run_all.sh (it resumes) =="
        exit 1
    fi
    if [ "$DRY_RUN" != "1" ]; then
        sleep 5
    fi
done

echo
echo "== run_all done: ${passed} collected, ${skipped} skipped, ${total} total =="
echo "   next: python3 featureExtractor.py (fold new runs into combined_features.csv)"
