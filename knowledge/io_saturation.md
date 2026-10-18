# I/O Saturation & Storage Bottlenecks

## Symptoms & Metrics
- High `sector_count` on block layer completion tracepoints (`block:block_rq_complete`).
- High `iowait_time` on scheduler wait tracepoints (`sched:sched_stat_iowait`).
- Elevated PSI I/O pressure metrics (`io.some` and `io.full` avg10 / avg60).
- High `vfsreadtime` indicating prolonged VFS read system call duration.
- Causal paths show application processes unblocked by kernel worker threads (`kworker`, `jbd2`) or storage engines (e.g. `fio`, database writers).

## Underlying Mechanism
Storage device bandwidth or IOPS capacity is saturated. In small random writes (`ioStressor1`), queue depth and latency explode with synchronous `fsync` barriers. In large sequential writes (`ioStressor2`), throughput is saturated and dirty page writeback saturates the block layer.

## Diagnostic Clues
- Spikes in `sector_count` (>10,000 sectors per 3s window) indicate sequential throughput saturation.
- Spikes in `iowait_time` and `io_some_avg10` with lower `sector_count` indicate latency-bound random I/O bottleneck.
- Journaling thread `jbd2` or storage engine appears as a frequent unblocker.

## Remediation
- Use asynchronous non-blocking I/O (`io_uring`, `libaio`) instead of synchronous fsync calls.
- Tune dirty page flush ratios (`vm.dirty_background_ratio`, `vm.dirty_ratio`).
- Migrate high-IOPS workloads to faster NVMe storage or distribute across multiple mount points.
