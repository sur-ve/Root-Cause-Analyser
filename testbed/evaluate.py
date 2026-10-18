#!/usr/bin/env python3
"""testbed/evaluate.py — ground truth vs pipeline output evaluation.

Usage (from the project root):
    python3 testbed/evaluate.py [--features data/scored_features.csv]
                                [--ground-truth data/ground_truth.jsonl]
                                [--output data/evaluation_results.csv]
                                [--metrics data/demo_metrics.json]

Output:
    data/evaluation_results.csv — per-fault: truth, detected?, cause, match, latency
    data/demo_metrics.json      — Top-1 accuracy, detection recall/FPR, MTTR, overhead

Does NOT require root. Runs on the already-collected + scored corpus.

Plan 4.7: accuracy is computed on the label-free path (metrics + graph +
retrieval via diagnosis.py), never from the ground-truth labels.
"""

import argparse
import json
import os
import sys
import time

import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import diagnosis
from labels import FAULT_FAMILIES, ROOT_CAUSES

SCORED_FILE = os.path.join(BASE_DIR, 'data', 'scored_features.csv')
GT_FILE = os.path.join(BASE_DIR, 'data', 'ground_truth.jsonl')
CAUSAL_SUMMARIES_FILE = os.path.join(BASE_DIR, 'data', 'causal_summaries.json')
OUT_CSV = os.path.join(BASE_DIR, 'data', 'evaluation_results.csv')
OUT_METRICS = os.path.join(BASE_DIR, 'data', 'demo_metrics.json')

NORMAL_LABEL = 'Normal'

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def load_ground_truth(path: str) -> dict:
    """Returns {run_id: gt_record} — last entry wins (memStressor2 duplicates)."""
    if not os.path.exists(path):
        return {}
    records = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                records[rec['run_id']] = rec
            except Exception:
                pass
    return records


def load_causal_summaries(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def episode_latency(gt_rec: dict, summaries_by_run: dict) -> float:
    """Seconds from stressor_start_time to the first alert for that run.

    Returns None if the run was not detected.
    """
    run_id = gt_rec['run_id']
    eps = summaries_by_run.get(run_id, [])
    if not eps:
        return None
    stressor_start = gt_rec.get('stressor_start_time')
    if stressor_start is None:
        return None
    first_alert = min(ep.get('start', float('inf')) for ep in eps)
    latency = first_alert - stressor_start
    return round(latency, 2) if latency > -60 else None   # ignore pre-stressor alerts


# --------------------------------------------------------------------------
# main evaluation
# --------------------------------------------------------------------------


def evaluate(scored_path: str, gt_path: str, summaries_path: str,
             out_csv: str, out_metrics: str) -> pd.DataFrame:
    t0 = time.time()

    # ---- scored features ----
    if not os.path.exists(scored_path):
        print(f"[-] Scored features not found: {scored_path}")
        print("    Run: python3 anomalyDetection.py --score")
        sys.exit(1)
    df = pd.read_csv(scored_path)
    if 'predicted_anomaly' not in df.columns:
        print("[-] 'predicted_anomaly' column missing from scored features.")
        sys.exit(1)

    # ---- ground truth ----
    gt = load_ground_truth(gt_path)
    if not gt:
        print(f"[-] No ground truth found at {gt_path}")
        sys.exit(1)

    # ---- causal summaries (for MTTR) ----
    summaries = load_causal_summaries(summaries_path)
    summaries_by_run: dict = {}
    for s in summaries:
        run_id = s.get('run_id', 'unknown')
        summaries_by_run.setdefault(run_id, []).append(s)

    # ---- per-run evaluation ----
    rows = []
    normal_windows = fault_windows = tp = fp = fn = 0
    mttr_values = []
    correct_family = 0
    fault_run_count = 0

    for run_id, gt_rec in sorted(gt.items()):
        label = gt_rec.get('label', ROOT_CAUSES.get(run_id, 'Unknown'))
        family = FAULT_FAMILIES.get(label, 'unknown')
        is_fault = family != 'normal'

        # Subset scored data for this run
        run_df = df[df['run_id'] == run_id] if 'run_id' in df.columns else pd.DataFrame()

        # Detection: is any window flagged?
        if run_df.empty:
            detected = False
            predicted_family = 'unknown'
            predicted_cause = 'Unclassified Anomaly'
            flagged_windows = 0
            total_windows_run = 0
        else:
            # Unique windows
            win_groups = run_df.groupby(['window_start', 'window_end'])
            total_windows_run = len(win_groups)

            # Window-level flagging: majority rule (>= 50% rows flagged)
            flagged_windows = sum(
                1 for _, wdf in win_groups
                if wdf['predicted_anomaly'].mean() >= 0.5
            )
            detected = flagged_windows > 0

            # Cause classification from best (highest flagged fraction) window
            best_wdf = max(win_groups, key=lambda x: x[1]['predicted_anomaly'].mean())[1]
            metrics = diagnosis.aggregate_window_metrics(best_wdf)
            comms = diagnosis.top_process_comms(best_wdf)
            diag = diagnosis.classify_cause(metrics, comms)
            predicted_family = diag['family']
            predicted_cause = diag['cause']

        # Detection stats
        if is_fault:
            fault_windows += total_windows_run
            fault_run_count += 1
            if detected:
                tp += 1
            else:
                fn += 1
        else:
            normal_windows += total_windows_run
            if detected:
                fp += 1

        # Family accuracy (label-free path — plan 4.7)
        family_match = (predicted_family == family) if is_fault else None
        if is_fault and family_match:
            correct_family += 1

        # Latency (MTTR)
        latency = episode_latency(gt_rec, summaries_by_run) if is_fault and detected else None
        if latency is not None and latency >= 0:
            mttr_values.append(latency)

        rows.append({
            'run_id': run_id,
            'ground_truth_label': label,
            'ground_truth_family': family,
            'total_windows': total_windows_run,
            'flagged_windows': flagged_windows,
            'detected': detected,
            'predicted_family': predicted_family,
            'predicted_cause': predicted_cause,
            'family_match': family_match,
            'alert_latency_sec': latency,
        })

    results_df = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)
    results_df.to_csv(out_csv, index=False)

    # ---- summary metrics ----
    fault_detected = tp
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    fpr = fp / len([r for r in rows if r['ground_truth_family'] == 'normal']) if \
        any(r['ground_truth_family'] == 'normal' for r in rows) else 0.0
    top1_acc = correct_family / fault_run_count if fault_run_count > 0 else 0.0
    mttr_mean = sum(mttr_values) / len(mttr_values) if mttr_values else None

    metrics = {
        'evaluated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'n_runs': len(rows),
        'n_fault_runs': fault_run_count,
        'n_normal_runs': len(rows) - fault_run_count,
        'fault_runs_detected': fault_detected,
        'fault_run_recall': round(recall, 4),
        'normal_run_false_positive_rate': round(fpr, 4),
        'top1_family_accuracy_label_free': round(top1_acc, 4),
        'mttr_sec_mean': round(mttr_mean, 2) if mttr_mean is not None else None,
        'mttr_values_sec': mttr_values,
        'elapsed_sec': round(time.time() - t0, 2),
    }
    with open(out_metrics, 'w', encoding='utf-8') as f:
        json.dump(metrics, f, indent=2)

    # ---- pretty print ----
    print("\n" + "=" * 72)
    print("  EVALUATION RESULTS")
    print("=" * 72)
    print(f"{'Run ID':<22} {'Label':<30} {'Det?':>4} {'Fam match':>10} {'Latency(s)':>11}")
    print("-" * 72)
    for r in rows:
        det = "YES" if r['detected'] else "no"
        fam = ("ok" if r['family_match'] else "MISS") if r['family_match'] is not None else "N/A"
        lat = f"{r['alert_latency_sec']:.1f}" if r['alert_latency_sec'] is not None else "-"
        print(f"{r['run_id']:<22} {r['ground_truth_label']:<30} {det:>4} {fam:>10} {lat:>11}")

    print("=" * 72)
    print(f"\nDetection recall:          {recall:.3f}  ({fault_detected}/{fault_run_count} fault runs detected)")
    print(f"Normal FP rate:            {fpr:.3f}  ({fp} normal runs flagged)")
    print(f"Top-1 family accuracy:     {top1_acc:.3f}  (label-free path)")
    print(f"MTTR (mean latency):       " + (f"{mttr_mean:.1f}s" if mttr_mean else "N/A") +
          f"  ({len(mttr_values)} measurements)")
    print(f"\nResults: {out_csv}")
    print(f"Metrics: {out_metrics}")

    return results_df


def main():
    parser = argparse.ArgumentParser(
        description='RCA evaluator: ground truth vs pipeline output',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument('--features', default=SCORED_FILE,
                        help=f'scored features CSV (default: {SCORED_FILE})')
    parser.add_argument('--ground-truth', default=GT_FILE,
                        help=f'ground truth JSONL (default: {GT_FILE})')
    parser.add_argument('--summaries', default=CAUSAL_SUMMARIES_FILE,
                        help=f'causal summaries for MTTR (default: {CAUSAL_SUMMARIES_FILE})')
    parser.add_argument('--output', default=OUT_CSV,
                        help=f'results CSV (default: {OUT_CSV})')
    parser.add_argument('--metrics', default=OUT_METRICS,
                        help=f'metrics JSON (default: {OUT_METRICS})')
    args = parser.parse_args()

    evaluate(args.features, args.ground_truth, args.summaries, args.output, args.metrics)


if __name__ == '__main__':
    main()
