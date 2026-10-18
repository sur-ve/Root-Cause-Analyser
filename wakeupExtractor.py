#!/usr/bin/env python3
"""
wakeupExtractor.py — windowed wakeup-edge collection for the RCA pipeline.

Runs `bpftrace -q -f json wakeupAggregator.bt` and converts its per-3s window
dumps into compact JSONL records (one per window):

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


def extract_ns_marker(rec: dict, prefix: str):
    """Read 'PREFIX <nsecs>' markers emitted via printf in bpftrace JSON mode.

    printf output is wrapped (e.g. {"type": "printf", "data": "..."}), so look
    for the prefix in any string field.
    """
    for value in rec.values():
        if isinstance(value, str) and value.startswith(prefix):
            try:
                return int(value[len(prefix):].strip())
            except ValueError:
                return None
    return None


class WindowAccumulator:
    """Turns wakeupAggregator's JSON lines into epoch-stamped window records.

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

        if rec.get('data') == 'END':
            return self._flush()

        ns = extract_ns_marker(rec, 'WINDOW_END_NS ')
        if ns is not None:
            self.end_ns = ns
            return False
        ns = extract_ns_marker(rec, 'BEGIN_NS ')
        if ns is not None:
            self.begin_ns = ns
            return False

        if rec.get('type') == 'map':
            data = rec.get('data')
            if isinstance(data, dict):
                self._add_edges(data.get('@edges'))
        return False

    def _add_edges(self, edges_map):
        """Accept the dict shape {"wakee|waker": count} (and list shapes defensively)."""
        if isinstance(edges_map, dict):
            for key, value in edges_map.items():
                self._add_edge(str(key), value)
        elif isinstance(edges_map, list):
            for entry in edges_map:
                if not isinstance(entry, dict):
                    continue
                key = entry.get('key')
                if isinstance(key, list) and len(key) == 2:
                    self._add_pair(str(key[0]), str(key[1]), entry.get('value'))
                elif isinstance(key, str):
                    self._add_edge(key, entry.get('value'))

    def _add_edge(self, key: str, value):
        if '|' not in key:
            return
        wakee, waker = key.split('|', 1)
        self._add_pair(wakee, waker, value)

    def _add_pair(self, wakee: str, waker: str, value):
        try:
            count = int(value)
        except (TypeError, ValueError):
            return
        pair = (wakee, waker)
        self.edges[pair] = self.edges.get(pair, 0) + count

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
        ['stdbuf', '-oL', 'bpftrace', '-q', '-f', 'json', BPF_SCRIPT],
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
