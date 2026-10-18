# RCA — Implementation Guide

Phase-by-phase guide for building, validating, and operating the RCA system
from a fresh checkout. All commands are run from the project root unless noted.

---

## Phase 0 — Environment

### 0.1 System prerequisites

```bash
# Debian / Ubuntu
sudo apt install -y bpftrace python3 python3-venv python3-pip \
    stress-ng fio stdbuf systemd

# Verify kernel features
ls /proc/pressure/cpu          # PSI (kernel ≥ 4.20)
bpftrace --version             # 0.16+ recommended (0.20.x tested)
```

> [!IMPORTANT]
> All collection scripts require root (`sudo`) for bpftrace and cgroup scope management.
> `testbed/demo_replay.py` and `testbed/evaluate.py` do **not** need root.

### 0.2 Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 0.3 Environment checks (preflight)

```bash
sudo bash testbed/preflight.sh --clean
```

Checks: root, tools, PSI, `sched_schedstats`, disk (≥ 4 GiB), memory (≥ 2 GiB),
stale collectors, bpftrace smoke test, and per-script sanity runs for
`probes.bt`, `wakeupTracker.bt`, and `wakeupTrackerMonitor.bt`.

---

## Phase 1 — Collection layer

### 1.1 Collect one baseline run

```bash
sudo python3 parentProcess.py default
```

Artefacts produced:

| File | Content |
|---|---|
| `data/probeData_default.jsonl` | eBPF probe windows (process-level metrics) |
| `data/procfsData_default.jsonl` | PSI + vmstat samples (1 s interval) |
| `data/edges_default.jsonl` | sched_wakeup edges (1-in-100 sampled) |
| `data/meta_default.json` | `boot_time_epoch`, timestamps, label=Normal |
| `data/ground_truth.jsonl` | appended: run metadata + stressor start/end |

Verify:
```bash
ls -lh data/probeData_default.jsonl data/edges_default.jsonl
cat data/meta_default.json | python3 -m json.tool
```

### 1.2 Collect the full fault matrix

```bash
# Recommended: preflight first, then run all 10 fault classes
sudo bash testbed/run_all.sh
```

`run_all.sh` runs `testbed/run_fault.sh <id>` for each stressor in
least-→-most-destructive order and skips runs that already have data
(safe to resume after an interruption).

`run_fault.sh` wraps memory stressors in `systemd-run --scope` cgroups
(`MemoryMax` / `MemorySwapMax`) so OOM kills stay inside the scope and
cannot take the host down.

**`memStressor2` note:** runs a supervisor loop of 20 × 2.5G allocations,
each inside its own 2G/256M-swap scope. Each allocation is OOM-killed; the
loop keeps the pressure active for ~40 s so the probes have time to record
the `oom_kill_process` tracepoint hits.

---

## Phase 2 — Feature extraction

```bash
python3 featureExtractor.py
```

Produces `data/combined_features.csv` — one row per `(window, process_comm)`:

| Column group | Source |
|---|---|
| `window_start`, `window_end` | bpftrace clock markers (`BEGIN_NS`, `WINDOW_END_NS`) + `boot_time_epoch` |
| `comm` | process command name |
| `vfsreadtime`, `runtime`, `sector_count`, `futex_*`, `locktime`, `rq_time`, `iowait_time`, `oom_time` | `probes.bt` maps |
| `cpu_some_avg10`, `mem_some_avg10`, `io_some_avg10` | `/proc/pressure/*` |
| `allocstall_count`, `major_fault_count`, `swap_in_count`, `swap_out_count`, `oom_kill_count` | `/proc/vmstat` deltas |
| `root_cause` | `label_for_run_id()` — meta file first, then `labels.ROOT_CAUSES`, then `Unlabeled` |

Current corpus: **71 777 rows** across 16 runs (6 baselines + 10 faults).

---

## Phase 3 — Anomaly detection

### 3.1 Train the model

```bash
python3 anomalyDetection.py --train
```

Trains a **OneClassSVM** (`kernel=rbf`, `nu=0.05`) on Normal rows only
(≥ 50 required; capped at 3 000 for RBF tractability). Saves the artifact:

```
data/model/ocsvm.joblib   # scaler + model + feature_cols + metadata
```

### 3.2 Score all windows

```bash
python3 anomalyDetection.py --score
```

Adds `anomaly_score` (decision function) and `predicted_anomaly` columns.
Writes `data/scored_features.csv`.

**Current numbers** (`detectorBenchmark.py` / `overallBenchmark.py`):

| Detector | F1 | FPR | Notes |
|---|---|---|---|
| **OneClassSVM** | **0.982** | **5.4 %** | Production |
| LocalOutlierFactor | 0.919 | 26.1 % | Comparison |
| IsolationForest | 0.867 | 8.9 % | Prior baseline |

Held-out test (30 %, stratified): F1 **0.981**, FPR 5.7 %, per-class recall
≈ 1.0 for 9/10 classes and 0.999 for the 10th (FPs 488, FNs 1).

---

## Phase 4 — Causal graphs

```bash
python3 analyzeCausalGraphs.py
```

1. Loads `predicted_anomaly == True` windows from `scored_features.csv`.
2. Pads ±1 s, merges overlapping intervals **per run** (edges are per-run).
3. Reads `data/edges_<run_id>.jsonl` — aligns `nsecs`-since-boot to wall
   clock via `data/meta_<run_id>.json` (`boot_time_epoch`).
4. Filters noise comms: `swapper/*`, `bpftrace`, `ksoftirqd/*`,
   `migration/*`, `rcu_*`, `cpuhp/*`.
5. Builds `Wakee → Waker` directed graph; candidate roots are terminal
   blockers (`in_degree > 0` and `out_degree == 0`).
6. Writes `data/causal_summaries.json` and `data/causal_graphs.json`.

---

## Phase 5 — RAG explanation

```bash
python3 ragExplainer.py
```

For each anomaly episode from `causal_summaries.json`:

1. **TF-IDF retrieval** — cosine similarity against `knowledge/*.md`.
2. **Label-free classification** (`diagnosis.py`) — tiers:
   culprit process comm → OOM-kill event → `stress-ng-vm` identity →
   metric signatures (futex, locktime, PSI, swap, sectors, CPU-PSI) → `unknown`.
3. **Grounded report** — cause + evidence points + remediation from runbook.
4. **Optional LLM review** — if `RCA_LLM_API_KEY` is set, appends an
   LLM-reviewed section (OpenAI-compatible API, any provider).

Writes `data/rca_report.txt` (human-readable) and `data/rca_report.json`.

### Enabling the LLM layer

The LLM review is optional and supports cloud APIs or local Ollama / vLLM instances:

**Cloud API (OpenAI, Groq, OpenRouter, Together):**
```bash
export RCA_LLM_API_KEY=sk-...
export RCA_LLM_BASE_URL=https://api.groq.com/openai/v1    # optional
export RCA_LLM_MODEL=llama-3.3-70b-versatile              # optional
python3 ragExplainer.py
```

**Local Ollama (no API key required):**
```bash
export RCA_LLM_BASE_URL=http://localhost:11434/v1
export RCA_LLM_MODEL=llama3.2
python3 ragExplainer.py
```

**Run LLM mock unit tests:**
```bash
python3 testbed/test_llm.py
```

The template report is always produced; the LLM adds a strictly bounded review section. If the API call fails or is unreachable, the template is used alone without interruption. In background daemon mode (`rcaDaemon.py`), the LLM review is also automatically included in finalized episode reports when configured.

---

## Phase 6 — One-command offline pipeline

```bash
python3 run_pipeline.py
```

Runs Phases 2–5 in order. Continues past stage failures so later stages
can still use existing outputs.

---

## Phase 7 — Benchmarks

```bash
python3 detectorBenchmark.py          # OCSVM vs IF vs LOF side-by-side
python3 overallBenchmark.py           # Hold-out accuracy + cause-family agreement
python3 diagnosis.py data/combined_features.csv   # Window-level family accuracy
```

All three use `diagnosis.py` for cause classification and never peek at
ground-truth labels during the classification step (labels are only used
for computing accuracy at the end).

---

## Phase 8 — Background daemon

### 8.1 Learn (collect host-normal baseline)

```bash
sudo .venv/bin/python rcaDaemon.py --learn 300
```

Collects 300 s of idle activity, appends rows to `data/daemon_baseline.csv`,
and trains `data/model/ocsvm_daemon.joblib`.

### 8.2 Monitor (continuous)

```bash
sudo .venv/bin/python rcaDaemon.py --monitor \
    --interval 30 --rotate 900 --retain-hours 24
```

Each cycle:
1. Read new probe/procfs bytes (byte offsets from `daemon_state.json`).
2. Score with the persisted daemon model (fallback: batch model).
3. Flag windows with ≥ 50 % anomalous rows.
4. Finalize complete episodes → causal graph + grounded report.
5. Append alerts to `data/alerts.jsonl`; update `data/rca_report.txt`.
6. Heartbeat → `data/daemon_status.json`.

Graceful shutdown (SIGINT / SIGTERM) finalizes the active run and persists
last-scored offsets; a restart continues without reprocessing.

### 8.3 Offline smoke test (no root)

```bash
.venv/bin/python rcaDaemon.py --no-collect \
    --runs memStressor2,default1 --reprocess
```

Logs one line per stage (featurize / score / graph / explain) per run.

### 8.4 systemd unit

```bash
sudo cp deploy/rca-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rca-daemon.service
sudo systemctl status rca-daemon.service
```

The unit has `CAP_BPF` / `CAP_PERFMON` capability bounding, `SIGTERM`
graceful stop, and restart-on-failure.

---

## Phase 9 — Evaluation & demo

### 9.1 Per-fault evaluation (no root)

```bash
python3 testbed/evaluate.py
```

Produces `data/evaluation_results.csv` and `data/demo_metrics.json`.
Current measured numbers:

| Metric | Value |
|---|---|
| Fault detection recall | 1.000 (10/10 fault runs) |
| Normal FPR | 0.000 (0 of 6 baselines flagged) |
| Top-1 family accuracy | 1.000 (label-free path) |
| MTTR | 2.5 s mean |

### 9.2 Offline demo replay (no root, ~11 s)

```bash
python3 testbed/demo_replay.py
```

Runs the full pipeline on the corpus and prints a per-fault results table
plus a sample RCA report. See `DEMO.md` for expected output.

### 9.3 Live end-to-end demo (root required, ~3 min)

```bash
sudo bash testbed/demo_live.sh
```

Starts the daemon, injects `cpuStressor1` (safe, 45 s), waits for an alert
in `data/alerts.jsonl`, prints it and the latest report excerpt, then stops
the daemon cleanly.

---

## Retraining after new data

```bash
# After sudo bash testbed/run_all.sh collects new runs:
python3 featureExtractor.py                    # merge new runs into combined_features.csv
python3 anomalyDetection.py --train            # refit model on updated Normal rows
python3 anomalyDetection.py --score            # score everything
python3 analyzeCausalGraphs.py                 # rebuild causal summaries
python3 ragExplainer.py                        # regenerate reports
python3 testbed/evaluate.py                    # verify numbers
```

---

## Key design decisions

| Decision | Rationale |
|---|---|
| OneClassSVM over IsolationForest | Better F1 (0.982 vs 0.867) and lower FPR (5.4 % vs 8.9 %) on this corpus |
| Persisted `joblib` artifact | Daemon only scores; no refit needed per cycle |
| Per-run edge files | Eliminates 3 GB global `edges.jsonl`; no cross-run alignment needed |
| 1-in-100 wakeup sampling | Reduces 530 MB/min to ~2 MB/run; graph quality unaffected for RCA |
| Majority-rule window flagging | Avoids false alerts at ~3 % row-level FPR; fault windows are ≥ 0.94 flagged |
| Label-free diagnosis path | Accuracy figures stand up to scrutiny — no label peeking in production |
| OpenAI-compatible LLM API | No local model install; grounded template is the always-on fallback |
