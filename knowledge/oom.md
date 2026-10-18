# Out Of Memory (OOM) Killer Invocations

## Symptoms & Metrics
- Non-zero `oom_kill_count` in `/proc/vmstat`.
- High `oom_time` latency recorded on kernel `out_of_memory` tracepoints/kprobes.
- Spikes in `allocstall_count` and extreme PSI memory pressure (`mem.full` approaching 100%).
- Terminal blocker in causal graph corresponds to rapid memory consumers (e.g., `stress-ng-vm`, memory leaks).

## Underlying Mechanism
The kernel cannot satisfy page allocation requests even after direct page reclaim and swapping. The Out-Of-Memory Killer (`out_of_memory()`) is triggered to terminate one or more rogue processes based on `badness()` heuristic scoring to free up memory pages for system survival.

## Diagnostic Clues
- `oom_kill_count` > 0 is definitive proof of OOM events.
- Kernel dmesg / syslog contains "Out of memory: Killed process" logs.
- Memory allocation stall durations (`oom_time`) appear in bpftrace probe metrics.

## Remediation
- Identify and terminate or constrain the memory-hogging process.
- Configure cgroup memory limits with `memory.oom.group` to kill isolated task hierarchies instead of arbitrary system processes.
- Adjust `oom_score_adj` to protect critical system services from being killed.
- Increase system RAM or swap space.
