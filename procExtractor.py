import time
import re
import json
import sys

import os

os.makedirs('data', exist_ok=True)
run_id = sys.argv[1] if len(sys.argv) > 1 else 'default'
writeFile = f'data/procfsData_{run_id}.jsonl'
# compiling vmstat pattern to extract the required metrics from /proc/vmstat
pattern = re.compile(r'(allocstall_.*|pgmajfault|pswpin|pswpout|oom_kill) (\d+)')

# to read PSI metrics and convert them to a dictionary
def psiLabeler(data: str) -> dict:
    opDict = {}
    lines = data.strip().split('\n')
    for line in lines:
        psiVal = {}
        words = line.strip().split(' ')
        key = words[0]
        for i in range(1, len(words)):
            p1, p2 = words[i].split('=')
            psiVal[p1] = float(p2)
        opDict[key] = psiVal
    return opDict

# to read the vmstat metrics and convert them to a dictionary
def vmstatRead() -> dict:
    vmDict = {}
    with open('/proc/vmstat', 'r') as f:
        lines = f.read().split('\n')
        for line in lines:
            match = pattern.search(line)
            if match:
                var = match.group(1).strip()
                val = int(match.group(2).strip())
                vmDict[var] = val
    # aggregate zone-specific allocation stalls; tolerate kernels without some zones
    vmDict['allocstall'] = sum(
        vmDict.get(k, 0) for k in
        ('allocstall_dma', 'allocstall_dma32', 'allocstall_normal', 'allocstall_movable')
    )
    return vmDict


def counterDelta(cur: dict, prev: dict, key: str) -> int:
    """Delta of a monotonically increasing counter; 0 if absent, clamped at 0 on reset."""
    return max(0, cur.get(key, 0) - prev.get(key, 0))

def readPSI(path: str) -> str:
    with open(path, 'r') as f:
        return f.read()

print("Reading procfs data")
try:
    prevValues = vmstatRead() # primer read so as to capture delta values accurately
    toJSON = {}

    # clear the output file before writing to it
    with open(writeFile, 'w') as f:
        pass
    
    while True:
        # only collecting 'some' metrics for cpu_stat
        cpu_stat = readPSI('/proc/pressure/cpu').split('\n')[0]
        mem_stat = readPSI('/proc/pressure/memory')
        io_stat = readPSI('/proc/pressure/io')
        vmDict = vmstatRead()

        # adding psi data to dictionary
        toJSON['cpu'] = psiLabeler(cpu_stat)
        toJSON['mem'] = psiLabeler(mem_stat)
        toJSON['io'] = psiLabeler(io_stat)

        # adding vmstat deltas to dictionary
        toJSON['allocstall_count'] = counterDelta(vmDict, prevValues, 'allocstall')
        toJSON['major_fault_count'] = counterDelta(vmDict, prevValues, 'pgmajfault')
        toJSON['swap_in_count'] = counterDelta(vmDict, prevValues, 'pswpin')
        toJSON['swap_out_count'] = counterDelta(vmDict, prevValues, 'pswpout')
        toJSON['oom_kill_count'] = counterDelta(vmDict, prevValues, 'oom_kill')
        prevValues = vmDict.copy()

        # write to JSON file along with timestamp
        with open(writeFile, 'a') as f:
            timestamp = time.time()
            toJSON['timestamp'] = timestamp
            json.dump(toJSON, f)
            f.write('\n')

        # perform read every 1 second
        time.sleep(1)
except KeyboardInterrupt:
    print("procfs extraction done!")
    pass
