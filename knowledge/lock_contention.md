# Lock Contention & Synchronization Bottlenecks

## Symptoms & Metrics
- High `futex_wait_time` and `futex_wait_count` from `sys_enter_futex` / `sys_exit_futex` tracepoints.
- Elevated `locktime` in kernel spinlock/mutex contention tracepoints (`lock:contention_begin` / `lock:contention_end`).
- Causal paths show deep chains of worker threads waiting on a single lock owner or thread manager.
- Thread starvation with modest CPU utilization.

## Underlying Mechanism
Multiple threads concurrently attempt to acquire shared mutual exclusion locks (userspace pthread mutexes, rwlocks, futexes, or kernel VFS inode/dentry spinlocks). Threads sleep waiting for the lock owner to release and wake them.

## Diagnostic Clues
- `futex_wait_time` in nanoseconds is high (>1e8 nsecs) alongside thousands of `futex_wait_count` wakeups.
- In userspace lock contention (`lockStressor1`), threads repeatedly unblock each other in circular or fan-in patterns.
- In kernel lock contention (`lockStressor2` / dentry / inode), `locktime` metric spikes due to contention on VFS and directory cache structures.

## Remediation
- Use fine-grained locking or lock-free data structures (e.g. RCU, atomic operations).
- Shard critical sections to reduce lock hold times.
- Avoid excessive file creation/deletion in single directories to relieve VFS dcache spinlock bottlenecks.
