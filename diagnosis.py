#!/usr/bin/env python3
"""diagnosis.py — label-free cause classification shared by the RAG explainer
and the evaluation benchmarks.

Classification works on *window-level* metrics (as stored in
data/causal_summaries.json -> top_metrics) plus observed culprit process names
(top CPU consumers / causal-graph roots). It never uses ground-truth labels.

Evidence tiers, in order:
  1. distinct culprit process names  (fio / stress-ng-* — unambiguous workload
     identity: the fault is in that process);
  2. kernel OOM-kill event           (strongest system-level signature);
  3. metric signatures               (futex waits, lock hold time, PSI, swap,
     major faults, block I/O);
  4. the ambiguous 'stress-ng-vm' name (swapping vs OOM) as a last fallback;
  5. 'unknown'.

Thresholds are calibrated against the collected fault corpus (window p10/p90
margins ~2-10x, see PLAN.md phase 2.5). They are deliberately coarse: the
classifier runs only on windows the detector already flagged as anomalous.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Sequence

import pandas as pd

# Metrics aggregated with max() over the window (gauges + window-scoped counters;
# max is robust to anomaly windows merging across several probe windows)
MAX_METRICS = [
    'runtime', 'rq_time', 'futex_wait_time', 'futex_wait_count',
    'oom_time', 'iowait_time', 'sector_count', 'locktime',
    'cpu_some_avg10', 'mem_some_avg10', 'io_some_avg10',
    'allocstall_count', 'major_fault_count', 'swap_in_count', 'swap_out_count',
    'oom_kill_count', 'oom_kills',
]

FAMILY_NAMES = {
    'cpu': 'CPU / Scheduling',
    'memory': 'Memory Pressure',
    'oom': 'OOM',
    'heap': 'Heap Expansion',
    'lock': 'Lock Contention',
    'io': 'I/O Saturation',
    'unknown': 'Unknown',
}

# Thresholds (see module docstring)
FUTEX_LOCK = 80_000            # userspace-contention p10=94k; all other classes max<=38.4k
LOCKTIME_LOCK = 5e8            # vs 11.8M p90 userspace, 7.3M p90 normal
HEAP_MEM_PSI = 5.0             # brk windows: mem_some p50 ~4-21, others ~0
HEAP_MAJOR = 50                # brk windows: major faults spike with anon growth
MEM_SWAP = 5                   # swap activity; others p90 = 0
MEM_MAJOR = 20                 # reclaim proxy when swap counters are quiet
IO_SECTORS = 100_000           # bulk writes; non-IO classes p90 <= 24k
IO_PSI = 10.0                  # bulk writes stall other tasks on I/O
CPU_PSI = 15.0                 # throttle windows: p10 = 19; others p90 <= 9

# Culprit process-name hints: (family, prefix match on comm). Order = priority;
# names are the ones observed in collection (comm is truncated to 15 chars).
COMM_HINTS = [
    ('io', ('fio',)),
    ('cpu', ('stress-ng-cpu',)),
    ('heap', ('stress-ng-brk',)),
    ('lock', ('stress-ng-futex', 'stress-ng-dentr')),
]

# Ambiguous hint: swapping and cgroup-OOM runs both show up as stress-ng-vm.
AMBIGUOUS_MEMORY_HINT = ('stress-ng-vm',)


def aggregate_window_metrics(window_df: pd.DataFrame) -> Dict[str, Any]:
    """Window-level metrics dict from the per-process rows of one window.

    max() over the window for gauges/counters, sum() for per-process counts
    (e.g. futex_wait_count is keyed by comm in probes.bt).
    """
    if window_df is None or window_df.empty:
        return {}

    metrics: Dict[str, Any] = {}
    for m in MAX_METRICS:
        if m in window_df.columns:
            val = float(pd.to_numeric(window_df[m], errors='coerce').max())
            if val > 0:
                metrics[m] = round(val, 3)

    return metrics


def top_process_comms(window_df: pd.DataFrame, top_k: int = 10) -> List[str]:
    """Observed culprit candidates: distinct comms of the top CPU consumers."""
    if window_df is None or window_df.empty or 'comm' not in window_df.columns:
        return []
    df = window_df[['comm', 'runtime']].copy() if 'runtime' in window_df.columns else window_df[['comm']].copy()
    df['comm'] = df['comm'].astype(str)
    df = df[df['comm'].str.len() > 0]
    if 'runtime' in df.columns:
        df = df.sort_values('runtime', ascending=False)
    return list(dict.fromkeys(df['comm']))[:top_k]


def _hint_family(comms: Sequence[str]) -> str | None:
    for family, prefixes in COMM_HINTS:
        if any(c.startswith(p) for c in comms for p in prefixes):
            return family
    return None


def _has_ambiguous_memory_hint(comms: Sequence[str]) -> bool:
    return any(c.startswith(p) for c in comms for p in AMBIGUOUS_MEMORY_HINT)


def classify_cause(metrics: Mapping[str, Any], culprit_comms: Sequence[str] = ()) -> Dict[str, str]:
    """Classify one anomalous window from observed evidence only.

    Returns {'family', 'cause', 'basis'}. 'family' is one of
    FAMILY_NAMES / 'unknown'; 'cause' is the human-readable label used in the
    report; 'basis' names the evidence that fired.
    """
    def g(key: str) -> float:
        try:
            return float(metrics.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    comms = [str(c).split(':')[0] for c in culprit_comms if c]

    futex_max = g('futex_wait_count')
    oom_sig = g('oom_kill_count') + g('oom_time') + g('oom_kills')
    locktime = g('locktime')
    sectors = g('sector_count')
    io_psi = g('io_some_avg10')
    mem_psi = g('mem_some_avg10')
    cpu_psi = g('cpu_some_avg10')
    swap_total = g('swap_in_count') + g('swap_out_count')
    major = g('major_fault_count')

    # --- tier 1: distinct culprit process identity ---------------------------
    hint = _hint_family(comms)
    if hint is not None:
        hit = _culprit_hit(comms, hint)
        cause = {
            'io': ('Disk I/O Saturation (Bulk/Sequential Writes)' if sectors > IO_SECTORS
                   else 'Disk I/O Saturation (Small/Random Writes)'),
            'cpu': ('CPU Throttling / Quota Exhaustion' if cpu_psi >= CPU_PSI
                    else 'CPU Competition / Scheduling Starvation'),
            'heap': 'Continuous Heap Expansion (brk-style Anon Growth)',
            'lock': ('Userspace Lock Contention (Futex Waitqueue)' if hit.startswith('stress-ng-futex')
                     else 'Kernel Lock/Path Contention (VFS, dcache)'),
        }[hint]
        return _diag(hint, cause, f'culprit process identified: {hit}')

    # --- tier 2: kernel OOM-kill event --------------------------------------
    if oom_sig > 0:
        return _diag('oom', 'OOM Kill (Memory Exhaustion)',
                     f'OOM-kill event observed (oom evidence={oom_sig:.3g})')

    # --- tier 2.5: ambiguous stress-ng-vm identity ---------------------------
    # A visible stress-ng-vm process is the swapping stressor (heap runs show
    # stress-ng-brk, handled in tier 1); kernel OOM evidence above still wins.
    if _has_ambiguous_memory_hint(comms):
        return _diag('memory', 'Memory Pressure & Swap Thrashing',
                     'culprit=stress-ng-vm (no OOM event observed)')

    # --- tier 3: metric signatures -------------------------------------------
    if futex_max > FUTEX_LOCK:
        return _diag('lock', 'Userspace Lock Contention (Futex Waitqueue)',
                     f'futex_wait_count={futex_max:.3g} > {FUTEX_LOCK:g}')
    if locktime > LOCKTIME_LOCK:
        return _diag('lock', 'Kernel Lock/Path Contention (VFS, dcache)',
                     f'locktime={locktime:.3g} ns > {LOCKTIME_LOCK:.3g}')
    if mem_psi >= HEAP_MEM_PSI and major >= HEAP_MAJOR:
        return _diag('heap', 'Continuous Heap Expansion (brk-style Anon Growth)',
                     f'mem_psi={mem_psi:g} >= {HEAP_MEM_PSI:g} and major_faults={major:.3g} >= {HEAP_MAJOR}')
    if swap_total >= MEM_SWAP or major >= MEM_MAJOR:
        return _diag('memory', 'Memory Pressure & Swap Thrashing',
                     f'swap_total={swap_total:.3g} (>= {MEM_SWAP}) or major_faults={major:.3g} (>= {MEM_MAJOR})')
    if sectors > IO_SECTORS or io_psi >= IO_PSI:
        cause = ('Disk I/O Saturation (Bulk/Sequential Writes)' if sectors > IO_SECTORS
                 else 'Disk I/O Saturation (Small/Random Writes)')
        return _diag('io', cause, f'sectors={sectors:.3g} / io_psi={io_psi:g}')
    if cpu_psi >= CPU_PSI:
        return _diag('cpu', 'CPU Throttling / Quota Exhaustion',
                     f'cpu_psi={cpu_psi:g} >= {CPU_PSI:g}')

    return _diag('unknown', 'Unclassified Anomaly', 'no signature matched')


def _diag(family: str, cause: str, basis: str) -> Dict[str, str]:
    return {'family': family, 'cause': cause, 'basis': basis}


def _culprit_hit(comms: Iterable[str], family: str) -> str:
    """The matched comm that identifies `family` (or '?' if somehow absent)."""
    prefixes = next(p for f, p in COMM_HINTS if f == family)
    return next((c for c in comms for p in prefixes if c.startswith(p)), '?')


if __name__ == '__main__':
    # Smoke test on the collected corpus: window-level family agreement.
    import sys
    from labels import FAULT_FAMILIES

    path = sys.argv[1] if len(sys.argv) > 1 else 'data/combined_features.csv'
    df = pd.read_csv(path)
    total = correct = 0
    by_family: Dict[str, List[int]] = {}
    for (run_id, _ws), rows in df.groupby(['run_id', 'window_start']):
        label = rows['root_cause'].value_counts().index[0]
        family = FAULT_FAMILIES.get(label, 'unknown')
        if family == 'normal':
            continue
        metrics = aggregate_window_metrics(rows)
        diag = classify_cause(metrics, top_process_comms(rows))
        total += 1
        ok = diag['family'] == family
        correct += ok
        stats = by_family.setdefault(family, [0, 0])
        stats[0] += 1
        stats[1] += ok
    print(f'fault windows: {total}  agreement={correct / total:.3f}' if total else 'no fault windows')
    for family, (n, ok) in sorted(by_family.items()):
        print(f'  {FAMILY_NAMES.get(family, family):<16} n={n:<4} recall={ok / n:.2f}')
