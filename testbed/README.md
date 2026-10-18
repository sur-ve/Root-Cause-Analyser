# RCA Testbed — Evaluator Guide

This directory contains the scripts used to collect the fault corpus, evaluate
pipeline accuracy, and run the live end-to-end demonstration.

## Prerequisites

| Requirement | Check |
|---|---|
| Linux kernel ≥ 5.4 with eBPF + PSI | `/proc/pressure/cpu` readable |
| bpftrace 0.16+ | `bpftrace --version` |
| stress-ng, fio, stdbuf | `command -v stress-ng fio stdbuf` |
| systemd-run | available on systemd hosts |
| Python 3.10+, project `.venv` | `.venv/bin/python --version` |
| ≥ 4 GiB disk free | `df -BG .` |
| ≥ 2 GiB MemAvailable | `awk '/MemAvailable/{print $2}' /proc/meminfo` |

> [!IMPORTANT]
> All collection scripts (`run_fault.sh`, `run_all.sh`, `demo_live.sh`)
> **require root** (`sudo`). The replay and evaluation scripts do **not**.

---

## Quick Demo (3 steps)

### Step 1 — Recorded-corpus replay (no root, ~30 s)

```bash
python3 testbed/demo_replay.py
```

Runs `feature → score → graph → explain` on the pre-collected 16-run corpus
and prints a per-fault results table plus a sample RCA report.

### Step 2 — Evaluate accuracy

```bash
python3 testbed/evaluate.py
```

Produces `data/evaluation_results.csv` (per-fault ground truth vs detection
vs predicted cause) and `data/demo_metrics.json` (Top-1 accuracy, MTTR).

### Step 3 — Live background proof (root required, ~3 min)

```bash
sudo bash testbed/demo_live.sh
```

Starts the daemon, injects a safe CPU fault, waits for an alert in
`data/alerts.jsonl`, prints the alert + RCA report excerpt, then stops
the daemon. The alert should appear within 1–2 cycles (≤ 60 s).

---

## Full Fault Matrix (~30–40 min, root required)

```bash
# Check all prerequisites
sudo bash testbed/preflight.sh --clean

# Collect all 10 faults (resumes if interrupted)
sudo bash testbed/run_all.sh

# Rebuild features + retrain
python3 featureExtractor.py
python3 anomalyDetection.py --train
python3 analyzeCausalGraphs.py
python3 ragExplainer.py

# Evaluate
python3 testbed/evaluate.py
```

> [!WARNING]
> `memStressor2` deliberately triggers repeated OOM kills inside a cgroup
> scope. On a workstation with < 8 GiB RAM it may be slow. Use a VM for
> this run and skip it if disk space is tight.

---

## Script Reference

| Script | Root? | Purpose |
|---|---|---|
| `preflight.sh [--clean]` | yes | Check tools, disk, memory, bpftrace smoke test |
| `run_fault.sh <run_id>` | yes | One guarded fault run + post-run verification |
| `run_all.sh` | yes | Full 10-fault matrix, least→most destructive, resumable |
| `demo_replay.py` | no | Offline replay of the corpus through the full pipeline |
| `evaluate.py` | no | Ground truth vs pipeline output → results CSV + metrics JSON |
| `demo_live.sh` | yes | Daemon + fault injection → alert within N cycles |
| `test_llm.py` | no | Unit tests for LLM review layer (cloud APIs + local Ollama) |

### run_fault.sh run IDs

| Run ID | Fault class | Family |
|---|---|---|
| `default` / `default1`–`default5` | Idle baseline | Normal |
| `lockStressor1` | Userspace futex contention | lock |
| `lockStressor2` | Kernel VFS/dentry contention | lock |
| `cpuStressor1` | CPU competition (2 workers) | cpu |
| `cpuStressor2` | CPU throttling (quota) | cpu |
| `cpuStressor3` | Baseline CPU usage | cpu |
| `memStressor1` | Swap thrashing | memory |
| `memStressor2` | Cgroup OOM kill (safe) | oom |
| `memStressor3` | Continuous heap expansion | heap |
| `ioStressor1` | Small random writes | io |
| `ioStressor2` | Large sequential writes | io |

---

## Output Artefacts

| Artefact | Content |
|---|---|
| `data/evaluation_results.csv` | Per-fault: truth, detected?, cause, match, latency |
| `data/demo_metrics.json` | Top-1 accuracy, detection recall/FPR, MTTR |
| `data/alerts.jsonl` | Machine-readable alerts from the live demo |
| `data/rca_report.txt` | Human-readable RCA report (batch + daemon) |
| `data/ground_truth.jsonl` | Collected ground truth (start/end timestamps + labels) |

---

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `RCA_LLM_API_KEY` | _(unset)_ | Enable online LLM review (OpenAI, Groq, OpenRouter) |
| `RCA_LLM_BASE_URL` | `https://api.openai.com/v1` | Override API endpoint (e.g. `http://localhost:11434/v1` for local Ollama) |
| `RCA_LLM_MODEL` | `gpt-4o-mini` | Model name (e.g. `llama3.2` for Ollama) |
| `DRY_RUN=1` | 0 | Print the plan without running collection (no root needed) |
| `FORCE=1` | 0 | Force re-collection even when run files exist |
| `PYTHON` | `.venv/bin/python` | Python interpreter override |
