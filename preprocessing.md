# Preprocessing Steps

All transforms applied before anomaly detection, causal analysis, and report generation, in pipeline order.

---

## 1. Collection-time transforms

### 1.1 Procfs delta conversion (`procExtractor.py`)

- Primer read of `/proc/vmstat` establishes a baseline delta.
- Each 1 s sample stores **deltas**, not absolute counters:
  - `allocstall_count`, `major_fault_count`, `swap_in_count`, `swap_out_count`, `oom_kill_count`
- Deltas are clamped at 0 to guard against counter resets.
- All available `allocstall_*` zones (`dma`, `dma32`, `normal`, `movable`) are **summed** into a single `allocstall` before differencing (tolerating missing zones on older/newer kernels).
- PSI text from `/proc/pressure/{cpu,memory,io}` is parsed into nested dicts (`some` / `full` × `avg10` / `avg60` / `avg300` / `total`).
- Each record is stamped with UNIX epoch `time.time()` and written as JSONL to `data/procfsData_<run_id>.jsonl`.

### 1.2 Probe window alignment (`probeExtractor.py`)

- `probes.bt` emits nanosecond clock markers (`BEGIN_NS` / `WINDOW_END_NS`) around each ~3 s map accumulation window.
- `probeExtractor.py` pairs window bounds with `boot_time_epoch` from `data/meta_<run_id>.json`:
  - `window_start = boot_time_epoch + (begin_ns / 1e9)`
  - `window_end = boot_time_epoch + (end_ns / 1e9)`
- Consecutive windows abut accurately without magic constants or attach delay skew.
- Per-`comm` map entries are flattened into `processes[comm][metric]`.
- Futex probes are filtered to blocking/waiting ops (0, 9, 11) to eliminate userspace fast-path noise.
- Kernel OOM kill events (`oom_kill_process`, `__oom_kill_process`) are tracked directly via kprobes into `@oom_kills`.
- Non-JSON bpftrace chatter is dropped.

### 1.3 Run metadata & ground truth (`parentProcess.py`)

- Writes `data/meta_<run_id>.json` containing `boot_time_epoch`, `start_time`, `end_time`, stressor command, and label.
- Appends to `data/ground_truth.jsonl` with start/end timestamps and expected fault family (strictly for offline benchmark evaluation).

---

## 2. Feature extraction (`featureExtractor.py`)

### 2.1 Run discovery & label hygiene

- Auto-discovers matching pairs of `data/probeData_<run_id>.jsonl` and `data/procfsData_<run_id>.jsonl`.
- Resolves run labels from `data/meta_<run_id>.json` first, then falls back to `labels.py` (`ROOT_CAUSES`), and finally to `Unlabeled` (no arbitrary `Custom (...)` tags).

### 2.2 Probe flattening

- Explodes probe `processes` map into one row per `(window, process_comm)`.
- System-level probe fields (e.g. `oom_kills`, `futex_wait_count`, `sector_count`) are copied onto every process row in that window.
- Adds `run_id`, `window_start`, `window_end`, `comm`, and `root_cause`.

### 2.3 Procfs flattening & temporal join

- Nested PSI JSON is expanded with `pd.json_normalize(..., sep='_')` (e.g. `cpu.some.avg10` → `cpu_some_avg10`).
- Both datasets are sorted by timestamp.
- `pd.merge_asof(..., direction='nearest', tolerance=2.0)` joins each probe window row to the nearest PSI/vmstat sample within **2 seconds**.
- All runs are concatenated into `data/combined_features.csv`.

---

## 3. Anomaly-detection input prep (`anomalyDetection.py`)

Production detector: **OneClassSVM** (`kernel='rbf'`, `nu=0.05`), selected after `detectorBenchmark.py` comparison against IsolationForest and LocalOutlierFactor.

### 3.1 Column selection

- Drops non-feature identifiers: `window_start`, `window_end`, `timestamp`, `comm`, `root_cause`, `run_id`.
- Keeps **numeric** columns only (`select_dtypes(include=['number'])`) — exactly 35 numeric metrics in the production feature set.

### 3.2 Missing-value fill

- `fillna(0)` on the numeric feature matrix (metrics absent for idle processes in a given window become zeros).

### 3.3 Training subset & artifact persistence

- Production model is trained **strictly on Normal rows** (`root_cause == 'Normal'`).
- Refuses to train if fewer than 50 Normal rows exist (prevents training on corrupted/insufficient baselines).
- Normal training set is capped at 3 000 rows for RBF kernel tractability.
- `StandardScaler` is fit on the Normal subset and applied before fitting the OneClassSVM.
- Persists scaler, model, feature column list, and training metadata to `data/model/ocsvm.joblib`.

### 3.4 Scoring

- Production scoring (`--score`) aligns input columns to the trained feature list, fills missing columns with zeros, and applies the saved `StandardScaler`.
- Adds `anomaly_score` (decision function) and `predicted_anomaly` (True when `predict == -1`).
- Writes `data/scored_features.csv`.

---

## 4. Causal-graph prep (`analyzeCausalGraphs.py`)

### 4.1 Anomaly window merge

- Filters rows where `predicted_anomaly == True`.
- Merges overlapping anomaly intervals **per run** (±1 s pad) into contiguous anomaly episodes.

### 4.2 Per-run edge alignment & noise filtering

- Reads per-run edge file `data/edges_<run_id>.jsonl` collected with 1-in-100 sampling (`wakeupTracker.bt`) or 1-in-1000 sampling in daemon mode (`wakeupTrackerMonitor.bt`).
- Converts edge nanoseconds-since-boot to epoch using `boot_time_epoch` from `data/meta_<run_id>.json`.
- Filters out kernel worker and idle noise: `swapper/*`, `bpftrace`, `ksoftirqd/*`, `migration/*`, `rcu_*`, `cpuhp/*`.
- Retains only edges whose timestamp falls inside the merged anomaly window.
- Builds a directed graph: **Wakee → Waker** (blocked task points to who unblocked it).
- Identifies candidate roots as terminal blockers (`in_degree > 0` and `out_degree == 0`).
- Writes `data/causal_summaries.json` and `data/causal_graphs.json`.

---

## 5. Explainer prep (`ragExplainer.py`)

### 5.1 Knowledge indexing

- Runbook documents in `knowledge/*.md` are indexed using `TfidfVectorizer(stop_words='english')`.

### 5.2 Label-free retrieval query construction

- Retrieval queries are constructed from candidate roots, sample causal dependency paths, and keyword tokens from the shared `diagnosis.py` classifier (`QUERY_TOKENS`).
- **Ground-truth labels are never used** for retrieval or classification — diagnosis relies entirely on observed runtime evidence.

### 5.3 Classification & report synthesis

- `diagnosis.classify_cause()` evaluates evidence across deterministic tiers:
  1. Primary culprit comm identity (`fio`, `stress-ng-cpu/futex/dentr/brk`)
  2. Kernel OOM-kill tracepoint event (`oom_kills > 0` or `oom_kill_count > 0`)
  3. Memory stressor identity (`stress-ng-vm`)
  4. Metric signatures (futex wait time, locktime, PSI mem + major faults, swap, sectors, CPU-PSI)
  5. `unknown` fallback
- Extracts remediation actions directly from the top-scoring matched runbook.
- Writes human-readable reports to `data/rca_report.txt` and structured data to `data/rca_report.json`.

### 5.4 Bounded LLM review layer (optional)

- If `RCA_LLM_API_KEY` or a local endpoint (`RCA_LLM_BASE_URL`) is configured, queries an OpenAI-compatible endpoint or local Ollama instance (`/v1/chat/completions` with `/api/chat` fallback).
- Strictly grounded prompt requires the model to reason only on the cited metrics and graph evidence, preventing hallucinations.
- If disabled or if the network call fails, the grounded template report is used without interruption.

---

## 6. Daemon incremental preprocessing (`rcaDaemon.py`)

- **Offset tracking:** Per-run file byte offsets are tracked in `data/daemon_state.json`. Each 30 s cycle reads only newly appended bytes from `probeData_*.jsonl` and `procfsData_*.jsonl`.
- **Window-level anomaly rule:** A 3 s window (~290 process rows) is flagged as anomalous iff **≥ 50% of its rows** are flagged by the OneClassSVM (`--flag-fraction`). This eliminates idle false alerts (baselines peak at 0.44; fault windows are ≥ 0.94).
- **Episode lifecycle:** Consecutive anomalous windows merge into an episode; once an episode cools down (or upon cycle close), graph analysis and grounded RAG explanation are executed in-process.
- **Alert emission:** Appends structured JSON lines to `data/alerts.jsonl` and appends readable reports to `data/rca_report.txt`.

---

## Summary

| Stage | Input | Key preprocessing | Output |
|---|---|---|---|
| Procfs | `/proc/pressure`, `/proc/vmstat` | Sum zones, compute deltas, clamp ≥ 0, parse PSI | `data/procfsData_<run_id>.jsonl` |
| Probes | `probes.bt` map stream | `BEGIN_NS`/`WINDOW_END_NS` clock align, flatten per comm | `data/probeData_<run_id>.jsonl` |
| Features | Probe + procfs JSONL | Per-process explode, nearest-time join (2 s), label hygiene | `data/combined_features.csv` |
| Detector | Features CSV | Select 35 numeric metrics, `fillna(0)`, `StandardScaler`, OneClassSVM | `data/scored_features.csv`, `ocsvm.joblib` |
| Graphs | Scored CSV + sampled edges | Per-run merge (±1 s), boot epoch align, noise filter | `data/causal_summaries.json`, `graphs.json` |
| RAG | Causal summaries + `knowledge/` | Label-free query, TF-IDF cosine sim, `diagnosis.py` tiers, optional LLM review | `data/rca_report.txt`, `rca_report.json` |
| Daemon | Streaming collectors | Per-run byte offsets, ≥ 50% row rule, episode merge, alert stream | `data/alerts.jsonl`, `daemon_status.json` |
