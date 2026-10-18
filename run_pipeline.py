#!/usr/bin/env python3
"""
run_pipeline.py — One-command end-to-end RCA pipeline runner.

Usage:
    python3 run_pipeline.py              # run all stages on existing data
    python3 run_pipeline.py --help       # show usage

Stages:
    1. featureExtractor.py   → data/combined_features.csv
    2. anomalyDetection.py   → data/scored_features.csv
    3. analyzeCausalGraphs.py → data/causal_summaries.json + causal_graphs.json
    4. ragExplainer.py        → data/rca_report.txt + rca_report.json

Pre-requisites:
    Data collection must already be done:
        sudo python3 parentProcess.py default
        sudo python3 parentProcess.py cpuStressor1
        ...
    Or run autoExtractor.sh to collect all stressor classes.
"""

import sys
import os
import subprocess
import time

PYTHON = sys.executable
STAGES = [
    ('Feature Extraction', 'featureExtractor.py'),
    ('Anomaly Detection',  'anomalyDetection.py'),
    ('Causal Graph Analysis', 'analyzeCausalGraphs.py'),
    ('RAG Explainer',      'ragExplainer.py'),
]

def run_stage(name: str, script: str) -> bool:
    print(f"\n{'='*60}")
    print(f"  STAGE: {name}")
    print(f"  Script: {script}")
    print(f"{'='*60}")
    t0 = time.time()
    result = subprocess.run([PYTHON, script], check=False)
    elapsed = time.time() - t0
    if result.returncode == 0:
        print(f"\n[+] {name} completed in {elapsed:.1f}s")
        return True
    else:
        print(f"\n[-] {name} FAILED (exit code {result.returncode})")
        return False

def main():
    if '--help' in sys.argv or '-h' in sys.argv:
        print(__doc__)
        return

    # Ensure we're in the project root
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # Guard: data directory must exist
    if not os.path.isdir('data'):
        print("[-] 'data/' directory not found. Run collection first:")
        print("    sudo python3 parentProcess.py default")
        sys.exit(1)

    print("\n" + "="*60)
    print("       ROOT CAUSE ANALYSER — PIPELINE RUN")
    print("="*60)
    start = time.time()

    failed = []
    for name, script in STAGES:
        ok = run_stage(name, script)
        if not ok:
            failed.append(name)
            # Don't abort — later stages may partially work with prior outputs

    total = time.time() - start
    print(f"\n{'='*60}")
    if not failed:
        print(f"  ALL STAGES COMPLETED SUCCESSFULLY in {total:.1f}s")
        print(f"  Report: data/rca_report.txt")
        print(f"  JSON:   data/rca_report.json")
    else:
        print(f"  Completed in {total:.1f}s with failures:")
        for f in failed:
            print(f"    - {f}")
    print(f"{'='*60}\n")

if __name__ == '__main__':
    main()
