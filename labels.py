"""labels.py — single source of truth for stressor → ground-truth labels.

Used by:
  - parentProcess.py   → writes ground truth (label + family) into meta / ground_truth.jsonl
  - featureExtractor.py → labels collected runs for evaluation
  - testbed/evaluate.py → maps predictions to coarse cause families

Ground-truth labels are for EVALUATION ONLY. The production explainer must not
lean on them (see ragExplainer.py / PLAN.md phase 2.4).
"""

ROOT_CAUSES = {
    'cpuStressor1': 'CPU Competition',
    'cpuStressor2': 'Process Throttled',
    'cpuStressor3': 'Baseline CPU Usage',
    'memStressor1': 'Memory Swapping',
    'memStressor2': 'OOM Kill',
    'memStressor3': 'Continuous Heap Expansion',
    'lockStressor1': 'Userspace Contention',
    'lockStressor2': 'Kernel-Internal Contention',
    'ioStressor1': 'Small Random Writes',
    'ioStressor2': 'Large Sequential Writes',
    'default': 'Normal',
}

# Repeated baseline runs use indexed run_ids (default1, default2, ...) so their
# per-run files don't overwrite each other; every one of them means 'Normal'.
for _i in range(1, 9):
    ROOT_CAUSES.setdefault(f'default{_i}', 'Normal')

# Coarse cause families used by the evaluation table (testbed/evaluate.py)
FAULT_FAMILIES = {
    'Normal': 'normal',
    'CPU Competition': 'cpu',
    'Process Throttled': 'cpu',
    'Baseline CPU Usage': 'cpu',
    'Memory Swapping': 'memory',
    'OOM Kill': 'oom',
    'Continuous Heap Expansion': 'heap',
    'Userspace Contention': 'lock',
    'Kernel-Internal Contention': 'lock',
    'Small Random Writes': 'io',
    'Large Sequential Writes': 'io',
}


def label_for_run(run_id: str) -> str:
    """Ground-truth label for a run_id ('Custom (...)' when unknown)."""
    return ROOT_CAUSES.get(run_id, f'Custom ({run_id})')


def family_for_label(label: str) -> str:
    """Coarse cause family for a ground-truth label."""
    return FAULT_FAMILIES.get(label, 'unknown')
