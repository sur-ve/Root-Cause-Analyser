# RCA System — Evaluator Demo Guide

> [!IMPORTANT]
> This is the click-through guide for the evaluator. Follow steps **in order**.
> Phases 1–2 (no root) take ~20 seconds. Phase 3 (root) takes ~2–3 minutes.

---

## Phase 1 — Offline Replay (no root, ~10 s)

```bash
python3 testbed/demo_replay.py
```

**What to look for:**
- Pipeline stages complete without errors: `[1/4]` features → `[2/4]` score → `[3/4]` graph → `[4/4]` explain
- Sample RCA report for a fault episode (cause, evidence, remediation)
- Results table at the bottom — every fault run should show `YES` in `Det?` column and `ok` in `Match`

Expected output (abridged):
```
== RCA PIPELINE — OFFLINE DEMO REPLAY ==
[1/4] Loading features from data/combined_features.csv ...
[2/4] Scoring with persisted model ...
[3/4] Building causal graphs ...
[4/4] Generating RCA reports ...

--- SAMPLE RCA REPORT ---
=== RCA Report: lockStressor1_w1 ===
Likely root cause: Userspace Lock Contention (Futex Waitqueue)
Primary culprit process: stress-ng-futex
Evidence:
  - Futex waits: 1588194 calls, 106986268763 ns total.
  - Classification basis: culprit process identified: stress-ng-futex.
  - Causal graph terminal blocker: `stress-ng-futex:99132` (most-blocked root in window).
  - Matched knowledge runbook 'lock_contention' ...
Suggested action: Use fine-grained locking or lock-free data structures (e.g. RCU, atomic operations).

==============================================================================
  DEMO REPLAY — PER-FAULT RESULTS TABLE
==============================================================================
Run ID               True Label                   Det?  Pred Family  Match
------------------------------------------------------------------------------
lockStressor1        Userspace Contention          YES         lock     ok
lockStressor2        Kernel-Internal Contention    YES         lock     ok
cpuStressor1         CPU Competition               YES          cpu     ok
cpuStressor2         Process Throttled             YES          cpu     ok
cpuStressor3         Baseline CPU Usage            YES          cpu     ok
memStressor1         Memory Swapping               YES       memory     ok
memStressor2         OOM Kill                      YES          oom     ok
memStressor3         Continuous Heap Expansion     YES         heap     ok
ioStressor1          Small Random Writes           YES           io     ok
ioStressor2          Large Sequential Writes       YES           io     ok
default              Normal                         no      unknown    N/A
default1             Normal                         no      unknown    N/A
default2             Normal                         no      unknown    N/A
default3             Normal                         no      unknown    N/A
default4             Normal                         no       memory    N/A
default5             Normal                         no      unknown    N/A
==============================================================================

Fault detection recall:    1.000  (10/10)
Top-1 family accuracy:     1.000  (label-free path, fault runs only)
```

---

## Phase 2 — Evaluate Accuracy (no root, ~3 s)

```bash
python3 testbed/evaluate.py
```

**What to look for:**
- Per-fault table printed to stdout
- `data/evaluation_results.csv` created
- `data/demo_metrics.json` created with Top-1 accuracy 1.000 and recall 1.000

Expected output (abridged):
```
========================================================================
  EVALUATION RESULTS
========================================================================
Run ID                 Label                          Det?  Fam match  Latency(s)
------------------------------------------------------------------------
cpuStressor1           CPU Competition                 YES         ok         2.4
cpuStressor2           Process Throttled               YES         ok         2.5
cpuStressor3           Baseline CPU Usage              YES         ok         2.5
default                Normal                           no        N/A           -
...
ioStressor1            Small Random Writes             YES         ok         2.3
ioStressor2            Large Sequential Writes         YES         ok         2.3
lockStressor1          Userspace Contention            YES         ok         3.2
lockStressor2          Kernel-Internal Contention      YES         ok         2.7
memStressor1           Memory Swapping                 YES         ok         2.6
memStressor2           OOM Kill                        YES         ok         2.4
memStressor3           Continuous Heap Expansion       YES         ok         2.6
========================================================================

Detection recall:          1.000  (10/10 fault runs detected)
Normal FP rate:            0.000  (0 normal runs flagged)
Top-1 family accuracy:     1.000  (label-free path)
MTTR (mean latency):       2.5s  (10 measurements)
```

Check the JSON metrics:
```bash
cat data/demo_metrics.json
```

---

## Phase 3 — Live Background Proof (root required, ~2–3 min)

```bash
sudo bash testbed/demo_live.sh
```

**What to look for:**
- `collectors started` → monitor running in background
- `Injecting fault: cpuStressor1 ...`
- `alert detected within Ns of fault injection!` where N ≤ 60 s
- Alert details printed (cause, culprit, family)
- RCA report excerpt showing CPU evidence and remediation
- `DEMO LIVE: end-to-end alert in Ns ✓`

**If the alert takes > 60 s:** this is expected if the cycle interval is 20 s and the fault starts right after a cycle; the default timeout is 120 s.

---

## Key Claims (all verifiable from the above)

| Claim | How to verify |
|---|---|
| Detector recall = 1.000 on 10 fault classes | `evaluate.py` output |
| Normal FPR = 0.000 (0 of 6 baselines flagged) | `evaluate.py` output |
| Top-1 family accuracy = 1.000 (label-free) | `evaluate.py` → `demo_metrics.json` |
| Alert latency (MTTR) mean 2.5 s | `evaluate.py` → `demo_metrics.json` |
| Background daemon detects a live fault | `demo_live.sh` |
| Reports use metrics/graph/retrieval (no label peeking) | `ragExplainer.py` line 190: `# Ground-truth labels are for evaluation...` |
| Pipeline runs end-to-end in < 10 s (no bpftrace) | `demo_replay.py` elapsed time |

---

## Checking the Data

```bash
# How many rows in the corpus?
wc -l data/combined_features.csv

# How many Normal rows?
python3 -c "
import pandas as pd
df = pd.read_csv('data/combined_features.csv')
print(df['root_cause'].value_counts())
"

# Ground truth entries
cat data/ground_truth.jsonl | python3 -m json.tool | head -30

# Latest RCA report
cat data/rca_report.txt | tail -60

# Latest alerts (from live demo or daemon)
tail -5 data/alerts.jsonl | python3 -c "import json,sys; [print(json.dumps(json.loads(l),indent=2)) for l in sys.stdin if l.strip()]"
```

---

## LLM Review Layer (optional)

The RCA pipeline includes an evidence-grounded LLM review layer supporting any OpenAI-compatible cloud provider (OpenAI, Groq, OpenRouter, Together) or local Ollama / vLLM instances:

### Option A: Cloud API (e.g. OpenAI or Groq)
```bash
export RCA_LLM_API_KEY=sk-...
export RCA_LLM_BASE_URL=https://api.groq.com/openai/v1   # optional
export RCA_LLM_MODEL=llama-3.3-70b-versatile             # optional
python3 ragExplainer.py
```

### Option B: Local Ollama (no API key required)
```bash
export RCA_LLM_BASE_URL=http://localhost:11434/v1
export RCA_LLM_MODEL=llama3.2                           # or mistral, qwen2.5, etc.
python3 ragExplainer.py
```

### Test LLM Integration
Run the mock unit test suite (no external services or API keys needed):
```bash
python3 testbed/test_llm.py
```

The grounded template report is always produced; the LLM adds a short, strictly bounded review section at the end. If the API call fails or is unreachable, the template is used alone without interruption.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No model found` | `python3 anomalyDetection.py --train` |
| `bpftrace failed` (demo_live.sh) | `sudo bash testbed/preflight.sh --clean` |
| `disk free < 2 GiB` | Delete old data: `rm data/edges_rolling_*.jsonl data/probeData_rolling_*.jsonl` |
| Alert takes > 120 s | Reduce `--interval`: `sudo bash testbed/demo_live.sh --interval 10` |
| `collectors did not start` | Check `data/demo_daemon.log`; run preflight first |
