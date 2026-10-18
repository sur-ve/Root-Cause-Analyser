# RCA — Bug-fix, Background Monitoring & Evaluator Testbed Plan

**Date:** 2026-10-18
**Scope:** take the current offline prototype to (A) a reliable background monitor and (B) a repeatable testbed that demonstrates *detect → causal graph → explanation* to an evaluator.

This plan is based on reading every source file and executing the pipeline against the current corpus. All "evidence" below was observed live, not inferred from comments.

---

## 1. Verified current state

### 1.1 What already works

| Component | Status |
|---|---|
| Collection stack (`parentProcess.py` + `procExtractor.py` + `probeExtractor.py` + `probes.bt` + `wakeupTracker.bt`) | Produces probeData/procfsData/edges from 6 recorded runs |
| Offline chain `featureExtractor → anomalyDetection → analyzeCausalGraphs → ragExplainer` | Runs end-to-end; reports written to `data/rca_report.txt/.json` |
| Causal graphs | Correctly surface `stress-ng-cpu`, `stress-ng-vm` as terminal blockers |
| RAG reports | Accurate cause + evidence + remediation when labels exist |
| Benchmarks | `detectorBenchmark.py` / `overallBenchmark.py` run (numbers currently invalid, see below) |

### 1.2 Blocking bugs and risks (with evidence)

| # | Issue | Evidence | Impact |
|---|---|---|---|
| B1 | **Daemon passes `run_id` where stages expect file paths.** `rcaDaemon.run_analysis_cycle` runs `python3 <stage> <run_id>`; `anomalyDetection.py` reads `sys.argv[1]` as the input CSV | `python3 anomalyDetection.py rolling_...` → `[-] Feature file 'rolling_...' not found`, exit 1; graph/explainer stages silently no-op | **Background analysis never runs** |
| B2 | **No `Normal` baseline exists.** Corpus has 6 fault classes only | Detector prints `Insufficient 'Normal' rows (0). Training on all available data`; per-class recall **3.2–11.3 %**; benchmark F1 **OneClassSVM = 0.116** (README claims ~0.997) | Detector is useless as-is; benchmark/demo numbers unshowable |
| B3 | **Rolling/unknown data mislabeled.** `featureExtractor` labels unknown run_ids `Custom (run_id)`; `ragExplainer` classifies from `labeled_causes` first | A live daemon would print `Likely root cause: Custom (rolling_...)` | Reports garbage in background mode |
| B4 | **`edges.jsonl` unbounded: 3.18 GB / 23.8 M lines for ~6 min of collection (~530 MB/min)** | `ls -l data/edges.jsonl`; rotation only triggers >50 MB and uses `readlines()` (whole file into RAM) while bpftrace appends | Disk at **97 % (2.7 GB free)**; rotation is a RAM bomb + rewrite race |
| B5 | **Causal graph stage takes 4 m 22 s** (two full JSON passes over 23.8 M lines) | `time python3 analyzeCausalGraphs.py` | Daemon stage `timeout=120 s` → **guaranteed timeout every cycle**; live demo impossible |
| B6 | **Kernel scheduler stats are off:** `kernel.sched_schedstats=0` → `sched_stat_wait` / `sched_stat_iowait` never fire | `rq_time`, `iowait_time`, `oom_time` columns absent from `data/combined_features.csv`; `/proc/sys/kernel/sched_schedstats` = 0 | Two documented metrics + one explainer heuristic silently dead |
| B7 | **Futex probe counts every futex syscall** (op not filtered; includes WAKE/fast paths) | 399 583 "futex waits" in a pure CPU-stressor window; heuristic `futex > 1000` fires everywhere | Lock detection signal unusable, contaminates all classes |
| B8 | **Testbed hazards.** memStressor2 OOM'd the host — machine rebooted mid-collection; memStressor3 truncated to 2 probe windows. ioStressor2 asks for 5 GB with 2.7 GB free and does not `--unlink` | `uptime` 9 min vs pre-reboot meta files; `probeData_memStressor3` has 2 windows vs 22 for others | Repeats tester experience: reboots, ENOSPC failures |
| B9 | **Ground truth incomplete.** Meta JSON has `start_time` only | `data/meta_*.json` | Cannot compute MTTR or window-level accuracy; benchmarks are row-level only |
| B10 | No git repo; `data/` artefacts owned by root | `Is directory a git repo: no`; `ls -la data/` | No safety net for refactors; user can't clean daemon output without sudo |

Also fixed-doc drift: `data.txt` review items (bare `parentProcess.py`, `autoExtractor.sh` sudo, partial datasets) are **already fixed in current code** — the checklist is stale.

Corpus gaps for the 10-fault matrix: only `cpuStressor1-3`, `memStressor1-3` exist. Missing: `default`, `lockStressor1/2`, `ioStressor1/2`.

---

## 2. Target architecture for background mode

```
PHASE A — learn (run once, or after big system changes)
  sudo python3 rcaDaemon.py --learn 300
      collect 5 min idle/desktop activity  ──▶  featureExtractor (Normal rows)
      anomalyDetection.py --train          ──▶  data/model/ocsvm.joblib
                                               (+ scaler, feature list, metrics)

PHASE B — monitor (long-running)
  sudo python3 rcaDaemon.py --interval 30
      collectors ──▶ per-cycle run files (probe/procfs/edges + meta with labels)
      every 30 s:
        new windows only (offset tracking) ──▶ score with saved model
        if anomaly:
          build graph from THIS cycle's aggregated edges
          build report (metrics + graph + TF-IDF retrieval, NO label peeking)
          append data/alerts.jsonl, update data/rca_report.txt
          optional desktop notification
      housekeeping: restart dead collectors, delete processed run files
                    older than N hours, heartbeat to data/daemon_status.json
```

Key design decisions (and why):

1. **Aggregate wakeup edges at source.** New `wakeupAggregator.bt` batches 3 s windows in-kernel
   (`@edge[wakee_comm, waker_comm] = count()`), emitting one compact JSONL envelope per window
   (pair counts + boot-relative window bounds, mirroring `probes.bt`'s `END` marker).
   → 530 MB/min becomes a few KB/min, boot-time guessing disappears, graph stage drops from
   minutes to milliseconds. Raw `wakeupTracker.bt` is kept as `--raw-edges` for short demo runs.
2. **Per-run files, not one global append file.** `data/<kind>_<run_id>.jsonl` for edges too;
   daemon deletes processed runs after retention. No 3 GB re-reads, no rotation races.
3. **Persisted model.** Train once on Normal; daemon only scores. `joblib` (ships with sklearn).
   `--train` / `--score` modes in `anomalyDetection.py`; production path unchanged for offline use.
4. **Label hygiene.** `parentProcess.py` writes the true label into `meta_<run>.json`
   (`Normal` for idle, fault id otherwise); `featureExtractor` reads meta first, falls back to
   `ROOT_CAUSES`, unknown → `Unlabeled`. `ragExplainer` must never print a raw label as the cause:
   labeled data is for *evaluation only*; the report path uses metrics + graph + retrieval.
5. **Incremental analysis.** Track last-scored window timestamp per run; each cycle only new
   windows. Bounded CPU per cycle (target < 5 s).

---

## 3. Phased execution plan

### Phase 0 — Housekeeping & safety (≈1 hour)

| Task | File / command | Acceptance |
|---|---|---|
| 0.1 Free disk: archive or delete `data/edges.jsonl` (regenerable) | `rm data/edges.jsonl` (frees 3.2 GB) | ≥ 5 GB free |
| 0.2 `git init`, commit code; ignore artefacts | `.gitignore`: add `data/*.jsonl`, `data/*.csv`, `data/*.json`, `__pycache__/`, `.venv/`, `*.joblib` | `git log` shows clean baseline |
| 0.3 Fix fio stressors: sizes ≤ 512 MB, add `--unlink=1` | `parentProcess.py` `STRESSORS` | no leftover files; fits on disk |
| 0.4 Enable kernel scheduler stats | `sudo sysctl -w kernel.sched_schedstats=1` + preflight check | `rq_time`/`iowait_time` appear after a smoke run |
| 0.5 Update `changes.md`/`data.txt` to point at this plan | docs | stale checklist removed |

### Phase 1 — Collection-layer fixes (≈½ day)

| Task | File | Acceptance |
|---|---|---|
| 1.1 Write `wakeupAggregator.bt`: comm-pair counts per 3 s window, `END` marker, boot-rel bounds | new file | ≤ 50 KB per 60 s run; parser round-trips |
| 1.2 Emit/collect edges per run; wire into `parentProcess.py` + daemon | `parentProcess.py`, `rcaDaemon.py` | no global `edges.jsonl` growth |
| 1.3 Record stressor `start_time` / `end_time` + ground-truth label in meta; append `data/ground_truth.jsonl` | `parentProcess.py` | `jq` shows start<end within run; file has fault + family |
| 1.4 Fix `probeExtractor` first-window timestamp hack: probes.bt `BEGIN { printf(...nsecs...) }`, derive window_start from bpftrace clock + meta boot epoch | `probes.bt`, `probeExtractor.py` | consecutive windows abut; no 3.0 s magic constant |
| 1.5 Filter futex probe to wait-like ops only (`op & 0x7f` ∈ {0, 9, 11}); keep count semantics | `probes.bt` | CPU stressor window futex count drops orders of magnitude |
| 1.6 `procExtractor`: tolerate missing vmstat keys (older/newer kernels) | `procExtractor.py` | runs on kernels without `allocstall_dma32` |
| 1.7 Per-second rate normalization of count features at read time (optional but helps cross-window comparability) | `featureExtractor.py` | window duration no longer leaks into features |

### Phase 2 — Baseline + detector (≈½–1 day)

| Task | File | Acceptance |
|---|---|---|
| 2.1 Collect 4–6 `default` baseline runs (~60 s each) at different times of day | `parentProcess.py default` | ≥ 8 000 Normal rows in `combined_features.csv` |
| 2.2 Label hygiene: meta-first labels, `Unlabeled` fallback | `featureExtractor.py` | no `Custom (...)` labels in output |
| 2.3 `anomalyDetection.py --train` / `--score`: persist scaler+model+feature list+metrics to `data/model/` | `anomalyDetection.py` | `--score` on unseen data works without refit |
| 2.4 Window-level helpers: aggregate rows→window features; match windows to ground truth | `featureExtractor.py` or new `windowUtils.py` | per-fault flagged-rate table matches `overallBenchmark` output |
| 2.5 Re-run `detectorBenchmark.py` + `overallBenchmark.py`; retune `nu`, fix family-heuristic thresholds with normalized metrics | benchmarks | Normal FPR ≤ 5–10 % AND ≥ 8/10 classes recall ≥ 0.8 |
| 2.6 Update README numbers with measured values | `README.md` | no unreproducible claims |

### Phase 3 — Background daemon rewrite (≈1 day)

| Task | File | Acceptance |
|---|---|---|
| 3.1 Fix stage invocation: explicit flags (`--scored-file`, `--summaries-file`, …) or in-process function calls; never reuse `run_id` as path | `rcaDaemon.py`, stage `main()`s | each stage runs and logs success in `--no-collect` smoke test |
| 3.2 Two-phase operation `--learn N` / `--monitor` with persisted model | `rcaDaemon.py` | learn produces a model file; monitor starts without refit |
| 3.3 Incremental scoring of only new windows; graph/explain only on flags | `rcaDaemon.py` | cycle wall-time < 5 s with 24 h of data retained |
| 3.4 Alerting: `data/alerts.jsonl` (timestamp, window, cause, culprit, root, remediation), human-readable `rca_report.txt` with **readable timestamps**, optional `notify-send` | `rcaDaemon.py`, `ragExplainer.py` | one alert line per anomalous window |
| 3.5 Housekeeping: collector health/restart, heartbeat `daemon_status.json`, retention of run files (keep last N hours) | `rcaDaemon.py` | 2 h soak, no orphan processes, disk growth < 50 MB/h |
| 3.6 systemd unit with capability bounding set (`CAP_BPF`, `CAP_PERFMON`) + docs | `deploy/rca-daemon.service` | service start/stop/restart works |
| 3.7 Graceful shutdown persists last-scored offset (no reprocessing after restart) | `rcaDaemon.py` | restart continues where it stopped |

### Phase 4 — Evaluator testbed (≈1 day)

```
testbed/
├── preflight.sh        # root/tools/disk/schedstats checks, kill stale collectors, smoke-test bpftrace
├── run_fault.sh ID     # one cgroup-guarded parentProcess.py run + verification + ground truth
├── run_all.sh          # 10-fault matrix + 2 baselines, least→most destructive, resume support
├── demo_replay.py      # offline replay of a recorded corpus through the full pipeline (no root)
├── demo_live.sh        # start monitor (root) → inject one safe fault → show alert within N s
├── evaluate.py         # ground truth vs pipeline output → results table + metrics JSON/CSV
└── README.md           # evaluator-facing runbook
```

| Task | Detail | Acceptance |
|---|---|---|
| 4.1 Safety rails | memory stressors under `systemd-run --scope -p MemoryMax=60% -p MemorySwapMax=1G`; disk preflight ≥ 4 GB; abort on load/disk thresholds; VM recommended for `memStressor2` | matrix completes; host does not reboot |
| 4.2 Ground truth | every run appends `{run_id, fault, family, expected_cause, start_ts, end_ts}` | 10/10 runs recorded |
| 4.3 Collection verification | after each run: probe/procfs/edges non-empty, meta dates sane, no collector leftovers | script fails loudly on partial runs |
| 4.4 `evaluate.py` | per-fault table: ground truth vs detector flagged vs predicted cause family vs graph root match; **alert latency (MTTR)**; collector CPU overhead (`pidstat`/`/proc/<pid>/stat`) | `data/evaluation_results.csv` + summary printed |
| 4.5 `demo_replay.py` | deterministic demo on recorded labeled corpus: pipeline stages + results table + one full report excerpt | runs without root in one command |
| 4.6 `demo_live.sh` | monitor + single `cpuStressor1` (safe, 45 s); alert must appear ≤ 1 cycle after fault | end-to-end live proof |
| 4.7 Honest measurement | Top-1 family accuracy computed on the **label-free** path (metrics + graph + retrieval), never from `labeled_causes` | numbers stand up to scrutiny |

### Phase 5 — Optional LLM layer + final docs (≈½ day, decide)

- Ollama is **not installed** (`command -v ollama` → missing). The timeline mentions LLM diagnosis;
  the code has a disabled `query_local_ollama` hook.
- If required for the evaluator: install Ollama + a small model, enable the hook with strict
  grounding (graph + metrics + retrieved docs only), keep TF-IDF path as fallback.
- Finish by refreshing `README.md`, `IMPLEMENTATION.md`, `preprocessing.md`, and adding
  `DEMO.md` (evaluator click-through).

---

## 4. Evaluator demo flow (after Phases 0–4)

```bash
# 1) Recorded-corpus demo (fast, deterministic, no root)
python3 testbed/demo_replay.py            # pipeline + per-fault results table

# 2) Live background proof (root; safe fault only)
sudo testbed/demo_live.sh                 # learn (or load model) → monitor → inject cpu fault
                                          # → alert + report printed when detected

# 3) Full matrix (optional, ~30–40 min, VM recommended for memory faults)
sudo testbed/run_all.sh                   # collect → evaluate
python3 testbed/evaluate.py               # Top-1 accuracy, MTTR, overhead table
```

Expected artefacts for the evaluator:

| Artefact | Content |
|---|---|
| `data/evaluation_results.csv` | per-fault: truth, detected?, cause, match, latency |
| `data/demo_metrics.json` | Top-1 accuracy, detection recall/FPR, MTTR, tracer CPU overhead |
| `data/alerts.jsonl` | machine-readable alerts from the live demo |
| `data/rca_report.txt` | human-readable RCA report |
| `DEMO.md` | what to click/run during the presentation |

---

## 5. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Host reboot during memory faults (already happened) | cgroup-scoped stressors (`MemoryMax`), run `memStressor2` only in a VM, keep swap pressure bounded |
| Disk exhaustion (97 % full now) | Phase 0 cleanup, aggregated edges, fio `--unlink`, retention policy, preflight check |
| sudo password prompts break automation | document one `sudo -v` warm-up; `preflight.sh` fails fast with clear message; optional sudoers entry (user decision) |
| bpftrace attach failure under load | preflight smoke test; parentProcess already aborts on tracker death |
| Timestamp drift across reboot | per-run meta boot epoch + aggregator envelope; drop the multi-boot inference heuristics once per-run files exist |
| Evaluator asks "is the cause label real?" | Phase 4.7: all accuracy numbers from the label-free path; labels only used for scoring |
| Detector degrades over time | persisted model + periodic `--retrain` recipe; README documents baseline refresh |

---

## 6. Open decisions

1. **Where does the full 10-fault matrix run?** Recommended: this workstation with cgroup guards for all
   except `memStressor2`, which runs in a VM (or is skipped/documented).
2. **Live LLM diagnosis?** Decided (2026-10-18): **online OpenAI-compatible API**
   (`RCA_LLM_API_KEY`) — no local model install; the grounded template report is the
   fallback when no key is set.
3. **Edge fidelity?** Decided (2026-10-18): raw tracker with fixed 1-in-100 in-kernel
   sampling, per-run files; temporarily remove the sampling predicate if a demo needs
   full fidelity (files grow ~100×).

---

## Progress log

### 2026-10-18 — Phase 0 + 1 implemented (awaiting sudo smoke test)

- [x] 0.1 `data/edges.jsonl` (3.18 GB) removed — disk back to ~5.8 GB free
- [x] 0.2 git repo initialised; artefacts (`data/`, `__pycache__/`, `.venv/`) ignored
- [x] 0.3 fio stressors bounded (512M / 1G, `--unlink=1`, `--filename=data/scratch_*`)
- [x] 0.4 `kernel.sched_schedstats` handled by `testbed/preflight.sh`
- [x] 0.5 `changes.md` refreshed (data.txt left as a historical review)
- [x] 1.1/1.2 per-run edge files wired into `parentProcess.py` and `rcaDaemon.py`
      (aggregation attempt replaced by sampled raw edges — see below)
- [x] 1.3 stressor start/end + label/family in meta and `data/ground_truth.jsonl`
- [x] 1.4 `probeExtractor.py` window stamps from the bpftrace clock
- [x] 1.5 futex wait-op filter in `probes.bt`
- [x] 1.6 `procExtractor.py` robustness (missing zones, counter resets)
- [x] Analyzer: per-run windows, per-run edge loading, weighted graphs
- [ ] 1.7 per-second rate features (deferred to Phase 2 with the detector retrain)

Unit-tested without root: `ProbeStreamParser` (markers), analyzer per-run raw edge path
(synthetic `data/edges_*` + `meta_*`), online LLM client (local stub server).

### 2026-10-18 (later) — simplified edge collection + online LLM

`for` loops and string-concat map keys are unreliable on bpftrace **0.20.2**
(the installed version; `next/bpftraceReference.md` documents a newer
release). Decision: drop in-kernel aggregation entirely and use the proven
raw tracker:

- `wakeupTracker.bt` — same raw JSONL, plus `/rand % 100 == 0/` (1-in-100)
  sampling → a few MB per run instead of ~530 MB/min
- per-run `data/edges_<run_id>.jsonl`; exact alignment via
  `boot_time_epoch` from `data/meta_<run_id>.json`
- `analyzeCausalGraphs.py` reads the per-run file directly — boot-time
  inference, aggregated `edgeData` loading, and the global `edges.jsonl`
  fallback were all removed
- deleted `wakeupAggregator.bt` / `wakeupExtractor.py` (recoverable from git)

`ragExplainer.py` gained an optional online LLM review (OpenAI-compatible
`/chat/completions`, `RCA_LLM_API_KEY`; no local install). Verified against a
local stub server.

Pending sudo smoke test (bpftrace cannot run as a normal user):

```bash
sudo bash testbed/preflight.sh --clean
sudo python3 parentProcess.py default
ls -la data/edges_default.jsonl data/ground_truth.jsonl
```

Next: Phase 2 — collect `default` baselines, persist the OneClassSVM
(`--train` / `--score`), fix label hygiene, re-run benchmarks.

