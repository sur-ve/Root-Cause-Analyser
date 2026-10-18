#!/usr/bin/env python3
"""
rcaDaemon.py — Continuous background RCA monitor.

Runs the collection stack continuously (no stressor) and periodically:
  1. Featurises recent telemetry
  2. Scores with OneClassSVM (production anomaly detector)
  3. Builds causal graphs for flagged windows
  4. Generates an RCA report

Usage (must be run as root or with CAP_BPF / CAP_SYS_ADMIN):
    sudo python3 rcaDaemon.py [--interval 300] [--run-id rolling]

Options:
    --interval N   Analyse cycle interval in seconds (default: 300)
    --run-id ID    Base run_id for rotating data files (default: rolling)
    --no-collect   Skip bpftrace/procfs collection (analyse existing data only)

Wakeup edges are written per-run (data/edges_<run_id>.jsonl), sampled
1-in-100 in-kernel by wakeupTracker.bt, so no global edges rotation is needed.
"""

import os
import sys
import json
import time
import signal
import shlex
import argparse
import subprocess
import datetime
import importlib.util


def import_module_from_file(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_running = True

def _handle_signal(signum, frame):
    global _running
    print(f"\n[rcaDaemon] Received signal {signum}, shutting down...")
    _running = False

signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def make_run_id(base: str) -> str:
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    return f"{base}_{ts}"


def start_collectors(run_id: str):
    """Start procExtractor, probeExtractor, and the windup edge extractor in background."""
    os.makedirs(os.path.join(BASE_DIR, 'data'), exist_ok=True)

    # Record boot time metadata for timestamp alignment
    boot_time_epoch = time.time() - time.monotonic()
    meta = {
        'run_id': run_id,
        'label': 'Unlabeled',  # rolling runs carry no ground-truth label
        'boot_time_epoch': boot_time_epoch,
        'start_time': time.time(),
        'stressor_command': None
    }
    for fname in (f'data/meta_{run_id}.json', 'data/meta_latest.json'):
        with open(os.path.join(BASE_DIR, fname), 'w') as f:
            json.dump(meta, f, indent=2)

    proc1 = subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, 'procExtractor.py'), run_id],
        cwd=BASE_DIR
    )
    proc2 = subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, 'probeExtractor.py'), run_id],
        cwd=BASE_DIR
    )
    # Sampled raw wakeup edges (1-in-100 in-kernel) → data/edges_<run_id>.jsonl
    edges_file = open(os.path.join(BASE_DIR, f'data/edges_{run_id}.jsonl'), 'w')
    proc3 = subprocess.Popen(
        ['stdbuf', '-oL', 'bpftrace', '-q', os.path.join(BASE_DIR, 'wakeupTracker.bt')],
        stdout=edges_file,
        text=True,
        cwd=BASE_DIR,
    )

    print(f"[rcaDaemon] Collectors started (run_id={run_id})")
    return proc1, proc2, proc3, edges_file


def stop_collectors(procs, edges_file=None):
    print("[rcaDaemon] Stopping collectors...")
    for p in procs:
        try:
            p.send_signal(signal.SIGINT)
        except (ProcessLookupError, OSError):
            pass
    for p in procs:
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    if edges_file is not None:
        try:
            edges_file.close()
        except Exception:
            pass
    print("[rcaDaemon] Collectors stopped.")


def run_analysis_cycle(run_id: str):
    """Run featureExtract → anomalyDetect → causalGraph → ragExplain."""
    print(f"\n[rcaDaemon] Starting analysis cycle for run_id={run_id}...")

    stages = [
        ('featureExtractor', os.path.join(BASE_DIR, 'featureExtractor.py')),
        ('anomalyDetection', os.path.join(BASE_DIR, 'anomalyDetection.py')),
        ('analyzeCausalGraphs', os.path.join(BASE_DIR, 'analyzeCausalGraphs.py')),
        ('ragExplainer', os.path.join(BASE_DIR, 'ragExplainer.py')),
    ]

    for stage_name, script_path in stages:
        if not os.path.exists(script_path):
            print(f"[rcaDaemon] Warning: {script_path} not found, skipping {stage_name}.")
            continue
        try:
            result = subprocess.run(
                [sys.executable, script_path, run_id],
                cwd=BASE_DIR,
                timeout=120,
                check=False
            )
            if result.returncode != 0:
                print(f"[rcaDaemon] {stage_name} failed with exit code {result.returncode}.")
        except subprocess.TimeoutExpired:
            print(f"[rcaDaemon] {stage_name} timed out.")

    # Print / log the latest RCA report summary
    report_file = os.path.join(BASE_DIR, 'data/rca_report.txt')
    if os.path.exists(report_file):
        with open(report_file, 'r') as rf:
            content = rf.read().strip()
        if content:
            print(f"\n{'='*60}\n[rcaDaemon] Latest RCA Report:\n{content}\n{'='*60}\n")

    print(f"[rcaDaemon] Analysis cycle complete.")


def main():
    parser = argparse.ArgumentParser(description='rcaDaemon — continuous background RCA monitor')
    parser.add_argument('--interval', type=int, default=300, help='Analysis interval in seconds (default: 300)')
    parser.add_argument('--run-id', type=str, default='rolling', help='Base run_id for data files (default: rolling)')
    parser.add_argument('--no-collect', action='store_true', help='Skip bpftrace/procfs collection; analyse existing data only')
    args = parser.parse_args()

    os.makedirs(os.path.join(BASE_DIR, 'data'), exist_ok=True)

    run_id = make_run_id(args.run_id)
    collector_procs = []
    edges_file = None

    if not args.no_collect:
        if os.geteuid() != 0:
            print("[rcaDaemon] ERROR: Must run as root (sudo) to start bpftrace/procExtractor collectors.")
            sys.exit(1)
        proc1, proc2, proc3, edges_file = start_collectors(run_id)
        collector_procs = [proc1, proc2, proc3]

    print(f"[rcaDaemon] Running. Analysis interval = {args.interval}s. Ctrl+C to stop.")
    last_analysis = 0.0

    try:
        while _running:
            now = time.time()
            if now - last_analysis >= args.interval:
                # NOTE: edges are written per-run (data/edges_<run_id>.jsonl),
                # so the old global edges.jsonl rotation is no longer needed.
                run_analysis_cycle(run_id)
                last_analysis = now

            # Check if collectors are still alive
            if collector_procs:
                for p in collector_procs:
                    if p.poll() is not None:
                        print(f"[rcaDaemon] Collector process exited unexpectedly (pid {p.pid}). Attempting restart...")
                        # For simplicity, stop everything and re-start
                        stop_collectors(collector_procs, edges_file)
                        run_id = make_run_id(args.run_id)
                        proc1, proc2, proc3, edges_file = start_collectors(run_id)
                        collector_procs = [proc1, proc2, proc3]
                        break

            time.sleep(5)

    finally:
        if collector_procs:
            stop_collectors(collector_procs, edges_file)
        print("[rcaDaemon] Exited cleanly.")

if __name__ == '__main__':
    main()
