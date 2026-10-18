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

## Done (Phase 2 complete — 2026-10-18)

* [x] **Baseline runs** — 6 `default` runs collected; **28 789 Normal rows**.
* [x] **Fault matrix** — all 10 stressors recollected (16 runs, 69 682 rows);
      `memStressor2` was a 2 s blip (see OOM fix) and is being recollected.
* [x] **Detector** — retrained on all baselines: Normal FPR 5.2 %;
      `detectorBenchmark.py` OCSVM F1 0.981 / FPR 5.4 % (vs IF 0.826 / LOF 0.916);
      `overallBenchmark.py` held-out F1 0.979 / FPR 5.8 %, 10/10 classes ≈ 1.0.
* [x] **Cause classification (2.5)** — new shared module `diagnosis.py`
      (label-free; tiers: culprit comm → OOM-kill event → `stress-ng-vm` →
      metric signatures → unknown) used by the explainer and both benchmarks;
      old 18 % row-level heuristic deleted. Window-level agreement:
      **99.4 %** over fault-active windows / 88.0 % over all fault windows;
      episode level (17 reports): 10/11 correct (miss = broken OOM run).

## Done (Phase 4.1–4.3 scripts, dry-run tested)

* [x] `testbed/run_fault.sh` — root/disk/RAM guards, cgroup-wrapped memory
      stressors, post-run verification (files, meta, ground truth, no
      collector leftovers).
* [x] `testbed/run_all.sh` — least→most destructive order, resume support.

## In progress / next

* **OOM path** — `probes.bt` now counts `oom_kill_process` /
  `__oom_kill_process` (`oom_kills` metric); guarded `memStressor2` now loops
  2.5G allocations under `MemoryMax=2G / MemorySwapMax=256M` for repeated
  in-cgroup OOM kills. Needs: `sudo bash testbed/preflight.sh` (validates the
  new kprobes) + `sudo bash testbed/run_fault.sh memStressor2`, then rebuild.
* **Phase 3 daemon rewrite** — `rcaDaemon.py` still passes `run_id` where
  stages expect file paths, so background analysis never runs; two-phase
  `--learn`/`--monitor`, incremental scoring, `data/alerts.jsonl`, retention,
  systemd unit.
* **Phase 4** — `testbed/evaluate.py`, `demo_replay.py`, `demo_live.sh`,
  `testbed/README.md`.
* `kernel.sched_schedstats=1` keeps `rq_time` / `iowait_time` alive
  (preflight sets it per boot).
