# Changes / status

Tracking doc — the full plan lives in [PLAN.md](PLAN.md).

## Done (Phase 0 + 1, 2026-10-18) — sudo-validated

* [x] **probeExtractor timestamps** — windows now use bpftrace-clock markers
      (`BEGIN_NS` / `WINDOW_END_NS` from `probes.bt`) converted with
      `boot_time_epoch` from `data/meta_<run_id>.json`; the old
      "fin_timestamp - 3.0" guess is gone.
* [x] **Bounded edge collection (simplified)** — `wakeupTracker.bt` emits raw
      JSONL with **1-in-100 in-kernel sampling** (`/rand % 100 == 0/`) into
      per-run `data/edges_<run_id>.jsonl` (1.6 MB for a 70 s idle run). The
      `wakeupAggregator.bt` approach was abandoned: bpftrace 0.20.2 has no
      `for` loops and rejected string-concat map keys.
* [x] **Futex probe op-filter** (only waiting ops: 0/6/9/11) so lock metrics
      are meaningful instead of counting every futex syscall.
* [x] **procExtractor robustness** — tolerates kernels without all
      `allocstall_*` zones; counter deltas clamped at 0 on resets.
* [x] **Ground truth** — `parentProcess.py` records stressor
      `start_time` / `end_time` and label/family in meta, and appends to
      `data/ground_truth.jsonl` (used for evaluation + MTTR).
* [x] **Safer fio stressors** — 512M / 1G files with `--unlink=1` and
      `--filename=data/scratch_*.dat`.
* [x] **`testbed/preflight.sh`** — smoke tests passed under sudo for
      `probes.bt` and `wakeupTracker.bt`.
* [x] **Causal graphs** — per-run anomaly windows; edges read directly from
      `data/edges_<run_id>.jsonl` and aligned with `meta_<run_id>.json`.
* [x] **Online LLM (optional)** — `ragExplainer.py` calls any OpenAI-compatible
      `/chat/completions` API when `RCA_LLM_API_KEY` is set; no local install.
* [x] **End-to-end run validated** — feature → detect → graph → report in ~6 s;
      graph has 410 edges / 236 nodes from the sampled tracker.

## Done (Phase 2, in progress)

* [x] **Label hygiene** — `featureExtractor.py` resolves labels meta-first and
      falls back to `Unlabeled` (never `Custom (...)`); `ragExplainer.py` no
      longer reads `labeled_causes` at all (evaluation-only; verified with a
      synthetic contradictory label).
* [x] **Persisted detector** — `anomalyDetection.py --train` / `--score`
      (scaler + OneClassSVM + feature list in `data/model/ocsvm.joblib`);
      `--score` auto-trains when no artifact exists. `default1..default8` are
      all labeled `Normal`.
* [ ] **Baseline runs** — 1 of ~6 collected (5120 Normal rows, FPR 6.8 %);
      `default1..default5` in progress.
* [ ] **Fault matrix** — recollect with `testbed/run_all.sh`.
* [ ] **Benchmarks (2.5)** — re-run `detectorBenchmark.py` /
      `overallBenchmark.py` after the matrix; retune `nu`; update README (2.6).

## Done (Phase 4.1–4.3 scripts, dry-run tested)

* [x] `testbed/run_fault.sh` — root/disk/RAM guards, cgroup-wrapped memory
      stressors (memStressor2 = host-safe cgroup OOM kill), post-run
      verification (files, meta, ground truth, no collector leftovers).
* [x] `testbed/run_all.sh` — least→most destructive order, resume support.

## Next

* `sudo bash testbed/run_all.sh` (after baselines) → retrain → benchmarks.
* Phase 3 daemon rewrite: `rcaDaemon.py` still passes `run_id` where stages
  expect file paths, so background analysis never runs.
* `kernel.sched_schedstats=1` keeps `rq_time` / `iowait_time` alive
  (preflight sets it per boot).
