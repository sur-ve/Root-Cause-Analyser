#!/usr/bin/env python3
"""
wakeupExtractor.py — windowed wakeup-edge collection for the RCA pipeline.

Runs `bpftrace wakeupAggregator.bt` and converts its per-3s window dumps into
compact JSONL records (one per window):

    {"window_start": <epoch>, "window_end": <epoch>,
     "edges": [["wakee_comm", "waker_comm", <count>], ...]}

Usage:
    python3 wakeupExtractor.py <run_id>

Writes:
    data/edgeData_<run_id>.jsonl

Boot-relative bpftrace timestamps are converted with boot_time_epoch from
data/meta_<run_id>.json (written by parentProcess.py / rcaDaemon.py); falls
back to (time.time() - time.monotonic()) when meta is missing.
"""

import json
import os
import signal
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BPF_SCRIPT = os.path.join(BASE_DIR, 'wakeupAggregator.bt')


def load_boot_epoch(run_id: str) -> float:
    """boot_time_epoch for this run (from its meta file) or best-effort estimate."""
    meta_path = os.path.join(BASE_DIR, 'data', f'meta_{run_id}.json')
    try:
        with open(meta_path, 'r') as f:
            return float(json.load(f)['boot_time_epoch'])
    except Exception:
        return time.time() - time.monotonic()


class WindowAccumulator:
    """Turns the aggregator's line protocol into epoch-stamped window records.

    Kept importable and side-effect free so it can be unit-tested without root.
    """

    def __init__(self, boot_epoch: float, write_fn):
        self.boot_epoch = boot_epoch
        self.write_fn = write_fn
        self.begin_ns = None
        self.end_ns = None
        self.prev_end_ns = None
        self.edges = {}  # (wakee, waker) -> count
        self.windows_written = 0

    def _ns_to_epoch(self, ns: int) -> float:
        return self.boot_epoch + (ns / 1e9)

    def handle(self, line: str) -> bool:
        """Process one raw output line. Returns True when a window was written."""
        line = line.strip()
        if not line:
            return False
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            return False
        if not isinstance(rec, dict):
            return False

        marker = rec.get('marker')
        if marker == 'begin':
            self.begin_ns = rec.get('nsecs')
            return False
        if marker == 'window_end':
            self.end_ns = rec.get('nsecs')
            return False
        if marker == 'window_done':
            return self._flush()
        if 'wakee' in rec and 'waker' in rec:
            try:
                key = (str(rec['wakee']), str(rec['waker']))
                self.edges[key] = self.edges.get(key, 0) + int(rec.get('count', 1))
            except (TypeError, ValueError):
                pass
        return False

    def _flush(self) -> bool:
        wrote = False
        start_ns = self.prev_end_ns if self.prev_end_ns is not None else self.begin_ns
        if self.edges and start_ns is not None and self.end_ns is not None:
            self.write_fn({
                'window_start': self._ns_to_epoch(start_ns),
                'window_end': self._ns_to_epoch(self.end_ns),
                'edges': [[wakee, waker, count] for (wakee, waker), count in self.edges.items()],
            })
            wrote = True
            self.windows_written += 1
        # Chain window boundaries even for empty windows
        if self.end_ns is not None:
            self.prev_end_ns = self.end_ns
        self.end_ns = None
        self.edges = {}
        return wrote


def main():
    run_id = sys.argv[1] if len(sys.argv) > 1 else 'default'
    data_dir = os.path.join(BASE_DIR, 'data')
    os.makedirs(data_dir, exist_ok=True)
    out_path = os.path.join(data_dir, f'edgeData_{run_id}.jsonl')

    boot_epoch = load_boot_epoch(run_id)

    # stdbuf -oL keeps bpftrace's stdout line-buffered through the pipe
    proc = subprocess.Popen(
        ['stdbuf', '-oL', 'bpftrace', '-q', BPF_SCRIPT],
        stdout=subprocess.PIPE,
        text=True,
    )

    with open(out_path, 'w') as out:
        def write_record(record):
            out.write(json.dumps(record))
            out.write('\n')
            out.flush()

        acc = WindowAccumulator(boot_epoch, write_record)

        def consume(stream):
            for line in stream:
                acc.handle(line)

        try:
            consume(proc.stdout)
        except KeyboardInterrupt:
            proc.send_signal(signal.SIGINT)
            consume(proc.stdout)

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    print(f"Wakeup edge extraction done! "
          f"({acc.windows_written} windows -> data/edgeData_{run_id}.jsonl)")


if __name__ == '__main__':
    main()
