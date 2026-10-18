# CPU Competition & Scheduling Starvation

## Symptoms & Metrics
- High process `runtime` and elevated runqueue wait delay (`rq_time` in scheduler tracepoints).
- Elevated PSI CPU pressure metrics (`cpu.some` avg10 / avg60 elevated).
- Large volume of scheduler wakeup transitions between competing threads.
- Candidate root cause in causal graph often appears as compute-heavy tasks (e.g. `stress-ng-cpu`, worker threads) serving as terminal blockers unblocking starved threads or hogging CPU timeslices.

## Underlying Mechanism
Multiple compute-bound tasks saturate CPU runqueues. Lower-priority or interactive threads experience scheduling delays and latency spikes because runqueues are oversubscribed.

## Diagnostic Clues
- Causal paths show interactive or background services waiting on CPU-bound processes.
- High `runtime` per 3-second window (>100ms or 1e8 nsecs).
- Runqueue wait time `rq_time` spikes significantly above baseline.

## Remediation
- Isolate critical workloads onto dedicated CPU cores using `taskset` or `cpuset` cgroups.
- Apply CPU quotas, CFS bandwidth limits, or nice / real-time priorities (`chrt`, `renice`).
- Scale out compute load across more CPU cores or reduce parallel threads.
