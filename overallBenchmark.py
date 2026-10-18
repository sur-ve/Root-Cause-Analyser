#!/usr/bin/env python3
"""
overallBenchmark.py — End-to-end accuracy benchmark for the RCA program
using labeled stressor test data (ground truth).

Evaluates:
  1. Anomaly detection (OneClassSVM — production detector) — TP / TN / FP / FN
  2. Per-fault-class recall
  3. Metric-heuristic cause classification (RAG path without peeking at labels)

Usage:
    python3 overallBenchmark.py
    python3 overallBenchmark.py data/combined_features.csv --test-size 0.3

Outputs:
    Console report
    data/overall_benchmark.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.svm import OneClassSVM

NON_FEATURE_COLS = {'window_start', 'window_end', 'timestamp', 'comm', 'root_cause', 'run_id'}
NORMAL_LABEL = 'Normal'
RANDOM_STATE = 42
OCSVM_NU = 0.05
MAX_TRAIN_SAMPLES = 3000

# Maps ground-truth stressor labels → coarse cause families used by heuristics
LABEL_TO_FAMILY = {
    'CPU Competition': 'cpu',
    'Process Throttled': 'cpu',
    'Baseline CPU Usage': 'cpu',
    'Memory Swapping': 'memory',
    'OOM Kill': 'oom',
    'Continuous Heap Expansion': 'heap',
    'Userspace Contention': 'lock',
    'Kernel-Internal Contention': 'lock',
    'Small Random Writes': 'io',
    'Large Sequential Writes': 'io',
}

FAMILY_NAMES = {
    'cpu': 'CPU / Scheduling',
    'memory': 'Memory Pressure',
    'oom': 'OOM',
    'heap': 'Heap Expansion',
    'lock': 'Lock Contention',
    'io': 'I/O Saturation',
    'unknown': 'Unknown',
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='Overall RCA program accuracy benchmark')
    p.add_argument('input', nargs='?', default='data/combined_features.csv')
    p.add_argument('--test-size', type=float, default=0.3,
                   help='Fraction of data held out for testing (default 0.3)')
    p.add_argument('--nu', type=float, default=OCSVM_NU,
                   help=f'OneClassSVM nu (default {OCSVM_NU})')
    return p.parse_args()


def feature_matrix(df: pd.DataFrame) -> Tuple[np.ndarray, List[str]]:
    candidate = [c for c in df.columns if c not in NON_FEATURE_COLS]
    cols = df[candidate].select_dtypes(include=['number']).columns.tolist()
    return df[cols].fillna(0).to_numpy(dtype=float), cols


def detection_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> Dict[str, Any]:
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[False, True]).ravel()
    return {
        'tp': int(tp),
        'tn': int(tn),
        'fp': int(fp),
        'fn': int(fn),
        'accuracy': float(accuracy_score(y_true, y_pred)),
        'precision': float(precision_score(y_true, y_pred, zero_division=0)),
        'recall': float(recall_score(y_true, y_pred, zero_division=0)),
        'f1': float(f1_score(y_true, y_pred, zero_division=0)),
        'false_positive_rate': float(fp / (fp + tn)) if (fp + tn) else 0.0,
        'false_negative_rate': float(fn / (fn + tp)) if (fn + tp) else 0.0,
        'specificity': float(tn / (tn + fp)) if (tn + fp) else 0.0,
    }


def classify_family_from_metrics(row: pd.Series) -> str:
    """Mirror ragExplainer metric heuristics without using labeled_causes."""
    oom = float(row.get('oom_kill_count', 0) or 0)
    swap_in = float(row.get('swap_in_count', 0) or 0)
    swap_out = float(row.get('swap_out_count', 0) or 0)
    sectors = float(row.get('sector_count', 0) or 0)
    iowait = float(row.get('iowait_time', 0) or 0)
    futex = float(row.get('futex_wait_count', 0) or 0)
    locktime = float(row.get('locktime', 0) or 0)
    cpu_psi = float(row.get('cpu_some_avg10', 0) or 0)
    oom_time = float(row.get('oom_time', 0) or 0)

    if oom > 0 or oom_time > 0:
        return 'oom'
    if swap_in > 100 or swap_out > 100:
        return 'memory'
    if sectors > 5000:
        return 'io'
    if iowait > 0:
        return 'io'
    if futex > 1000:
        return 'lock'
    if locktime > 1e6:
        return 'lock'
    if cpu_psi > 50:
        return 'cpu'
    # Mild heap signal: high runtime alone is weak; leave unknown if nothing fires
    return 'unknown'


def evaluate_cause_heuristics(df: pd.DataFrame) -> Dict[str, Any]:
    """On non-Normal rows, compare heuristic family vs labeled family."""
    faults = df[df['root_cause'] != NORMAL_LABEL].copy()
    if faults.empty:
        return {'n': 0, 'accuracy': 0.0, 'by_family': {}}

    y_true = faults['root_cause'].map(LABEL_TO_FAMILY).fillna('unknown')
    y_pred = faults.apply(classify_family_from_metrics, axis=1)

    # Treat 'unknown' predictions as misses
    correct = (y_true == y_pred) & (y_pred != 'unknown')
    # Also count unknown→unknown only if label truly unknown (rare)
    acc = float(correct.mean()) if len(faults) else 0.0

    by_family: Dict[str, Dict[str, Any]] = {}
    for fam in sorted(set(y_true.unique()) | set(y_pred.unique())):
        mask = y_true == fam
        if mask.sum() == 0:
            continue
        by_family[FAMILY_NAMES.get(fam, fam)] = {
            'n': int(mask.sum()),
            'recall': float((y_pred[mask] == fam).mean()),
        }

    report = classification_report(
        y_true, y_pred, zero_division=0, output_dict=True
    )
    return {
        'n': int(len(faults)),
        'accuracy': acc,
        'strict_label_match_rate': float((y_true == y_pred).mean()),
        'by_family': by_family,
        'classification_report': report,
    }


def print_detection_report(split_name: str, m: Dict[str, Any], elapsed: float) -> None:
    print(f"\n--- Anomaly detection ({split_name}) ---")
    print(f"  TP={m['tp']}  TN={m['tn']}  FP={m['fp']}  FN={m['fn']}")
    print(f"  Accuracy  : {m['accuracy']:.4f}")
    print(f"  Precision : {m['precision']:.4f}")
    print(f"  Recall    : {m['recall']:.4f}  (false-negative rate={m['false_negative_rate']:.4f})")
    print(f"  F1        : {m['f1']:.4f}")
    print(f"  FPR       : {m['false_positive_rate']:.4f}  (specificity={m['specificity']:.4f})")
    print(f"  Fit+score : {elapsed:.3f}s")


def main() -> None:
    args = parse_args()
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    if not os.path.exists(args.input):
        print(f"[-] '{args.input}' not found. Run featureExtractor.py first.")
        sys.exit(1)

    df = pd.read_csv(args.input)
    if df.empty or 'root_cause' not in df.columns:
        print("[-] Input missing data or 'root_cause' labels.")
        sys.exit(1)

    X, feature_cols = feature_matrix(df)
    y_anomaly = (df['root_cause'] != NORMAL_LABEL).to_numpy(dtype=bool)

    # Stratified hold-out so Normal and each fault class appear in test
    idx = np.arange(len(df))
    try:
        train_idx, test_idx = train_test_split(
            idx,
            test_size=args.test_size,
            random_state=RANDOM_STATE,
            stratify=df['root_cause'],
        )
    except ValueError:
        train_idx, test_idx = train_test_split(
            idx, test_size=args.test_size, random_state=RANDOM_STATE
        )

    # Train OneClassSVM on Normal rows inside the train split only
    train_df = df.iloc[train_idx]
    normal_train_mask = (train_df['root_cause'] == NORMAL_LABEL).to_numpy()
    if normal_train_mask.sum() < 10:
        print("[!] Too few Normal rows in train split; fitting on all train rows.")
        X_fit = X[train_idx]
    else:
        X_fit = X[train_idx][normal_train_mask]

    if len(X_fit) > MAX_TRAIN_SAMPLES:
        rng = np.random.default_rng(RANDOM_STATE)
        X_fit = X_fit[rng.choice(len(X_fit), size=MAX_TRAIN_SAMPLES, replace=False)]

    print('=' * 72)
    print('  OVERALL RCA PROGRAM BENCHMARK (OneClassSVM)')
    print('=' * 72)
    print(f"  Features : {len(feature_cols)}")
    print(f"  Train / Test : {len(train_idx)} / {len(test_idx)} "
          f"(test_size={args.test_size})")
    print(f"  Normal train samples for OCSVM : {len(X_fit)} (nu={args.nu})")
    print(f"  Class distribution (full):\n{df['root_cause'].value_counts().to_string()}")

    scaler = StandardScaler()
    X_fit_scaled = scaler.fit_transform(X_fit)
    X_all_scaled = scaler.transform(X)

    model = OneClassSVM(kernel='rbf', gamma='scale', nu=args.nu)
    t0 = time.perf_counter()
    model.fit(X_fit_scaled)

    def predict_anomaly(indices: np.ndarray) -> np.ndarray:
        return model.predict(X_all_scaled[indices]) == -1

    y_pred_train = predict_anomaly(train_idx)
    y_pred_test = predict_anomaly(test_idx)
    elapsed = time.perf_counter() - t0

    m_train = detection_metrics(y_anomaly[train_idx], y_pred_train)
    m_test = detection_metrics(y_anomaly[test_idx], y_pred_test)

    print_detection_report('TRAIN', m_train, elapsed)
    print_detection_report('TEST (held-out)', m_test, elapsed)

    # Per-class flagged rate on test set
    test_labels = df.iloc[test_idx]['root_cause'].reset_index(drop=True)
    print('\n--- Per-class anomaly flag rate (TEST) ---')
    per_class: Dict[str, Dict[str, float]] = {}
    for cause in sorted(test_labels.unique()):
        mask = (test_labels == cause).to_numpy()
        rate = float(y_pred_test[mask].mean()) if mask.any() else 0.0
        n = int(mask.sum())
        per_class[cause] = {'n': n, 'flagged_rate': rate}
        kind = 'FP rate' if cause == NORMAL_LABEL else 'recall'
        print(f"  {cause:<32} n={n:<5} {kind}={rate:.3f}")

    # Cause-family heuristic accuracy on full data (explainer path)
    cause_eval = evaluate_cause_heuristics(df)
    print('\n--- Cause-family heuristic accuracy (explainer, no label peek) ---')
    print(f"  Fault rows evaluated : {cause_eval['n']}")
    print(f"  Family accuracy      : {cause_eval['accuracy']:.4f}")
    print(f"  Strict match rate    : {cause_eval['strict_label_match_rate']:.4f}")
    for fam, stats in cause_eval.get('by_family', {}).items():
        print(f"    {fam:<22} n={stats['n']:<5} recall={stats['recall']:.3f}")

    # Full-data scores matching production anomalyDetection.py
    t1 = time.perf_counter()
    normal_all = df['root_cause'] == NORMAL_LABEL
    X_full_fit = X[normal_all.to_numpy()] if normal_all.sum() >= 10 else X
    if len(X_full_fit) > MAX_TRAIN_SAMPLES:
        rng = np.random.default_rng(RANDOM_STATE)
        X_full_fit = X_full_fit[rng.choice(len(X_full_fit), size=MAX_TRAIN_SAMPLES, replace=False)]
    full_scaler = StandardScaler()
    X_full_fit_s = full_scaler.fit_transform(X_full_fit)
    X_full_s = full_scaler.transform(X)
    full_model = OneClassSVM(kernel='rbf', gamma='scale', nu=args.nu)
    full_model.fit(X_full_fit_s)
    y_pred_full = full_model.predict(X_full_s) == -1
    m_full = detection_metrics(y_anomaly, y_pred_full)
    full_elapsed = time.perf_counter() - t1
    print_detection_report('FULL DATA (production-style OCSVM)', m_full, full_elapsed)

    report = {
        'input': args.input,
        'detector': 'OneClassSVM',
        'nu': args.nu,
        'test_size': args.test_size,
        'n_features': len(feature_cols),
        'n_train': int(len(train_idx)),
        'n_test': int(len(test_idx)),
        'n_normal_fit': int(len(X_fit)),
        'detection_train': m_train,
        'detection_test': m_test,
        'detection_full': m_full,
        'per_class_test': per_class,
        'cause_heuristics': {
            k: v for k, v in cause_eval.items() if k != 'classification_report'
        },
        'timing_s': {
            'holdout_fit_score': elapsed,
            'full_fit_score': full_elapsed,
        },
    }

    os.makedirs('data', exist_ok=True)
    out_path = 'data/overall_benchmark.json'
    with open(out_path, 'w') as f:
        json.dump(report, f, indent=2)

    print('\n' + '=' * 72)
    print(f"  TEST summary: Acc={m_test['accuracy']:.3f}  "
          f"F1={m_test['f1']:.3f}  FP={m_test['fp']}  FN={m_test['fn']}")
    print(f"  Saved → {out_path}")
    print('=' * 72 + '\n')


if __name__ == '__main__':
    main()
