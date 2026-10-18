#!/usr/bin/env python3
"""testbed/demo_replay.py — deterministic offline replay of the recorded corpus.

Runs the full pipeline (feature extract → detect → graph → explain) on the
already-collected labeled corpus without requiring root access or running
collectors. Prints a per-fault results table and one full report excerpt.

Usage (from the project root, no root needed):
    python3 testbed/demo_replay.py [--runs cpuStressor1,memStressor2,...]
                                   [--no-graph]   # skip graph stage (fast)
                                   [--quiet]

The demo runs the label-free diagnosis path (metrics + graph + TF-IDF
retrieval). Ground-truth labels are used only for the evaluation table at the
end (Plan 4.5 / 4.7).
"""

import argparse
import json
import os
import sys
import time

import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import featureExtractor
import anomalyDetection
import analyzeCausalGraphs
import ragExplainer
import diagnosis
from labels import ROOT_CAUSES, FAULT_FAMILIES

SCORED_FILE = os.path.join(BASE_DIR, 'data', 'scored_features.csv')
FEATURES_FILE = os.path.join(BASE_DIR, 'data', 'combined_features.csv')
MODEL_FILE = os.path.join(BASE_DIR, 'data', 'model', 'ocsvm.joblib')
CAUSAL_SUMMARIES_FILE = os.path.join(BASE_DIR, 'data', 'causal_summaries.json')

# Ordered least → most destructive (same as run_all.sh)
DEFAULT_FAULT_RUNS = [
    'lockStressor1', 'lockStressor2',
    'cpuStressor1', 'cpuStressor2', 'cpuStressor3',
    'memStressor1', 'memStressor2', 'memStressor3',
    'ioStressor1', 'ioStressor2',
]

NORMAL_RUNS = ['default', 'default1', 'default2', 'default3', 'default4', 'default5']


# --------------------------------------------------------------------------
# pipeline stages
# --------------------------------------------------------------------------

def stage_features(run_ids: list, quiet: bool) -> pd.DataFrame:
    """Load combined_features.csv (already built) or merge on the fly."""
    if os.path.exists(FEATURES_FILE):
        if not quiet:
            print("[1/4] Loading features from data/combined_features.csv ...")
        df = pd.read_csv(FEATURES_FILE)
        # Filter to requested runs if specified
        if 'run_id' in df.columns:
            df = df[df['run_id'].isin(run_ids)]
        return df

    # Fallback: merge on the fly
    if not quiet:
        print("[1/4] Merging per-run probe/procfs files ...")
    dfs = []
    for run_id in run_ids:
        d = featureExtractor.merge_pair(run_id)
        if d is not None and not d.empty:
            dfs.append(d)
    if not dfs:
        print("[-] No feature data — run the full collection first.")
        sys.exit(1)
    return pd.concat(dfs, ignore_index=True)


def stage_score(df: pd.DataFrame, model_file: str, quiet: bool) -> pd.DataFrame:
    """Score features with the persisted model."""
    if not quiet:
        print("[2/4] Scoring with persisted model ...")
    artifact = anomalyDetection.load_artifact(model_file)
    if artifact is None:
        print(f"[-] No model at {model_file} — run: python3 anomalyDetection.py --train")
        sys.exit(1)
    feature_cols = artifact['feature_cols']
    X = pd.DataFrame(0.0, index=df.index, columns=feature_cols)
    for col in feature_cols:
        if col in df.columns:
            X[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
    X_scaled = artifact['scaler'].transform(X)
    df = df.copy()
    df['anomaly_score'] = artifact['model'].decision_function(X_scaled)
    df['predicted_anomaly'] = artifact['model'].predict(X_scaled) == -1
    if not quiet:
        flagged = int(df['predicted_anomaly'].sum())
        total = len(df)
        print(f"    scored {total} rows; {flagged} flagged ({flagged/total*100:.1f}%)")
    return df


def stage_graph(df: pd.DataFrame, run_ids: list, quiet: bool) -> list:
    """Build causal graphs for anomalous windows. Returns summaries list."""
    if not quiet:
        print("[3/4] Building causal graphs ...")
    # Use the batch pipeline's load_anomaly_windows (per-run grouping)
    # Write a temp scored CSV so analyzeCausalGraphs can read it
    tmp = os.path.join(BASE_DIR, 'data', '_demo_scored.csv')
    df.to_csv(tmp, index=False)
    anomaly_windows, _ = analyzeCausalGraphs.load_anomaly_windows(tmp, padding_sec=1.0)
    os.remove(tmp)

    summaries = []
    for win in anomaly_windows:
        run_id = win.get('run_id')
        edges = analyzeCausalGraphs.edges_for_window(run_id, win) if run_id else []
        w_df = df[(df['window_start'] >= win['start']) & (df['window_end'] <= win['end'])]
        if run_id and 'run_id' in df.columns:
            w_df = w_df[w_df['run_id'] == run_id]
        _G, summary = analyzeCausalGraphs.analyze_anomaly_graph(
            win['window_id'], win, edges, w_df)
        summaries.append(summary)
    if not quiet:
        print(f"    {len(summaries)} anomaly episode(s) detected across "
              f"{len({s.get('run_id') for s in summaries})} run(s)")
    return summaries


def stage_explain(summaries: list, quiet: bool) -> list:
    """RAG explanation for each episode. Returns reports list."""
    if not quiet:
        print("[4/4] Generating RCA reports ...")
    corpus = ragExplainer.KnowledgeCorpus()
    reports = []
    for s in summaries:
        query = ragExplainer.build_retrieval_query(s)
        docs = corpus.retrieve(query, top_k=2) if query.strip() else []
        report_text = ragExplainer.synthesize_grounded_report(s, docs)
        reports.append({'summary': s, 'report': report_text, 'docs': docs})
    return reports


# --------------------------------------------------------------------------
# evaluation table
# --------------------------------------------------------------------------

def print_results_table(df_scored: pd.DataFrame, summaries: list, run_ids: list):
    """Per-fault detection and cause classification table."""
    # Map run_id → summaries (episodes)
    episodes_by_run: dict = {}
    for s in summaries:
        episodes_by_run.setdefault(s.get('run_id'), []).append(s)

    print("\n" + "=" * 78)
    print("  DEMO REPLAY — PER-FAULT RESULTS TABLE")
    print("=" * 78)
    print(f"{'Run ID':<20} {'True Label':<28} {'Det?':>4} {'Pred Family':>12} {'Match':>6}")
    print("-" * 78)

    correct = fault_total = detected_count = 0
    for run_id in run_ids:
        label = ROOT_CAUSES.get(run_id, 'Unknown')
        true_family = FAULT_FAMILIES.get(label, 'unknown')
        is_fault = true_family != 'normal'

        run_df = df_scored[df_scored['run_id'] == run_id] if 'run_id' in df_scored.columns \
            else pd.DataFrame()

        # Window-level detection (majority rule)
        if run_df.empty:
            detected = False
            pred_family = 'unknown'
        else:
            flagged_wins = sum(
                1 for _, wdf in run_df.groupby(['window_start', 'window_end'])
                if wdf['predicted_anomaly'].mean() >= 0.5
            )
            detected = flagged_wins > 0
            # Pick the most-anomalous window for classification
            best_wdf = max(run_df.groupby(['window_start', 'window_end']),
                           key=lambda x: x[1]['predicted_anomaly'].mean())[1]
            metrics = diagnosis.aggregate_window_metrics(best_wdf)
            comms = diagnosis.top_process_comms(best_wdf)
            diag = diagnosis.classify_cause(metrics, comms)
            pred_family = diag['family']

        det_str = "YES" if detected else "no"
        if is_fault:
            fault_total += 1
            if detected:
                detected_count += 1
            match = (pred_family == true_family)
            if match:
                correct += 1
            match_str = "ok" if match else "MISS"
        else:
            match_str = "N/A"

        print(f"{run_id:<20} {label:<28} {det_str:>4} {pred_family:>12} {match_str:>6}")

    print("=" * 78)
    recall = detected_count / fault_total if fault_total > 0 else 0
    top1 = correct / fault_total if fault_total > 0 else 0
    print(f"\nFault detection recall:    {recall:.3f}  ({detected_count}/{fault_total})")
    print(f"Top-1 family accuracy:     {top1:.3f}  (label-free path, fault runs only)")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Offline demo replay — runs the full RCA pipeline on the collected corpus',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument('--runs', type=str, default='',
                        help='comma-separated run_ids (default: all fault runs + baselines)')
    parser.add_argument('--no-graph', action='store_true',
                        help='skip causal graph stage (faster; no MTTR or path info)')
    parser.add_argument('--quiet', action='store_true',
                        help='suppress stage progress output')
    parser.add_argument('--features', default=FEATURES_FILE,
                        help=f'combined features CSV (default: {FEATURES_FILE})')
    parser.add_argument('--model', default=MODEL_FILE,
                        help=f'trained model artifact (default: {MODEL_FILE})')
    args = parser.parse_args()

    os.chdir(BASE_DIR)   # stages use relative data/ paths

    if args.runs:
        run_ids = [r.strip() for r in args.runs.split(',') if r.strip()]
    else:
        run_ids = DEFAULT_FAULT_RUNS + NORMAL_RUNS

    # Filter to runs that have probe data
    available = set()
    for p in __import__('glob').glob(os.path.join(BASE_DIR, 'data', 'probeData_*.jsonl')):
        available.add(os.path.basename(p)[len('probeData_'):-len('.jsonl')])
    run_ids = [r for r in run_ids if r in available]
    if not run_ids:
        print("[-] No runs found in data/. Run the fault collection first.")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("  RCA PIPELINE — OFFLINE DEMO REPLAY")
    print("=" * 60)
    print(f"  Runs: {', '.join(run_ids)}")
    print()

    t0 = time.time()

    # Stage 1: features
    df_features = stage_features(run_ids, args.quiet)
    if df_features is None or df_features.empty:
        print("[-] No feature data. Aborting.")
        sys.exit(1)

    # Stage 2: score
    df_scored = stage_score(df_features, args.model, args.quiet)

    # Stage 3: graph
    summaries = []
    if not args.no_graph:
        summaries = stage_graph(df_scored, run_ids, args.quiet)
    else:
        if not args.quiet:
            print("[3/4] Causal graph stage skipped (--no-graph)")

    # Stage 4: explain
    reports = stage_explain(summaries, args.quiet) if summaries else []

    elapsed = time.time() - t0
    print(f"\n  Pipeline completed in {elapsed:.1f}s\n")

    # Print one full report excerpt (the most interesting fault episode)
    fault_reports = [r for r in reports
                     if ROOT_CAUSES.get(r['summary'].get('run_id'), 'Normal') != 'Normal']
    if fault_reports:
        print("\n" + "-" * 60)
        print("  SAMPLE RCA REPORT (first fault episode)")
        print("-" * 60)
        print(fault_reports[0]['report'])

    # Evaluation table
    print_results_table(df_scored, summaries, run_ids)
    print()


if __name__ == '__main__':
    main()
