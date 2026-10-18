# Step-by-Step Implementation Guide

This guide walks through building and running the Root Cause Analyser end to end: data collection → features → Isolation Forest → causal graphs → RAG LLM explanation.

Assumes project root:

```bash
cd /home/surya/programming/projects/RCA
```

---

## Phase 0 — Environment Setup

### Step 0.1 Install system tools

```bash
sudo apt update
sudo apt install -y bpftrace python3 python3-venv python3-pip stress-ng fio
```

Verify:

```bash
bpftrace -V
stress-ng --version
fio --version
ls /proc/pressure/   # cpu, memory, io should exist
```

### Step 0.2 Create Python virtualenv

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install scikit-learn==1.9.1 networkx==3.7 pandas numpy
```

For the RAG stage (Phase 5):

```bash
pip install langchain langchain-community chromadb sentence-transformers
# Local LLM example (install Ollama separately from https://ollama.com)
# ollama pull llama3.2
```

### Step 0.3 Confirm project files

Minimum required before collection:

| File | Role |
|---|---|
| `parentProcess.py` | Orchestrator + stressors |
| `procExtractor.py` | PSI / vmstat polling |
| `probeExtractor.py` | bpftrace map → JSONL |
| `probes.bt` | Kernel metric probes |
| `wakeupTracker.bt` | Wakeup edges → JSONL |
| `featureExtractor.py` | Merge + label |
| `anomalyDetection.py` | Isolation Forest |
| `analyzeCausalGraphs.py` | Causal graphs |

Create `data/` if missing:

```bash
mkdir -p data
```

---

## Phase 1 — Data Collection Layer

### Step 1.1 Understand `probes.bt`

Already implemented. It accumulates per-`comm` metrics and prints maps every 3s, then clears them. Marker string `"END"` delimits windows for `probeExtractor.py`.

No action required unless you add new metrics (then update `featureExtractor.py` + the Isolation Forest feature columns).

### Step 1.2 Confirm `wakeupTracker.bt`

File: `wakeupTracker.bt` (referenced by `parentProcess.py` as `wakeupTracker.bt`).

It must emit one JSON object per line with keys:

```text
timestamp, waker_tid, waker_pid, waker_comm, wakee_tid, wakee_comm
```

Smoke test (Ctrl+C after a few seconds):

```bash
sudo stdbuf -oL bpftrace wakeupTracker.bt | head -n 5
```

Expected: bpftrace attach message, then JSONL edges.

### Step 1.3 Confirm extractors

- `procExtractor.py` — polls every 1s; writes `data/procfsData_<run_id>.jsonl`
- `probeExtractor.py` — runs `bpftrace -f json probes.bt`; writes `data/probeData_<run_id>.jsonl`

Both exit on `SIGINT` from `parentProcess.py`.

### Step 1.4 Run a baseline (Normal) collection

```bash
sudo python3 parentProcess.py default
```

Produces:

- `data/procfsData_default.jsonl`
- `data/probeData_default.jsonl`
- Appends wakeup edges to `data/edges.jsonl`

### Step 1.5 Run the stressor suite (test suite)

Built into `parentProcess.py`. Run each class you care about (examples):

```bash
sudo python3 parentProcess.py cpuStressor1
sudo python3 parentProcess.py cpuStressor2
sudo python3 parentProcess.py cpuStressor3
sudo python3 parentProcess.py memStressor1
sudo python3 parentProcess.py memStressor2   # aggressive — use carefully
sudo python3 parentProcess.py memStressor3
sudo python3 parentProcess.py lockStressor1
sudo python3 parentProcess.py lockStressor2
sudo python3 parentProcess.py ioStressor1
sudo python3 parentProcess.py ioStressor2
```

Or a custom command:

```bash
sudo python3 parentProcess.py myRun 'stress-ng --cpu 2 --timeout 30s'
```

**Checkpoint:** For each `run_id`, both `probeData_<run_id>.jsonl` and `procfsData_<run_id>.jsonl` should be non-empty. `edges.jsonl` should grow during runs.

---

## Phase 2 — Feature Engineering

### Step 2.1 Review labeling map

In `featureExtractor.py`, `ROOT_CAUSES` maps stressor IDs → labels used later for evaluation (not required for unsupervised Isolation Forest scoring, but used for recall reports).

### Step 2.2 Build the combined feature table

```bash
python3 featureExtractor.py
```

Output: `data/combined_features.csv`

### Step 2.3 Sanity checks

```bash
python3 - <<'PY'
import pandas as pd
df = pd.read_csv('data/combined_features.csv')
print(df.shape)
print(df['root_cause'].value_counts())
print(df.columns.tolist())
PY
```

**Checkpoint:**

- `Normal` rows exist (from `default` runs)
- Stressor labels appear for collected run_ids
- Numeric feature columns are mostly filled (NaNs OK; anomaly stage fills with 0)

---

## Phase 3 — Anomaly Detection (Isolation Forest)

### Step 3.1 How the model is trained

`anomalyDetection.py`:

1. Selects feature columns (excludes ids / labels / timestamps)
2. Fits Isolation Forest on rows where `root_cause == 'Normal'`
3. Scores all rows → `anomaly_score`, `predicted_anomaly`
4. Prints flagged rates per labeled class
5. Writes `data/scored_features.csv`

### Step 3.2 Run detection

```bash
python3 anomalyDetection.py
```

### Step 3.3 Interpret the summary

- High `flagged_rate` on stressor classes ⇒ good recall
- Low `flagged_rate` on `Normal` ⇒ good specificity (false positives)

If recall is weak or FP rate is high:

1. Collect more / cleaner `default` baselines
2. Tune `contamination` or feature set
3. Address timestamp alignment issues noted in `changes.md`

**Checkpoint:** `data/scored_features.csv` has `predicted_anomaly` True for many stressed windows.

---

## Phase 4 — Causal Graph Construction

### Step 4.1 What the graph means

From wakeup edges:

- **Waker** = task that woke another task
- **Wakee** = task that was woken (was blocked)

Graph edge direction in code: **Wakee → Waker**  
(“blocked task points at the task that unblocked it”)

Candidate root causes: nodes with incoming edges but no outgoing edges (terminal blockers).

### Step 4.2 Run graph analysis

```bash
python3 analyzeCausalGraphs.py
```

### Step 4.3 Read the output

For each merged anomaly window you should see:

- Node / edge counts
- Candidate root cause list
- Sample paths: `symptom -> ... -> root`

**Checkpoint:** At least one window with edges and a plausible candidate (often a stress-ng / fio related `comm` under load).

### Step 4.4 Optional improvements

- Filter edges where waker/wakee is `swapper/*`, pure IRQ noise, or bpftrace itself
- Persist graphs to JSON/GML for the RAG stage
- Export a compact summary dict per window (used by Phase 5)

Suggested summary shape for later RAG input:

```json
{
  "window_id": "window_12",
  "start": 123.0,
  "end": 130.0,
  "candidate_roots": ["stress-ng-cpu:41553"],
  "sample_paths": ["app:1 -> kworker:2 -> stress-ng-cpu:41553"],
  "top_metrics": {"runtime": 1.2e9, "futex_wait_count": 800}
}
```

---

## Phase 5 — RAG LLM Explainer (to implement)

Create `ragExplainer.py`. Suggested steps:

### Step 5.1 Build a knowledge corpus

Create `knowledge/` with short markdown notes, for example:

- `cpu_competition.md` — high `runtime` / runqueue wait, many wakeups among competing tasks
- `memory_pressure.md` — PSI mem full, swap in/out, allocstall, major faults
- `oom.md` — `oom_kill_count` / `oom_time` spikes
- `lock_contention.md` — futex / locktime elevated
- `io_saturation.md` — iowait, sector_count, fio patterns

Also ingest:

- Per-window causal summaries from Phase 4
- Rows from `scored_features.csv` where `predicted_anomaly == True`

### Step 5.2 Chunk + embed + store

Pseudocode:

```python
from langchain_community.document_loaders import DirectoryLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma

docs = DirectoryLoader('knowledge', glob='**/*.md').load()
# also append Documents built from anomaly window summaries
chunks = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100).split_documents(docs)
embeddings = HuggingFaceEmbeddings(model_name='sentence-transformers/all-MiniLM-L6-v2')
vs = Chroma.from_documents(chunks, embeddings, persist_directory='data/chroma')
```

### Step 5.3 Retrieve + prompt

For the latest anomaly window:

1. Build a query string from metrics + candidate roots + paths
2. `retriever.get_relevant_documents(query)`
3. Call LLM with a fixed system prompt, e.g.:

```text
You are a Linux performance root-cause assistant.
Using ONLY the provided context (metrics, causal paths, docs),
state the most likely root cause, evidence, and a short remediation.
If uncertain, say what extra signal is needed.
```

### Step 5.4 Output to the user

Print / write something like:

```text
=== RCA Report ===
Likely root cause: CPU Competition (stress-ng-cpu)
Evidence:
  - Isolation Forest flagged windows W12–W14
  - Causal path: firefox-bin -> kworker/8:0 -> stress-ng-cpu:41553
  - Elevated runtime / rq wait during stressor interval
Suggested action: reduce competing CPU load or pin/isolate critical tasks
```

### Step 5.5 Wire into the pipeline

Add a final call after graphs:

```bash
python3 featureExtractor.py
python3 anomalyDetection.py
python3 analyzeCausalGraphs.py
python3 ragExplainer.py
```

Or invoke `ragExplainer.main()` from a thin `run_pipeline.py` wrapper.

---

## Phase 6 — Background / Continuous Mode (optional)

`parentProcess.py` today is run-oriented (stressor timeout). For always-on RCA:

### Step 6.1 Create `rcaDaemon.py`

Behavior:

1. Start `procExtractor`, `probeExtractor`, `wakeupTracker` with a rolling `run_id` (e.g. date hour)
2. No stressor (or optional watchdogs only)
3. Every N minutes: featureize recent files → score → if anomalies, build graphs → RAG report
4. Rotate / truncate `edges.jsonl` safely

### Step 6.2 Operational concerns

- Disk growth from `edges.jsonl` (high volume)
- CPU overhead of bpftrace under heavy scheduling
- Privilege model for a long-lived service (systemd unit with capabilities)

---

## Phase 7 — Validation Checklist

Use the built-in stressors as the ground-truth test suite.

| Test | Collect | Expect Isolation Forest | Expect graph / RAG hint |
|---|---|---|---|
| Baseline | `default` | Few / no flags | N/A or empty |
| CPU fight | `cpuStressor1` | Flags around stress window | stress-ng-cpu in path/roots |
| CPU throttle | `cpuStressor2` | Flags | throttled / low quota context |
| Memory swap | `memStressor1` | Flags | high mem PSI / swap counters |
| OOM path | `memStressor2` | Flags | oom metrics |
| Futex locks | `lockStressor1` | Flags | futex wait spikes |
| VFS locks | `lockStressor2` | Flags | locktime / dentry related |
| Random IO | `ioStressor1` | Flags | iowait / sector_count |
| Seq IO | `ioStressor2` | Flags | high sector_count |

Pass criteria for an iteration:

1. Normal FP rate reasonably low
2. Target stressor class recall high enough for your use case
3. Graph candidates correlate with the injected fault
4. RAG text names the correct class more often than chance

---

## Recommended Implementation Order

1. ~~Extractors + probes~~ (done)
2. ~~`wakeupTracker.bt`~~ (done)
3. ~~Feature merge + Isolation Forest + causal graphs~~ (done)
4. **Export structured graph summaries** from `analyzeCausalGraphs.py`
5. **Implement `ragExplainer.py`** + `knowledge/` docs
6. **End-to-end script** (`run_pipeline.py`) for one-command demos
7. **Optional daemon** for background monitoring
8. Harden training data / timestamps per `changes.md`

---

## Common Pitfalls

| Issue | Cause | Fix |
|---|---|---|
| Empty probe files | bpftrace failed / no sudo | Run with sudo; check `probes.bt` syntax |
| No anomalies flagged | Missing `default` training data | Collect longer Normal runs |
| Graphs empty | Timestamp skew between edges and windows | Align boot-time vs epoch conversion; pad windows |
| Edges file huge | Wakeups are very frequent | Filter comms; rotate file; sample |
| RAG hallucinations | Weak retrieval / no grounding | Strict prompt: use only retrieved context |

---

## One-Command Demo Flow (after RAG exists)

```bash
source .venv/bin/activate
sudo python3 parentProcess.py default
sudo python3 parentProcess.py cpuStressor1
python3 featureExtractor.py
python3 anomalyDetection.py
python3 analyzeCausalGraphs.py
python3 ragExplainer.py
```

That sequence is the intended product path: inject or observe a fault, detect it, graph it, and explain it.
