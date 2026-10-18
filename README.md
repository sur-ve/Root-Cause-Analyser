# Root Cause Analyser (RCA)

End-to-end Linux root-cause analysis system that:

1. Collects kernel (eBPF / bpftrace) and procfs (PSI + vmstat) telemetry while optional stressors run
2. Detects anomalous time windows with a **OneClassSVM** model (selected via detector benchmark)
3. Builds wakeup-based causal graphs for those windows (NetworkX)
4. Retrieves runbook knowledge via TF-IDF RAG and emits a structured RCA report
5. Optionally runs continuously as a background daemon

The stressor test suite is wired into `parentProcess.py` and doubles as labeled ground truth for accuracy benchmarks.

---

## Goal

Automatically answer: **what process / contention pattern caused a performance or reliability anomaly**, using low-level Linux signals (eBPF, PSI, vmstat) rather than only application logs.

---

## Technologies Used

| Layer | Technology | Role |
|---|---|---|
| Kernel tracing | **bpftrace** / eBPF | Probe latency, lock, IO, OOM, and wakeup edges |
| System metrics | **/proc** (PSI, vmstat) | CPU / memory / IO pressure and VM counters |
| Stress testing | **stress-ng**, **fio**, **systemd-run** | Reproducible fault injection |
| Feature join | **pandas** | Align probe windows with procfs samples |
| Anomaly detection | **scikit-learn OneClassSVM** (+ StandardScaler) | Flag anomalous feature windows (production) |
| Detector comparison | IsolationForest, LocalOutlierFactor | Benchmarked alternatives in `detectorBenchmark.py` |
| Causal graphs | **NetworkX** | Wakee → Waker dependency graphs + JSON export |
| Explanation | **TF-IDF RAG** (scikit-learn + `knowledge/`) | Natural-language root-cause report with remediation |
| Orchestration | **Python 3** subprocesses | Run extractors, stressors, and trackers together |

---

## Feature Matrix & Demonstration Cheat Sheet

Use this cheat sheet to quickly demonstrate any specific subsystem of RCA:

| Subsystem / Feature | Underlying Tech | Key Files | Quick Demo Command |
|---|---|---|---|
| **Turnkey Offline Replay** | Full 4-stage pipeline | `testbed/demo_replay.py` | `python3 testbed/demo_replay.py` |
| **Ground-Truth Evaluation** | Quantitative accuracy & MTTR | `testbed/evaluate.py` | `python3 testbed/evaluate.py` |
| **Live Daemon & Alerting** | Continuous background monitor | `testbed/demo_live.sh`, `rcaDaemon.py` | `sudo bash testbed/demo_live.sh` |
| **Detector Benchmark** | OCSVM vs IsolationForest vs LOF | `detectorBenchmark.py` | `python3 detectorBenchmark.py` |
| **Held-Out Stratified Test** | Stratified split & per-class recall | `overallBenchmark.py` | `python3 overallBenchmark.py` |
| **Kernel Latency / Lock Probes** | eBPF maps (futex, vfs, oom, rq, io) | `probes.bt`, `probeExtractor.py` | `sudo python3 parentProcess.py lockStressor1` |
| **Procfs PSI & Vmstat Deltas** | `/proc/pressure/*`, `/proc/vmstat` | `procExtractor.py` | `sudo python3 parentProcess.py memStressor1` |
| **Wakeup Edge Tracing** | In-kernel sampled sched_wakeup | `wakeupTracker.bt` | `sudo python3 parentProcess.py default` |
| **Persisted Model (Train/Score)** | StandardScaler + OneClassSVM | `anomalyDetection.py` | `python3 anomalyDetection.py --score` |
| **Causal Graph & Root Analysis** | NetworkX Digraph (Wakee→Waker) | `analyzeCausalGraphs.py` | `python3 analyzeCausalGraphs.py` |
| **Label-Free Classification** | Multi-tier evidence engine | `diagnosis.py` | `python3 diagnosis.py data/combined_features.csv` |
| **Evidence-Grounded RAG** | TF-IDF cosine sim + runbooks | `ragExplainer.py`, `knowledge/` | `python3 ragExplainer.py` |
| **LLM Review (Cloud API)** | OpenAI-compatible completions | `ragExplainer.py` | `RCA_LLM_API_KEY=sk-... python3 ragExplainer.py` |
| **LLM Review (Local Ollama)** | Ollama /v1 & /api endpoints | `ragExplainer.py` | `RCA_LLM_BASE_URL=http://localhost:11434/v1 python3 ragExplainer.py` |
| **LLM Mock Unit Test Suite** | Ephemeral HTTP test server | `testbed/test_llm.py` | `python3 testbed/test_llm.py` |
| **Safe Cgroup Stressors** | `systemd-run --scope` cgroups | `testbed/run_fault.sh`, `run_all.sh` | `sudo bash testbed/run_fault.sh memStressor2` |

---

## Dependencies & System Requirements

### Host Requirements
- **OS:** Linux (Ubuntu 22.04/24.04 LTS or Debian 11/12 recommended)
- **Kernel:** Linux ≥ 5.4 with eBPF tracepoints and PSI (`CONFIG_PSI=y`, check `/proc/pressure/cpu`)
- **Hardware:** ≥ 2 CPU cores, ≥ 4 GiB RAM (≥ 2 GiB available), ≥ 5 GiB free disk space
- **Privileges:** Root (`sudo`) is required for live eBPF collection and stressor cgroup scoping; regular user permissions suffice for offline replay, analysis, and evaluation

### System packages

```bash
sudo apt update
sudo apt install -y bpftrace python3 python3-venv python3-pip \
  stress-ng fio coreutils systemd
```

Enable kernel scheduler statistics (required for runqueue and I/O wait probe tracking):
```bash
sudo sysctl -w kernel.sched_schedstats=1
```

### Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

`requirements.txt` covers the offline pipeline (`scikit-learn`, `networkx`, `pandas`, `numpy`). The optional LLM review layer needs **no extra dependencies** — it communicates via Python standard library HTTP requests to any OpenAI-compatible API or local Ollama endpoint.

---

## Architecture

```
+------------------------------------------------------------------+
|                        parentProcess.py                          |
|              (orchestrator + built-in stressor suite)            |
|                                                                  |
|  +---------------+  +----------------+  +---------------------+  |
|  | procExtractor |  | probeExtractor |  |  wakeupTracker.bt   |  |
|  | (/proc PSI +  |  |  (probes.bt)   |  | (sched_wakeup JSON) |  |
|  |    vmstat)    |  |                |  |                     |  |
|  +-------+-------+  +--------+-------+  +----------+----------+  |
|          |                   |                     |             |
|          v                   v                     v             |
|  procfsData_*.jsonl   probeData_*.jsonl   edges_<run_id>.jsonl  |
|          |                   |                     |             |
|          +---------+---------+                     |             |
|                    v                               |             |
|            featureExtractor.py                     |             |
|          -> combined_features.csv                  |             |
|                    |                               |             |
|                    v                               |             |
|            anomalyDetection.py                     |             |
|         (StandardScaler + OneClassSVM)             |             |
|          -> scored_features.csv                    |             |
|                    |                               |             |
|                    +---------------+---------------+             |
|                                    v                             |
|                         analyzeCausalGraphs.py                   |
|                         (NetworkX causal graphs)                 |
|                       -> causal_summaries.json                   |
|                       -> causal_graphs.json                      |
|                                    |                             |
|                                    v                             |
|                          ragExplainer.py                         |
|                    (TF-IDF corpus + metric heuristics)           |
|                       -> data/rca_report.txt                     |
|                       -> data/rca_report.json                    |
+------------------------------------------------------------------+
```

### Data flow (short)

1. **Collect** — While a stressor (or idle baseline) runs, three collectors write telemetry.
2. **Featurise** — Merge bpftrace windows with nearest PSI/vmstat samples; label by stressor.
3. **Detect** — Scale features; train OneClassSVM on `root_cause == Normal`; score all windows.
4. **Graph** — For anomalous windows, pull matching wakeup edges and build directed graphs.
5. **Explain** — Retrieve similar knowledge docs via TF-IDF and apply metric heuristics.

Full preprocessing detail: see [`preprocessing.md`](preprocessing.md).

---

## Project Layout (file-by-file)

```
RCA/
├── parentProcess.py          # Orchestrator + stressor suite
├── procExtractor.py          # /proc PSI + vmstat sampler
├── probeExtractor.py         # Parses probes.bt JSON output
├── probes.bt                 # Latency / lock / IO / OOM bpftrace maps
├── wakeupTracker.bt          # sched_wakeup → edges_<run_id>.jsonl (1-in-100 sampled)
├── wakeupTrackerMonitor.bt   # Daemon variant: same schema, 1-in-1000 (bounded disk)
├── featureExtractor.py       # Merge + label features (auto-discovers runs)
├── diagnosis.py              # Shared label-free cause classification (explainer + benchmarks)
├── anomalyDetection.py       # Production OneClassSVM scorer
├── detectorBenchmark.py      # OCSVM vs IsolationForest vs LOF
├── overallBenchmark.py       # Hold-out accuracy (TP/TN/FP/FN) + cause-family agreement
├── analyzeCausalGraphs.py    # NetworkX causal analysis + JSON export
├── ragExplainer.py           # TF-IDF RAG explainer + structured reports
├── run_pipeline.py           # One-command offline pipeline runner
├── rcaDaemon.py              # Two-phase background monitor (--learn / --monitor)
├── deploy/rca-daemon.service # systemd unit for the monitor
├── labels.py                 # Single source of truth: stressor → label/family mapping
├── testbed/                  # Evaluator testbed (preflight, fault runs, demo)
│   ├── preflight.sh          # Environment & safety checks
│   ├── run_fault.sh          # One guarded fault run + post-run verification
│   ├── run_all.sh            # Full 10-fault matrix (resumable)
│   ├── demo_replay.py        # Offline replay demo (no root needed)
│   ├── demo_live.sh          # Live daemon + fault injection end-to-end demo
│   ├── evaluate.py           # Ground truth vs pipeline → results CSV + metrics JSON
│   └── README.md             # Evaluator runbook
├── requirements.txt          # Python dependencies
├── preprocessing.md          # Preprocessing steps documentation
├── IMPLEMENTATION.md         # Step-by-step build guide
├── changes.md                # Known follow-ups
├── knowledge/                # Runbook knowledge corpus (Markdown)
│   ├── cpu_competition.md
│   ├── cgroup_throttling.md
│   ├── memory_pressure.md
│   ├── oom.md
│   ├── lock_contention.md
│   ├── io_saturation.md
│   └── heap_expansion.md
└── data/                     # Runtime artefacts (generated)
    ├── procfsData_*.jsonl
    ├── probeData_*.jsonl
    ├── edges_<run_id>.jsonl
    ├── meta_*.json
    ├── combined_features.csv
    ├── scored_features.csv
    ├── model/ocsvm.joblib     # Persisted scaler + model + feature list
    ├── causal_summaries.json
    ├── causal_graphs.json
    ├── rca_report.txt / .json
    ├── detector_benchmark.json
    ├── overall_benchmark.json
    ├── ground_truth.jsonl     # Per-run labels + stressor start/end (evaluation only)
    ├── daemon_state.json      # Monitor: last-scored offsets (graceful restart)
    ├── daemon_status.json     # Monitor: heartbeat (pid, cycle, counters, disk)
    ├── alerts.jsonl           # Monitor: one alert per anomalous window
    ├── daemon_baseline.csv    # Monitor --learn: host-normal training rows
    └── daemon.log             # Monitor: rotating log
```

---

## File-by-File Reference

### Collection layer

#### `parentProcess.py`

Orchestrator. Usage:

```bash
sudo python3 parentProcess.py <stressor_name>
sudo python3 parentProcess.py <run_id> '<custom_command>'
sudo python3 parentProcess.py default   # idle baseline
```

Behaviour:

- Writes `data/meta_<run_id>.json` and `data/meta_latest.json` (`boot_time_epoch`, `start_time`, stressor command) for later edge↔window alignment.
- Starts in parallel:
  - `procExtractor.py <run_id>`
  - `probeExtractor.py <run_id>`
  - stressor command from `STRESSORS` (or CLI override)
  - `bpftrace -q wakeupTracker.bt` → `data/edges_<run_id>.jsonl` (1-in-100 in-kernel sampling)
- When the stressor exits, collectors continue ~10s (cooldown), then receive `SIGINT`.
- On `KeyboardInterrupt`, all children are signalled and waited on (with kill fallback).

#### `autoExtractor.sh`

Loops all built-in stressor IDs (including `default`) through `parentProcess.py`. Uses `sudo` automatically when not already root.

#### `procExtractor.py`

Polls every **1 second**:

| Metric | Source |
|---|---|
| `cpu.some` / `mem.some|full` / `io.some|full` | `/proc/pressure/*` |
| `allocstall_count` | delta of summed `allocstall_*` zones |
| `major_fault_count` | delta `pgmajfault` |
| `swap_in_count` / `swap_out_count` | delta `pswpin` / `pswpout` |
| `oom_kill_count` | delta `oom_kill` |

Writes `data/procfsData_<run_id>.jsonl`. Primer vmstat read ensures the first delta is meaningful.

#### `probeExtractor.py`

Runs `bpftrace -f json probes.bt`, buffers map dumps until `"END"`, flattens per-`comm` metrics into:

```json
{"window_start": ..., "window_end": ..., "processes": {"comm": {"runtime": ...}}, ...}
```

First-window start is backdated to `fin_timestamp - 3.0` to correct bpftrace attach delay. Writes `data/probeData_<run_id>.jsonl`.

#### `probes.bt`

Accumulates maps every 3s then clears. Metrics:

| Map | Description |
|---|---|
| `@vfsreadtime` | VFS read latency sum (ns) |
| `@rq_time` | Runqueue wait time (ns) |
| `@runtime` | Scheduled runtime (ns) |
| `@futex_wait_time` / `@futex_wait_count` | Futex wait duration / count |
| `@oom_time` | OOM path duration (ns) |
| `@iowait_time` | I/O wait scheduler delay (ns) |
| `@sector_count` | Block I/O sectors completed |
| `@locktime` | Lock contention duration (ns) |

#### `wakeupTracker.bt`

On `sched_wakeup` / `sched_wakeup_new`, emits JSONL:

`timestamp` (nsecs since boot), `waker_tid`, `waker_pid`, `waker_comm`, `wakee_tid`, `wakee_comm`.

---

### Feature & detection layer

#### `featureExtractor.py`

- Discovers `data/probeData_*.jsonl` automatically (or takes one `run_id` arg).
- Skips missing/empty pairs.
- Explodes probe `processes` into one row per `(window, comm)`.
- `json_normalize`s nested PSI fields (`cpu_some_avg10`, …).
- `merge_asof(..., direction='nearest', tolerance=2.0)` joins probe windows to procfs.
- Labels via `ROOT_CAUSES` map (`default` → `Normal`).
- Output: `data/combined_features.csv`.

#### `anomalyDetection.py` (production detector)

**Model:** `OneClassSVM(kernel='rbf', gamma='scale', nu=0.05)`  
**Preprocessing:** numeric column select → `fillna(0)` → train on Normal (≤ 3000 rows) → `StandardScaler` → fit/score.

Outputs columns `anomaly_score` (decision function; negative ≈ anomaly) and `predicted_anomaly` (True when `predict == -1`). Writes `data/scored_features.csv` and prints per-class flagged rates / Normal FPR.

**Why OneClassSVM?** `detectorBenchmark.py` on the labeled stressor set showed (current corpus, 71 777 rows):

| Model | F1 | FPR (Normal) | Notes |
|---|---|---|---|
| **OneClassSVM** | **0.982** | **5.4 %** | Selected for production |
| LocalOutlierFactor | 0.919 | 26.1 % | Near-perfect recall, too many false positives |
| IsolationForest | 0.867 | 8.9 % | Previous baseline |

#### `detectorBenchmark.py`

Side-by-side comparison of OneClassSVM, IsolationForest, and LocalOutlierFactor: accuracy, precision, recall, F1, FPR, FNR, TP/TN/FP/FN, fit time, score time, per-class recall. Writes `data/detector_benchmark.json`.

```bash
python3 detectorBenchmark.py
```

#### `overallBenchmark.py`

Hold-out (default 30%, stratified) accuracy for the **production OneClassSVM** path:

- TP / TN / FP / FN, accuracy, precision, recall, F1, FPR, FNR
- Per-class flag rates on the test split
- Label-free cause-family agreement at window level (shared with the explainer via
  `diagnosis.py`), split into windows overlapping the fault interval vs transition windows
- Full-data production-style score for reference

Writes `data/overall_benchmark.json`.

```bash
python3 overallBenchmark.py
python3 overallBenchmark.py --test-size 0.3 --nu 0.05
```

---

### Causal graph & explanation layer

#### `analyzeCausalGraphs.py`

1. Loads `predicted_anomaly == True` windows; pads ±1s; merges overlaps.
2. Resolves `boot_time_epoch` from `meta_latest.json` / reboot-aware inference.
3. Converts edge `nsecs` → epoch; filters noise (`swapper/*`, `bpftrace`, `ksoftirqd/*`, `migration/*`, `rcu_*`, `cpuhp/*`).
4. Builds Digraph edges **Wakee → Waker** (blocked task points at who unblocked it).
5. Candidate roots = `in_degree > 0` and `out_degree == 0` (terminal blockers).
6. Sample paths: symptom (`in_degree == 0`) → … → root via shortest path.
7. Exports `data/causal_summaries.json` and `data/causal_graphs.json` (NetworkX node-link).

#### `ragExplainer.py`

- Indexes `knowledge/*.md` with `TfidfVectorizer(stop_words='english')`.
- Builds a retrieval query from candidate roots, causal paths, and diagnosis-family keywords.
- Classifies cause via `diagnosis.py` (shared with the benchmarks; observed evidence only):
  culprit-process identity → kernel OOM-kill event → metric signatures → `unknown`.
  Ground-truth labels are never used.
- Optional LLM review via any OpenAI-compatible cloud API (OpenAI, Groq, OpenRouter) or local Ollama instance: set `RCA_LLM_API_KEY` or `RCA_LLM_BASE_URL=http://localhost:11434/v1` (with optional `RCA_LLM_MODEL`); no local install required for cloud use, template report is the fallback. Unit testable via `python3 testbed/test_llm.py`.
- Writes `data/rca_report.txt` and `data/rca_report.json`.

#### Knowledge corpus (`knowledge/`)

| File | Topic |
|---|---|
| `cpu_competition.md` | CPU fight / runqueue starvation |
| `cgroup_throttling.md` | CPUQuota / cgroup throttle |
| `memory_pressure.md` | PSI mem, swap, allocstall |
| `oom.md` | OOM killer path |
| `heap_expansion.md` | Continuous `brk` growth |
| `lock_contention.md` | Futex / VFS lock thrash |
| `io_saturation.md` | Random / sequential write saturation |

---

### Orchestration & ops

#### `run_pipeline.py`

Runs offline stages in order (no sudo required if data already collected):

1. `featureExtractor.py`
2. `anomalyDetection.py`
3. `analyzeCausalGraphs.py`
4. `ragExplainer.py`

Continues past stage failures so later stages can still use prior outputs.

#### `rcaDaemon.py`

Two-phase background monitor (see `deploy/rca-daemon.service` for the systemd unit):

```bash
# Phase A — learn this host's normal behavior, then train the daemon model:
sudo .venv/bin/python rcaDaemon.py --learn 300

# Phase B — continuous monitoring + alerts:
sudo .venv/bin/python rcaDaemon.py --monitor --interval 30 --rotate 900 --retain-hours 24

# Offline smoke test of every stage on existing runs (no root):
.venv/bin/python rcaDaemon.py --no-collect --runs memStressor2,default1 --reprocess
```

Monitor behavior:

- collectors run continuously; run files rotate every `--rotate` seconds (default 900)
- each cycle only the **new** windows are featurized and scored with the persisted model
  (`data/model/ocsvm_daemon.joblib`, falling back to the batch model); stages run as
  in-process calls — `run_id` is never passed where a stage expects a path
- a window counts as anomalous when ≥ `--flag-fraction` (default 50 %) of its rows are
  flagged; consecutive anomalous windows merge into episodes, and per episode the causal
  graph + grounded report are built
- alerts: one JSON line per anomalous window in `data/alerts.jsonl`
  (`timestamp, window, cause, culprit, root, remediation`), readable report appended to
  `data/rca_report.txt`, optional desktop notification (`--notify`)
- housekeeping: dead collectors restart (3 fast failures abort the daemon), run files older
  than `--retain-hours` are pruned (daemon-created runs only — never the batch corpus), and
  `data/daemon_status.json` is the heartbeat
- graceful shutdown finalizes the active run and persists last-scored offsets in
  `data/daemon_state.json`; a restart continues where it stopped (no reprocessing)

#### `preprocessing.md`

Canonical list of every preprocessing step (deltas, window alignment, merge, scaling, graph hygiene). Keep in sync when changing feature or detector code.

#### `IMPLEMENTATION.md`

Phase-by-phase build / validation guide (environment → collection → features → detection → graphs → RAG → daemon).

#### `changes.md`

Short backlog (e.g. more Normal baselines for training quality).

---

## Built-in Stressor Suite (test data)

Defined in `parentProcess.py` → `STRESSORS`:

| ID | Intent | Ground-truth label |
|---|---|---|
| `cpuStressor1` | Pure CPU competition | CPU Competition |
| `cpuStressor2` | cgroup `CPUQuota=20%` | Process Throttled |
| `cpuStressor3` | Mild CPU load | Baseline CPU Usage |
| `memStressor1` | Memory pressure / swapping | Memory Swapping |
| `memStressor2` | Heavy VM alloc → OOM | OOM Kill |
| `memStressor3` | Heap / `brk` expansion | Continuous Heap Expansion |
| `lockStressor1` | Userspace futex contention | Userspace Contention |
| `lockStressor2` | Kernel dentry / VFS lock thrash | Kernel-Internal Contention |
| `ioStressor1` | Small random writes (fio) | Small Random Writes |
| `ioStressor2` | Large sequential writes (fio) | Large Sequential Writes |
| `default` | Idle baseline (`sleep 60`) | Normal |

These labels train evaluation only; the detector itself is unsupervised (fits on Normal).

---

## Timestamp Alignment

`wakeupTracker.bt` emits nanoseconds since boot (`nsecs`); probe/procfs use UNIX epoch (`time.time()`).

`analyzeCausalGraphs.py` resolves this by:

1. Reading `data/meta_latest.json` for `boot_time_epoch` recorded at collection
2. If a reboot occurred between capture and analysis, inferring boot offset from window vs edge ranges
3. Applying: `edge_epoch = boot_time_epoch + (raw_nsecs / 1e9)`

---

## Testing Environment Instantiation & Demonstration

This section provides the exact steps to instantiate a testing environment and demonstrate how RCA detects, causally analyzes, and explains performance faults.

### Step 1: Instantiate the Environment

RCA can run directly on a Linux host or inside an isolated virtual machine.

#### Option A: Isolated Virtual Machine via Multipass (Recommended for stress testing)
If you want an isolated sandbox that will not disrupt your workstation during memory or CPU stress testing:
```bash
# Launch an Ubuntu 24.04 instance with required resources
multipass launch 24.04 --name rca-test --cpus 2 --memory 4G --disk 20G
multipass shell rca-test

# Inside the instance, clone the repository
git clone https://github.com/sur-ve/RCA.git RCA
cd RCA
```

#### Option B: Existing Linux Host
Ensure your system meets the [Dependencies & System Requirements](#dependencies--system-requirements) (Linux kernel ≥ 5.4, eBPF enabled, `/proc/pressure/cpu` readable).

---

### Step 2: System Provisioning & Setup

Install tracing and fault injection tools, enable scheduler statistics, and set up the Python virtual environment:

```bash
cd /path/to/RCA

# 1. Install OS packages and tracing utilities
sudo apt update
sudo apt install -y bpftrace python3 python3-venv python3-pip \
  stress-ng fio coreutils systemd

# 2. Enable kernel scheduler stats (enables runqueue wait and iowait tracepoints)
sudo sysctl -w kernel.sched_schedstats=1

# 3. Create virtual environment and install Python libraries
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

### Step 3: Run the Preflight Safety Check

Verify kernel tracepoints, eBPF attach permissions, resource limits, and probe compilation:

```bash
sudo bash testbed/preflight.sh --clean
```

**What this verifies:**
- Kernel tracepoints and eBPF attach permissions (`bpftrace`)
- `/proc/pressure` (PSI) readability
- Kernel `sched_schedstats=1` enabled
- Sufficient disk space (≥ 4 GiB free) and available memory (≥ 2 GiB)
- Smoke tests all eBPF probe scripts (`probes.bt`, `wakeupTracker.bt`, `wakeupTrackerMonitor.bt`)
- Automatically kills any stale collection processes or dangling cgroup scopes

---

### Step 4: Demonstrating How RCA Works (Turnkey Workflows)

Three turnkey demonstration modes demonstrate the system end-to-end:

#### Mode 1: Deterministic Offline Replay (No root needed, ~10 seconds)
Replays the pre-collected 16-run corpus through the entire pipeline:
```bash
python3 testbed/demo_replay.py
```
**What this demonstrates:**
1. **Featurization:** Merges bpftrace latency windows with procfs PSI/vmstat samples.
2. **Detection:** Scores all windows with the persisted OneClassSVM model (`data/model/ocsvm.joblib`).
3. **Causal Graph Analysis:** Builds directed `Wakee → Waker` dependency graphs and extracts terminal blockers (candidate roots).
4. **RAG Explanation:** Matches anomaly symptoms against runbooks in `knowledge/*.md` and generates a structured RCA report with remediation recommendations.
5. **Results Table:** Prints a per-fault verification table showing **10/10 fault classes detected** with **1.000 Top-1 family accuracy**.

#### Mode 2: Live Background Monitoring & Fault Injection (Root required, ~2 minutes)
Demonstrates real-time anomaly detection, causal analysis, and alert generation in continuous monitor mode:
```bash
sudo bash testbed/demo_live.sh
```
**What this demonstrates:**
1. Starts the background monitoring daemon (`rcaDaemon.py`) with continuous low-overhead tracing (1-in-1000 sampled edges).
2. Automatically injects a safe CPU competition fault (`cpuStressor1`, 45 s).
3. The daemon detects the fault in real time, bounds the anomaly episode, traces causal blocker graphs, and emits an alert to `data/alerts.jsonl`.
4. The testbed script captures the alert, measures MTTR (typically within 1 cycle, ≤ 60 s), prints the RCA report excerpt, and terminates the daemon cleanly.

#### Mode 3: Ground-Truth Quantitative Accuracy Evaluation (No root needed, ~3 seconds)
Evaluates pipeline predictions against labeled ground truth in `data/ground_truth.jsonl`:
```bash
python3 testbed/evaluate.py
```
**Expected metrics:**
- **Fault Detection Recall:** 1.000 (10/10 fault runs detected)
- **Normal False Positive Rate:** 0.000 (0 of 6 baseline runs flagged)
- **Top-1 Family Accuracy:** 1.000 (label-free diagnosis path)
- **Mean Time to Resolution (MTTR):** ~2.5 s
- Generates `data/evaluation_results.csv` and `data/demo_metrics.json`.

---

### Step 5: Testing with the Evidence-Grounded LLM Review Layer (Optional)

The RAG explainer includes an optional LLM layer that reviews rule-based diagnosis drafts against cited kernel metrics:

#### Cloud API (OpenAI, Groq, OpenRouter)
```bash
export RCA_LLM_API_KEY=sk-...
export RCA_LLM_BASE_URL=https://api.groq.com/openai/v1   # optional
export RCA_LLM_MODEL=llama-3.3-70b-versatile             # optional
python3 ragExplainer.py
```

#### Local Ollama (No API key needed)
```bash
export RCA_LLM_BASE_URL=http://localhost:11434/v1
export RCA_LLM_MODEL=llama3.2
python3 ragExplainer.py
```

#### Unit Test Mock Suite
Verify LLM client behavior, Ollama schema handling, and 404 fallbacks without external network calls:
```bash
python3 testbed/test_llm.py
```

---

### Step 6: Collecting a Fresh Fault Matrix (Optional, ~30–40 minutes)

To collect an entirely new corpus from scratch on your host:
```bash
# 1. Run all 10 fault classes with cgroup isolation guards (resumable)
sudo bash testbed/run_all.sh

# 2. Re-extract features, train the model, and evaluate
python3 featureExtractor.py
python3 anomalyDetection.py --train
python3 anomalyDetection.py --score
python3 analyzeCausalGraphs.py
python3 ragExplainer.py
python3 testbed/evaluate.py
```

---

### Step 7: Running Individual Pipeline Stages

You can also run stages individually or via the one-command batch runner:
```bash
python3 run_pipeline.py
```
Or stage-by-stage:
```bash
python3 featureExtractor.py       # merges probe + procfs data -> combined_features.csv
python3 anomalyDetection.py --score # scores features -> scored_features.csv
python3 analyzeCausalGraphs.py    # builds causal graphs -> causal_summaries.json
python3 ragExplainer.py           # synthesizes reports -> data/rca_report.txt
```

## Implementation Walkthrough

### Phase A — Collect

`parentProcess.py` launches extractors + bpftrace + stressor. Artefacts land under `data/`. Always collect at least one `default` run so OneClassSVM has a Normal manifold to fit.

### Phase B — Featurise

`featureExtractor.py` produces a wide table: probe process metrics + PSI/vmstat columns + `root_cause`. Sparse probe cells become 0 at detection time.

### Phase C — Detect

`anomalyDetection.py`:

1. Select numeric features
2. Fit `StandardScaler` + `OneClassSVM` on Normal
3. Score every row → `predicted_anomaly`
4. Persist `scored_features.csv`

### Phase D — Causal graphs

Only flagged windows are expanded. Noise wakeups dropped; terminal blockers become candidate roots; paths are shortest Wakee→…→Waker chains.

### Phase E — Explain

`ragExplainer.py` grounds the report in metrics + graph + `knowledge/` runbooks. LLM is optional; default path is fully offline.

### Phase F — Validate

Use stressors as ground truth:

| Check | Expectation |
|---|---|
| Normal FPR | Low (OneClassSVM 5.4 % on the current corpus) |
| Stressor recall | 10/10 fault classes ≈ 1.0 (held-out test) |
| Graph candidates | Often include stressor-related `comm` (e.g. `stress-ng-*`, `fio`) |
| Cause family (reports) | 99.4 % over fault-active windows; 88.7 % incl. transition windows |

Re-run `detectorBenchmark.py` / `overallBenchmark.py` after changing features or detector hyperparameters.

---

## Benchmark Suite & Quantitative Evaluation

The system includes multiple reproducible benchmarks to validate detector quality, held-out generalization, and end-to-end diagnostic accuracy against labeled ground truth.

### 1. Detector Comparison Benchmark (`detectorBenchmark.py`)

Compares **OneClassSVM**, **IsolationForest**, and **LocalOutlierFactor** on the full 71,777-row corpus (35 numeric features, trained on 3,000 Normal samples):

```bash
python3 detectorBenchmark.py
```

**Measured results (71,777 rows, 35 features):**

| Detector | Accuracy | Precision | Recall | F1 Score | Normal FPR | Fit Time | Score Time | Role |
|---|---|---|---|---|---|---|---|---|
| **OneClassSVM** | **0.9780** | **0.9649** | **0.9996** | **0.9820** | **5.43 %** | **0.036 s** | **0.700 s** | **Production** |
| LocalOutlierFactor | 0.8950 | 0.8510 | 0.9996 | 0.9193 | 26.13 % | 0.110 s | 0.941 s | Comparison |
| IsolationForest | 0.8513 | 0.9317 | 0.8112 | 0.8673 | 8.88 % | 0.295 s | 0.227 s | Prior baseline |

**Confusion Matrix breakdown:**
- **OneClassSVM:** TP: 42,971 | FP: 1,562 | FN: 17 | TN: 27,227
- **IsolationForest:** TP: 34,870 | FP: 2,557 | FN: 8,118 | TN: 26,232
- **LocalOutlierFactor:** TP: 42,972 | FP: 7,524 | FN: 16 | TN: 21,265

*Result artifact written to:* `data/detector_benchmark.json`

---

### 2. Held-Out Stratified Benchmark (`overallBenchmark.py`)

Performs a **30% stratified train/test split** (50,243 train rows / 21,534 test rows) on the production OneClassSVM pipeline and evaluates both row-level detection and window-level cause-family classification:

```bash
python3 overallBenchmark.py
# Or customize split and nu:
python3 overallBenchmark.py --test-size 0.3 --nu 0.05
```

**Held-out TEST performance (21,534 unseen rows):**
- **Accuracy:** 0.9773
- **Precision:** 0.9635
- **Recall:** 0.9999 (only 1 false negative out of 12,897 fault rows)
- **F1 Score:** 0.9814
- **Normal FPR:** 0.0565 (488 false positives out of 8,637 Normal rows)
- **Fit + Score Time:** 0.755 s

**Per-class detection recall on the held-out test split:**

| Class Name | Test Samples ($n$) | Detection Recall |
|---|---|---|
| `Baseline CPU Usage` | 1,440 | 1.000 |
| `CPU Competition` | 1,294 | 1.000 |
| `Process Throttled` | 1,407 | 1.000 |
| `Continuous Heap Expansion` | 638 | 1.000 |
| `Memory Swapping` | 1,412 | 1.000 |
| `OOM Kill` | 818 | 1.000 |
| `Userspace Contention` | 1,491 | 0.999 |
| `Kernel-Internal Contention` | 1,567 | 1.000 |
| `Small Random Writes` | 1,411 | 1.000 |
| `Large Sequential Writes` | 1,419 | 1.000 |
| `Normal` (baseline) | 8,637 | FPR = 0.057 |

**Window-level cause-family classification agreement (label-free via `diagnosis.py`):**
- **Fault-active windows:** **99.42 %** agreement (171 evaluated windows overlapping fault activity)
  - `CPU / Scheduling` ($n=57$): recall = 1.000
  - `I/O Saturation` ($n=38$): recall = 1.000
  - `Lock Contention` ($n=38$): recall = 1.000
  - `Memory Pressure` ($n=19$): recall = 1.000
  - `OOM` ($n=12$): recall = 1.000
  - `Heap Expansion` ($n=7$): recall = 0.857
- **All fault windows (including transition edges):** **88.67 %** agreement (203 total windows)

*Result artifact written to:* `data/overall_benchmark.json`

---

### 3. Ground-Truth Accuracy & Latency Evaluation (`testbed/evaluate.py`)

Evaluates the end-to-end pipeline across all 16 recorded runs (6 baselines + 10 faults) against `data/ground_truth.jsonl`:

```bash
python3 testbed/evaluate.py
```

**Run-level evaluation table:**

| Run ID | Ground-Truth Fault Label | Detected? | Family Match | Alert Latency (MTTR) |
|---|---|---|---|---|
| `cpuStressor1` | CPU Competition | **YES** | ok (cpu) | 2.4 s |
| `cpuStressor2` | Process Throttled | **YES** | ok (cpu) | 2.5 s |
| `cpuStressor3` | Baseline CPU Usage | **YES** | ok (cpu) | 2.5 s |
| `memStressor1` | Memory Swapping | **YES** | ok (memory) | 2.6 s |
| `memStressor2` | OOM Kill | **YES** | ok (oom) | 2.4 s |
| `memStressor3` | Continuous Heap Expansion | **YES** | ok (heap) | 2.6 s |
| `lockStressor1` | Userspace Contention | **YES** | ok (lock) | 3.2 s |
| `lockStressor2` | Kernel-Internal Contention | **YES** | ok (lock) | 2.7 s |
| `ioStressor1` | Small Random Writes | **YES** | ok (io) | 2.3 s |
| `ioStressor2` | Large Sequential Writes | **YES** | ok (io) | 2.3 s |
| `default` / `default1`–`default5` | Normal (6 baseline runs) | **no** (0/6 flagged) | N/A | — |

**Summary Metrics:**
- **Fault Detection Recall:** **1.000** (10/10 fault runs detected)
- **Normal False Positive Rate:** **0.000** (0 of 6 baseline runs flagged)
- **Top-1 Family Accuracy:** **1.000** (all 10 fault classes correctly classified on the label-free path)
- **Mean Time to Resolution (MTTR):** **2.5 s**

*Artifacts written to:* `data/evaluation_results.csv` and `data/demo_metrics.json`

---

### 4. Standalone Cause-Family Classifier Test (`diagnosis.py`)

Test the deterministic label-free diagnosis engine directly on any feature CSV:

```bash
python3 diagnosis.py data/combined_features.csv
```

Outputs window counts and recall percentages across CPU, Memory, OOM, Heap, Lock, and I/O families.

---

### 5. LLM Layer Mock Unit Test (`testbed/test_llm.py`)

Test the OpenAI-compatible completions, local Ollama integration, schema fallback, and network error handling without needing an active internet connection or paid API key:

```bash
python3 testbed/test_llm.py
```
*Result:* 6/6 tests pass in 0.5 s using a local mock HTTP server.

---

## Safety Notes

- Stressors can make the machine slow, swap heavily, or trigger OOM. Prefer a VM or spare host.
- The testbed runs memory stressors inside `systemd-run --scope` cgroups
  (`MemoryMax`/`MemorySwapMax`); `memStressor2` gets OOM-killed *inside its cgroup*
  (repeatedly, by design) and cannot take the host down.
- bpftrace and `/proc` collectors need elevated privileges.
- Do not point `fio` stressors at production disks without understanding write paths.
- `rcaDaemon.py` needs root (or CAP_BPF / CAP_SYS_ADMIN) for live collection; run in an isolated environment.

---

## Known Follow-ups

See `changes.md` and `PLAN.md`:

- Stronger filtering of noisy kernel wakeups (IRQ threads, kworkers)
- Optional upgrade from TF-IDF to dense embeddings (would need an embedding model or API)
- Tune OneClassSVM `nu` if Normal FPR drifts after new data collection on heavily loaded machines
