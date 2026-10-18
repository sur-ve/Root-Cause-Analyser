# cgroup CPU Quota Throttling

## Symptoms & Metrics
- High `rq_time` (runqueue wait) for processes inside the throttled cgroup despite moderate overall system load.
- Process `runtime` is bounded by CFS bandwidth quota (e.g. `CPUQuota=20%`).
- Causal graph displays tasks waiting on timers or kernel CFS bandwidth unthrottle events.

## Underlying Mechanism
The Linux Completely Fair Scheduler (CFS) enforces CPU bandwidth limits using tokens refreshed every period (default 100ms). Once a process exhausts its quota within a period, it is throttled and taken off the CPU until the next quota replenishment.

## Diagnostic Clues
- Check `/sys/fs/cgroup/cpu.stat` or cgroup v2 `nr_throttled` and `throttled_usec`.
- Process shows runqueue latency spikes without 100% host CPU utilization.

## Remediation
- Increase `CPUQuota` or adjust `CPUQuotaPeriodSec`.
- Check if burstable CPU limits are enabled (`cpu.weight`).
