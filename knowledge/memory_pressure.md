# Memory Pressure & Thrashing

## Symptoms & Metrics
- High PSI Memory pressure (`mem.some` and `mem.full` avg10 / avg60 elevated above 0).
- Elevated `swap_in_count` (`pswpin`) and `swap_out_count` (`pswpout`) in vmstat counters.
- Elevated `major_fault_count` (`pgmajfault`) and memory direct allocation stall counts (`allocstall`).
- Candidate root causes show high-memory allocation processes (`stress-ng-vm`, database buffers, browser renderer heaps).

## Underlying Mechanism
Active memory working set exceeds available physical RAM. The kernel kswapd and direct reclaim pathways are forced to scan page lists, evict inactive file-backed pages, and write anonymous pages to disk swap. Processes stall on disk I/O when accessing swapped pages.

## Diagnostic Clues
- Non-zero `allocstall` counters indicating threads stalled in direct reclaim while waiting for free page frames.
- Spikes in `major_fault_count` accompanied by `swap_in_count`.
- Causal paths often show system daemons and apps unblocked by kernel reclaimer threads or memory allocators.

## Remediation
- Increase system RAM or adjust swappiness (`vm.swappiness`).
- Restrict process memory consumption via systemd or cgroups (`memory.max`, `memory.high`).
- Investigate memory leaks and optimize caching layers.
