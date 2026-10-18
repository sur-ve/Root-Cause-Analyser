# Preprocessing Steps

All transforms applied before anomaly detection and causal analysis, in pipeline order.

---

## 1. Collection-time transforms

### 1.1 Procfs delta conversion (`procExtractor.py`)

- Primer read of `/proc/vmstat` establishes a baseline.
- Each 1s sample stores **deltas**, not absolute counters:
  - `allocstall_count`, `major_fault_count`, `swap_in_count`, `swap_out_count`, `oom_kill_count`
- `allocstall_*` zones (`dma`, `dma32`, `normal`, `movable`) are **summed** into a single `allocstall` before differencing.
- PSI text from `/proc/pressure/{cpu,memory,io}` is parsed into nested dicts (`some` / `full` × `avg10` / `avg60` / `avg300` / `total`).
- Each record is stamped with UNIX `time.time()` and written as JSONL.

### 1.2 Probe window alignment (`probeExtractor.py`)

- bpftrace map dumps are buffered until an `"END"` marker (every ~3s).
- Per-`comm` map entries are **flattened** into `processes[comm][metric]`.
- First window start is backdated to `fin_timestamp - 3.0` when attach delay would otherwise skew the start.
- Subsequent windows use the previous END time as `window_start`.
- Non-JSON bpftrace chatter (e.g. “Attaching …”) is dropped.

### 1.3 Run metadata (`parentProcess.py`)

- Writes `data/meta_<run_id>.json` with `boot_time_epoch`, `start_time`, and stressor command for later edge↔window alignment.

---

## 2. Feature extraction (`featureExtractor.py`)

### 2.1 Run discovery

- Auto-discovers `data/probeData_*.jsonl` / matching `procfsData_*.jsonl`.
- Skips missing, empty, or unparseable run pairs.

### 2.2 Probe flattening

- One row per `(window, process_comm)`.
- System-level probe fields (non-process maps) are copied onto every process row in that window.
- Adds `run_id` and human `root_cause` label from the stressor map (`default` → `Normal`).

### 2.3 Procfs flattening

- Nested PSI JSON is expanded with `pd.json_normalize(..., sep='_')`  
  (e.g. `cpu.some.avg10` → `cpu_some_avg10`).

### 2.4 Temporal join

- Both sides sorted by time.
- `pd.merge_asof(..., direction='nearest', tolerance=2.0)` joins each probe window to the nearest PSI/vmstat sample within **2 seconds**.
- Concatenates all runs → `data/combined_features.csv`.

---

## 3. Anomaly-detection input prep (`anomalyDetection.py`)

Production detector: **OneClassSVM** (`kernel='rbf'`, `nu=0.05`), selected after
`detectorBenchmark.py` comparison against IsolationForest and LocalOutlierFactor.

### 3.1 Column selection

- Drops non-features: `window_start`, `window_end`, `timestamp`, `comm`, `root_cause`, `run_id`.
- Keeps **numeric** columns only (`select_dtypes(include=['number'])`).

### 3.2 Missing-value fill

- `fillna(0)` on the feature matrix (sparse probe metrics absent for idle processes become zeros).

### 3.3 Training subset

- Prefer rows with `root_cause == 'Normal'` when ≥ 10 exist; otherwise train on all rows.
- Training set is capped at 3000 rows for RBF tractability.

### 3.4 Feature scaling

- `StandardScaler` is fit on the Normal training subset and applied to all rows
  before OneClassSVM fit/predict (required for RBF kernel geometry).

---

## 4. Causal-graph prep (`analyzeCausalGraphs.py`)

### 4.1 Anomaly window merge

- Filters `predicted_anomaly == True`.
- Deduplicates by `(window_start, window_end)`.
- Pads ±1s and **merges overlapping** intervals.

### 4.2 Wakeup edge hygiene

- Converts bpftrace `nsecs`-since-boot to epoch via `boot_time_epoch` (meta file or reboot-aware inference).
- Drops noise comms: `swapper/*`, `bpftrace`, `ksoftirqd/*`, `migration/*`, `rcu_*`, `cpuhp/*`.
- Keeps only edges whose epoch timestamp falls inside a merged anomaly window.

---

## 5. Explainer prep (`ragExplainer.py`)

- Knowledge markdown under `knowledge/` is TF-IDF indexed (`stop_words='english'`).
- Retrieval query is built from labeled causes, candidate roots, causal paths, and (if unlabeled) metric heuristics.
- No further numeric feature scaling is applied beyond the steps above.

---

## Summary

| Stage | Key preprocessing |
|---|---|
| Procfs | Counter → delta; zone sum; PSI parse |
| Probes | Map flatten; first-window time fix |
| Features | Per-process explode; nearest-time join (2s); label |
| Detector | Numeric select; NaN→0; Normal-only train; StandardScaler; OneClassSVM |
| Graphs | Overlap merge; boot-time align; noise filter |
| RAG | TF-IDF corpus index |

Production path uses **StandardScaler + OneClassSVM**. `detectorBenchmark.py` still compares IsolationForest (unscaled) and LOF (scaled) for regression checks.
