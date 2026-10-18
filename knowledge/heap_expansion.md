# Continuous Heap Expansion (brk/sbrk Syscalls)

## Symptoms & Metrics
- High rate of memory allocations exercising `brk()` and `sbrk()` syscalls.
- Expanding Virtual Memory Area (VMA) structures and high kernel VMA spinlock activity.
- Moderate major and minor page faults without full physical RAM exhaustion.

## Underlying Mechanism
Applications making rapid programmatic allocations via `brk` continuously modify process heap boundaries, requiring kernel memory management subsystem locks and page table updates for each allocation step.

## Diagnostic Clues
- `stress-ng-brk` or memory-allocating application as primary runtime consumer.
- Elevated minor page faults and VMA expansion latency.

## Remediation
- Use memory pool allocators (e.g. jemalloc, tcmalloc) that pre-allocate large memory chunks with `mmap` rather than frequent `brk` expansions.
- Optimize application memory allocation patterns.
