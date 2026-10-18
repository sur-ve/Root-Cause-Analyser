# Changes / status

Tracking doc — the full plan lives in [PLAN.md](PLAN.md).

## Done (Phase 0 + 1, 2026-10-18)

* [x] **probeExtractor timestamps** — windows now use bpftrace-clock markers
      (`BEGIN_NS` / `WINDOW_END_NS` from `probes.bt`) converted with
      `boot_time_epoch` from `data/meta_<run_id>.json`; the old
      "fin_timestamp - 3.0" guess is gone.
* [x] **Bounded edge collection (simplified)** — `wakeupTracker.bt` emits raw
      JSONL with **1-in-100 in-kernel sampling** (`/rand % 100 == 0/`) into
      per-run `data/edges_<run_id>.jsonl` (a few MB per run, not ~530 MB/min).
      The `wakeupAggregator.bt` approach was abandoned: bpftrace 0.20.2 has no
      `for` loops and rejected string-concat map keys.
* [x] **Futex probe op-filter** (only waiting ops: 0/6/9/11) so lock metrics
      are meaningful instead of counting every futex syscall.
* [x] **procExtractor robustness** — tolerates kernels without all
      `allocstall_*` zones; counter deltas clamped at 0 on resets.
* [x] **Ground truth** — `parentProcess.py` records stressor
      `start_time` / `end_time` and label/family in meta, and appends to
      `data/ground_truth.jsonl` (used for evaluation + MTTR).
* [x] **Safer fio stressors** — 512M / 1G files with `--unlink=1` and
      `--filename=data/scratch_*.dat` (disk was at 97 %).
* [x] **`testbed/preflight.sh`** — environment/safety checks plus bpftrace
      smoke tests for `probes.bt` and `wakeupTracker.bt`.
* [x] **Causal graphs** — per-run anomaly windows; edges read directly from
      `data/edges_<run_id>.jsonl` and aligned with `meta_<run_id>.json`
      (all boot-time inference heuristics removed).
* [x] **Online LLM (optional)** — `ragExplainer.py` calls any OpenAI-compatible
      `/chat/completions` API when `RCA_LLM_API_KEY` is set
      (`RCA_LLM_BASE_URL`, `RCA_LLM_MODEL` optional overrides). No local
      install; the grounded template report is the fallback.

## Next (Phase 2+)

* Collect real `default` baseline runs — the detector currently has **zero
  Normal rows** (trains on faults; recall 3–11 %, benchmark F1 0.12).
* Persisted model (`anomalyDetection.py --train` / `--score`) + label hygiene
  in `featureExtractor.py` / `ragExplainer.py` (never print `Custom (...)` as a cause).
* Daemon rewrite: `rcaDaemon.py` still passes `run_id` where stages expect file
  paths, so background analysis never runs (Phase 3).
* `kernel.sched_schedstats=1` keeps `rq_time` / `iowait_time` alive
  (preflight sets it per boot).
