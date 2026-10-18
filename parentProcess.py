"""
    Run using cli by adding arguments in the format
    -> sudo python3 parentProcess.py <stressor_name> <stressor_command>
    OR FOR DEFAULT RUN
    -> sudo python3 parentProcess.py

    Edge collection:
      default            → wakeupExtractor.py (aggregated per-3s windows,
                           data/edgeData_<run_id>.jsonl; low disk footprint)
      RCA_RAW_EDGES=1    → raw wakeupTracker.bt lines
                           (data/edges_<run_id>.jsonl; short experiments only)
"""

import json
import os
import subprocess
import time
import signal
import sys
import shlex

from labels import label_for_run, family_for_label

"""
# Stressor commands:
1. cpuStressor1 'stress-ng --cpu $(nproc) --timeout 60s' # pure competing load
2. cpuStressor2 'systemd-run --scope -p CPUQuota=20% stress-ng --cpu 2 --timeout 60s' # creates cgroups using systemd to artificially throttle the process

# systemd-run --scope creates a transient cgroup specifically for this execution. Once completed, cgroup is automatically cleaned up by system 
3. cpuStressor3 'stress-ng --cpu 1 --cpu-load 50 --timeout 60s' # baseline active conditions for CPU

4. memoryStressor1 'stress-ng --vm 2 --vm-bytes 70% --vm-keep --timeout 60s' # memory swapping by using a percentage that fits in RAM and keeps the memory dirtied forcing the kernel to age pages and swap without invoking OOM killer
5. memoryStressor2 'stress-ng --vm 1 --vm-bytes 150% --timeout 60s' # stressing the virtual memory by allocating 150% of availale memory and rapid triggering of oom_kill_process tracepoint
6. memoryStressor3 'stress-ng --brk 2 --timeout 60s' # continuous data expansion on heap, exercising different kernel paths(brk syscalls and VMA expansions) than standard mmap calls.

7. lockContention1 'stress-ng --futex 4 --timeout 60s' # generates massive userspace contention, triggering futex_wait, futex_wake events
8. lockContention2 'stress-ng --dentry 4 --timeout 60s' # for kernel-internal lock contention, aggressively creates and deletes files, thrahsing the VFS and causing genuine spinlock contention on dcache and inode locks

# fio syntax -> name = ASCII name of job used to override name printed by fio for job
# ioengine = definens how job issues i/o to the file [sync = position i/o location] [libaio = ansyc i/o. engine specific options]
# rw = readwrite [randwrite = random writes, write = seq writes]
# bs = blocksize is bytes for i/o units
# size = total size of file i/o for each thread of job
# iodepth = number of io units to keep in flight against the file
# fsync = issue fsync of dirty data every number of blocks given
# group_reporting = display stats of multiple jobs per job
# filename = to share files between threads in a job
# runtime = time for job completion, time_based = strictly follows runtime if 1, else job completed once size is filled, completed before runtime if possible.
9. ioContention1 'fio --name=randwrite --ioengine=libaio --rw=randwrite --bs=4k --size=1G --numjobs=1 --iodepth=16 --runtime=60 --time_based=1 --unlink=1'  # latency heavy, small random writes. Forces fsync after every small 4k block, guarantees high latency, keeping block layer and storage queues occupied.
10. ioContention2 'fio --name=seqwrite --ioengine=libaio --rw=write --bs=1M --size=5G --numjobs=1 --iodepth=16 --runtime=60 --time_based=1 --unlink=1' # Saturating bandwidth with large sequential writes which will drastically increase sector_count in block tracepoints

# mutliple concurrent jobs writing to the exact file creates a bottleneck on the VFS inode lock before hitting block layer. Secondary lock contention dataset to dentry test
# ioContention3 'fio --name=shared --ioengine=sync --rw=write --bs=4k --size=1G --numjobs=4 --group_reporting --filename=test.dat' 

"""

STRESSORS = {
    'cpuStressor1': 'stress-ng --cpu 2 --timeout 60s',
    'cpuStressor2': 'systemd-run --scope -p CPUQuota=20% stress-ng --cpu 2 --timeout 60s',
    'cpuStressor3': 'stress-ng --cpu 1 --cpu-load 50 --timeout 60s',
    'memStressor1': 'stress-ng --vm 2 --vm-bytes 70% --vm-keep --timeout 60s',
    'memStressor2': 'stress-ng --vm 1 --vm-bytes 150% --timeout 60s',
    'memStressor3': 'stress-ng --brk 2 --timeout 60s',
    'lockStressor1': 'stress-ng --futex 4 --timeout 60s',
    'lockStressor2': 'stress-ng --dentry 4 --timeout 60s',
    'ioStressor1': 'fio --name=randwrite --ioengine=libaio --rw=randwrite --bs=4k --size=512M --numjobs=1 --iodepth=16 --runtime=60 --time_based=1 --unlink=1 --filename=data/scratch_io1.dat',
    'ioStressor2': 'fio --name=seqwrite --ioengine=libaio --rw=write --bs=1M --size=1G --numjobs=1 --iodepth=16 --runtime=60 --time_based=1 --unlink=1 --filename=data/scratch_io2.dat',
    'default': 'sleep 60', # do nothing / normal
}


def __main__():
    run_id = sys.argv[1] if len(sys.argv) > 1 else 'default'
    if len(sys.argv) > 2:
        stressor_command = sys.argv[2]
    else:
        stressor_command = STRESSORS.get(run_id, 'sleep 60')

    # Record run and boot time metadata so causal graphs can align timestamps accurately
    boot_time_epoch = time.time() - time.monotonic()
    label = label_for_run(run_id)
    family = family_for_label(label)
    meta_path = f'data/meta_{run_id}.json'
    meta_latest_path = 'data/meta_latest.json'
    meta = {
        'run_id': run_id,
        'label': label,
        'family': family,
        'boot_time_epoch': boot_time_epoch,
        'start_time': time.time(),
        'stressor_command': stressor_command,
        'stressor_start_time': None,
        'stressor_end_time': None,
    }
    os.makedirs('data', exist_ok=True)

    try:
        with open(meta_path, 'w') as mf:
            json.dump(meta, mf, indent=2)
        with open(meta_latest_path, 'w') as mf:
            json.dump(meta, mf, indent=2)
    except Exception as e:
        print(f"Warning: could not write run metadata: {e}")

    # start the two extractors and the edge collector
    proc1 = subprocess.Popen([sys.executable, 'procExtractor.py', run_id])
    proc2 = subprocess.Popen([sys.executable, 'probeExtractor.py', run_id])

    edges_file = None
    if os.environ.get('RCA_RAW_EDGES', '') == '1':
        # Raw per-wakeup lines — short experiments only (hundreds of MB/min under load)
        edges_file = open(f'data/edges_{run_id}.jsonl', 'w')
        proc4 = subprocess.Popen(
            ['stdbuf', '-oL', 'bpftrace', '-q', 'wakeupTracker.bt'],
            stdout=edges_file,
            text=True,
        )
    else:
        # Aggregated per-window edges (default; bounded disk usage)
        proc4 = subprocess.Popen([sys.executable, 'wakeupExtractor.py', run_id])

    # start the stressor (shlex is used to lexically split the shell commands)
    stressor_parts = shlex.split(stressor_command)
    if not stressor_parts:
        print("[!] Empty stressor command — falling back to 'sleep 60'")
        stressor_parts = ['sleep', '60']
    stressor_start = time.time()
    proc3 = subprocess.Popen(stressor_parts)

    stressor_end = None
    try:
        while True:
            # if any of the processes has already terminated stop execution of the main process
            if proc1.poll() is not None or proc2.poll() is not None or proc4.poll() is not None:
                break
            if proc3.poll() is not None: # if stressor process is done(not None if done(int))
                stressor_end = time.time()
                time.sleep(10) # let polling go on for another 10 seconds to record cooldown after stressor phase
                for p in (proc1, proc2, proc4):
                    try:
                        p.send_signal(signal.SIGINT)
                    except ProcessLookupError:
                        pass
                break
            # check every 1 second
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nReceived KeyboardInterrupt, stopping all child processes...")
        stressor_end = time.time()
        for p in (proc1, proc2, proc3, proc4):
            try:
                p.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass

    finally:
        # wait for all child processes to terminate
        for p in (proc1, proc2, proc3, proc4):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        if edges_file is not None:
            edges_file.close()

        # Persist stressor start/end and append ground truth (evaluation + MTTR)
        meta['stressor_start_time'] = stressor_start
        meta['stressor_end_time'] = stressor_end if stressor_end is not None else time.time()
        try:
            with open(meta_path, 'w') as mf:
                json.dump(meta, mf, indent=2)
            with open(meta_latest_path, 'w') as mf:
                json.dump(meta, mf, indent=2)
            with open('data/ground_truth.jsonl', 'a') as gf:
                json.dump({
                    'run_id': run_id,
                    'fault': run_id,
                    'label': label,
                    'family': family,
                    'stressor_command': stressor_command,
                    'stressor_start_time': meta['stressor_start_time'],
                    'stressor_end_time': meta['stressor_end_time'],
                    'boot_time_epoch': boot_time_epoch,
                }, gf)
                gf.write('\n')
        except Exception as e:
            print(f"Warning: could not write ground truth: {e}")

        print("All processes terminated\n")

if __name__ == '__main__':
    __main__()
