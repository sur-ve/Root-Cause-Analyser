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

## Dependencies

### System packages

```bash
sudo apt update
sudo apt install -y bpftrace python3 python3-venv python3-pip \
  stress-ng fio coreutils
```

Requirements:

- Linux kernel with eBPF / tracepoints enabled
- Root (or `CAP_BPF` / `CAP_SYS_ADMIN`) to run bpftrace
- PSI available under `/proc/pressure/` (kernels ≥ 4.20)

### Python packages

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` covers the offline pipeline (`scikit-learn`, `networkx`, `pandas`, `numpy`). Optional lines exist for vector-store RAG (`langchain`, `chromadb`, `sentence-transformers`) or a local Ollama LLM backend.

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
|  procfsData_*.jsonl   probeData_*.jsonl      edges.jsonl         |
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
├── wakeupTracker.bt          # sched_wakeup → edges.jsonl
├── featureExtractor.py       # Merge + label features (auto-discovers runs)
├── anomalyDetection.py       # Production OneClassSVM scorer
├── detectorBenchmark.py      # OCSVM vs IsolationForest vs LOF
├── overallBenchmark.py       # Hold-out accuracy (TP/TN/FP/FN)
├── analyzeCausalGraphs.py    # NetworkX causal analysis + JSON export
├── ragExplainer.py           # TF-IDF RAG explainer + structured reports
├── run_pipeline.py           # One-command offline pipeline runner
├── rcaDaemon.py              # Continuous background monitoring daemon
├── autoExtractor.sh          # Shell helper to collect all stressor classes
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
    ├── edges.jsonl
    ├── meta_*.json
    ├── combined_features.csv
    ├── scored_features.csv
    ├── causal_summaries.json
    ├── causal_graphs.json
    ├── rca_report.txt / .json
    ├── detector_benchmark.json
    └── overall_benchmark.json
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
  - `bpftrace wakeupTracker.bt` → appends `data/edges.jsonl`
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

**Why OneClassSVM?** `detectorBenchmark.py` on the labeled stressor set showed:

| Model | F1 | FPR (Normal) | Notes |
|---|---|---|---|
| **OneClassSVM** | **~0.997** | **~5%** | Selected for production |
| LocalOutlierFactor | ~0.973 | ~53% | Perfect recall, too many FPs |
| IsolationForest | ~0.878 | ~20% | Previous baseline |

#### `detectorBenchmark.py`

Side-by-side comparison of OneClassSVM, IsolationForest, and LocalOutlierFactor: accuracy, precision, recall, F1, FPR, FNR, TP/TN/FP/FN, fit time, score time, per-class recall. Writes `data/detector_benchmark.json`.

```bash
python3 detectorBenchmark.py
```

#### `overallBenchmark.py`

Hold-out (default 30%, stratified) accuracy for the **production OneClassSVM** path:

- TP / TN / FP / FN, accuracy, precision, recall, F1, FPR, FNR
- Per-class flag rates on the test split
- Metric-heuristic cause-family accuracy (explainer path without label peek)
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
- Builds a retrieval query from labeled causes, candidate roots, paths, and (if unlabeled) metric keywords.
- Classifies cause: labeled stressor → metric heuristics → retrieved doc title.
- Collects evidence (OOM, swap, PSI, lock, I/O, graph) and remediation from runbooks.
- Optional Ollama hook (`query_local_ollama`) is present but commented out by default.
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

Continuous monitor:

```bash
python3 rcaDaemon.py --no-collect --interval 300   # analyse existing data
sudo python3 rcaDaemon.py --interval 300           # collect + analyse
```

Starts collectors (unless `--no-collect`), periodically runs the four analysis stages, rotates `edges.jsonl` when > ~50 MB, restarts dead collectors.

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

## Quick Start

```bash
cd /path/to/RCA
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 1) Collect baseline + fault classes (needs sudo)
sudo python3 parentProcess.py default
sudo python3 parentProcess.py cpuStressor1
# …or all classes:
sudo bash autoExtractor.sh

# 2) Full offline pipeline
python3 run_pipeline.py
# Reports → data/rca_report.txt / data/rca_report.json

# 3) Benchmarks (optional)
python3 detectorBenchmark.py
python3 overallBenchmark.py
```

Individual stages:

```bash
python3 featureExtractor.py
python3 anomalyDetection.py
python3 analyzeCausalGraphs.py
python3 ragExplainer.py
```

---

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
| Normal FPR | Low (OneClassSVM ~5% on current corpus) |
| Stressor recall | High across fault classes |
| Graph candidates | Often include stress-ng / fio related `comm` |
| RAG text | Names correct cause family more often than chance |

Re-run `detectorBenchmark.py` / `overallBenchmark.py` after changing features or detector hyperparameters.

---

## Observed Detection Metrics (current corpus)

From `detectorBenchmark.py` / production-style full-data scoring (re-run after data changes):

| Detector | Accuracy | F1 | FPR | Role |
|---|---|---|---|---|
| **OneClassSVM** | ~0.995 | ~0.997 | ~0.05 | **Production** |
| LocalOutlierFactor | ~0.949 | ~0.973 | ~0.53 | Comparison |
| IsolationForest | ~0.799 | ~0.878 | ~0.20 | Prior baseline |

Hold-out numbers (and TP/TN/FP/FN) are written to `data/overall_benchmark.json` when you run `overallBenchmark.py`.

---

## Safety Notes

- Stressors can make the machine slow, swap heavily, or trigger OOM. Prefer a VM or spare host.
- `memStressor2` is aggressive; accept OOM risk before running it.
- bpftrace and `/proc` collectors need elevated privileges.
- Do not point `fio` stressors at production disks without understanding write paths.
- `rcaDaemon.py` needs root (or CAP_BPF / CAP_SYS_ADMIN) for live collection; run in an isolated environment.

---

## Known Follow-ups

See `changes.md`:

- Collect more / cleaner `default` baselines for a tighter Normal boundary
- Stronger filtering of noisy kernel wakeups (IRQ threads, kworkers)
- Optional upgrade from TF-IDF to dense embeddings when Ollama / OpenAI is available
- Tune OneClassSVM `nu` if Normal FPR drifts after new data collection
