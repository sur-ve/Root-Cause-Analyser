#!/usr/bin/env python3
"""
detectorBenchmark.py — Compare the production OneClassSVM detector against
Isolation Forest and Local Outlier Factor on labeled RCA data.

Usage:
    python3 detectorBenchmark.py
    python3 detectorBenchmark.py data/combined_features.csv

Outputs:
    Console comparison table (capability + efficiency metrics)
    data/detector_benchmark.json
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)
from sklearn.neighbors import LocalOutlierFactor
from sklearn.preprocessing import StandardScaler
from sklearn.svm import OneClassSVM

NON_FEATURE_COLS = {'window_start', 'window_end', 'timestamp', 'comm', 'root_cause', 'run_id'}
NORMAL_LABEL = 'Normal'
# Cap training size so OneClassSVM stays tractable on large Normal baselines
MAX_TRAIN_SAMPLES = 3000
RANDOM_STATE = 42


def load_xy(path: str) -> Tuple[pd.DataFrame, np.ndarray, np.ndarray, List[str], pd.Series]:
    if not os.path.exists(path):
        print(f"[-] Feature file '{path}' not found.")
        sys.exit(1)

    df = pd.read_csv(path)
    if df.empty or 'root_cause' not in df.columns:
        print("[-] combined_features.csv is empty or missing 'root_cause'.")
        sys.exit(1)

    candidate = [c for c in df.columns if c not in NON_FEATURE_COLS]
    feature_cols = df[candidate].select_dtypes(include=['number']).columns.tolist()
    if not feature_cols:
        print("[-] No numeric features available.")
        sys.exit(1)

    X_all = df[feature_cols].fillna(0).to_numpy(dtype=float)
    y_true = (df['root_cause'] != NORMAL_LABEL).to_numpy(dtype=bool)  # True = anomaly
    labels = df['root_cause']
    return df, X_all, y_true, feature_cols, labels


def train_mask(df: pd.DataFrame) -> np.ndarray:
    normal = (df['root_cause'] == NORMAL_LABEL).to_numpy()
    if normal.sum() < 10:
        print("[!] Insufficient Normal rows; training on a random subset of all data.")
        rng = np.random.default_rng(RANDOM_STATE)
        idx = rng.choice(len(df), size=min(MAX_TRAIN_SAMPLES, len(df)), replace=False)
        mask = np.zeros(len(df), dtype=bool)
        mask[idx] = True
        return mask

    idx = np.flatnonzero(normal)
    if len(idx) > MAX_TRAIN_SAMPLES:
        rng = np.random.default_rng(RANDOM_STATE)
        idx = rng.choice(idx, size=MAX_TRAIN_SAMPLES, replace=False)
    mask = np.zeros(len(df), dtype=bool)
    mask[idx] = True
    return mask


def binary_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> Dict[str, Any]:
    # y_true / y_pred: True = anomaly
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
        'fpr': float(fp / (fp + tn)) if (fp + tn) else 0.0,
        'fnr': float(fn / (fn + tp)) if (fn + tp) else 0.0,
    }


def per_class_recall(labels: pd.Series, y_pred: np.ndarray) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for cause in sorted(labels.unique()):
        mask = (labels == cause).to_numpy()
        if mask.sum() == 0:
            continue
        out[cause] = float(y_pred[mask].mean())
    return out


def run_detector(
    name: str,
    model,
    X_train: np.ndarray,
    X_all: np.ndarray,
    scale: bool,
) -> Tuple[np.ndarray, float, float]:
    scaler = None
    train = X_train
    full = X_all
    if scale:
        scaler = StandardScaler()
        train = scaler.fit_transform(X_train)
        full = scaler.transform(X_all)

    t0 = time.perf_counter()
    model.fit(train)
    fit_s = time.perf_counter() - t0

    t1 = time.perf_counter()
    # sklearn convention: -1 = anomaly, 1 = normal
    raw = model.predict(full)
    score_s = time.perf_counter() - t1

    pred_anomaly = raw == -1
    return pred_anomaly, fit_s, score_s


def build_models() -> List[Tuple[str, Any, bool, str]]:
    """
    Returns (name, estimator, needs_scaling, brief_capability_note).
    OneClassSVM is the production detector; IF and LOF are comparison baselines.
    """
    return [
        (
            'OneClassSVM',
            OneClassSVM(kernel='rbf', gamma='scale', nu=0.05),
            True,
            'PRODUCTION. Kernel boundary around Normal; scaled features; best F1/FPR on this dataset.',
        ),
        (
            'IsolationForest',
            IsolationForest(contamination='auto', random_state=RANDOM_STATE, n_jobs=-1),
            False,
            'Prior baseline. Tree ensemble; no scaling; higher FPR on Normal (~20%).',
        ),
        (
            'LocalOutlierFactor',
            LocalOutlierFactor(
                n_neighbors=35,
                contamination='auto',
                novelty=True,
                n_jobs=-1,
            ),
            True,
            'Density / kNN novelty; perfect recall here but ~52% FPR on Normal.',
        ),
    ]


def print_table(results: List[Dict[str, Any]]) -> None:
    headers = [
        'model', 'accuracy', 'precision', 'recall', 'f1',
        'fpr', 'fnr', 'fit_s', 'score_s', 'tp', 'fp', 'fn', 'tn',
    ]
    widths = {h: max(len(h), 10) for h in headers}
    for r in results:
        for h in headers:
            widths[h] = max(widths[h], len(f"{r[h]}"))

    def fmt(row: Dict[str, Any]) -> str:
        cells = []
        for h in headers:
            v = row[h]
            if isinstance(v, float):
                cells.append(f"{v:<{widths[h]}.4f}" if h not in ('fit_s', 'score_s') else f"{v:<{widths[h]}.3f}")
            else:
                cells.append(f"{str(v):<{widths[h]}}")
        return '  '.join(cells)

    print('\n' + '=' * 100)
    print('  DETECTOR BENCHMARK — capability & efficiency')
    print('=' * 100)
    print(fmt({h: h for h in headers}))
    print('-' * 100)
    for r in results:
        print(fmt(r))
    print('=' * 100)

    print('\nCapability notes:')
    for r in results:
        print(f"  • {r['model']}: {r['capability']}")

    # Rank by F1 then by total time
    by_f1 = sorted(results, key=lambda x: (-x['f1'], x['fit_s'] + x['score_s']))
    print(f"\nBest F1: {by_f1[0]['model']} (F1={by_f1[0]['f1']:.4f})")
    by_speed = sorted(results, key=lambda x: x['fit_s'] + x['score_s'])
    print(f"Fastest end-to-end: {by_speed[0]['model']} "
          f"({by_speed[0]['fit_s'] + by_speed[0]['score_s']:.3f}s)")


def main() -> None:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    input_path = sys.argv[1] if len(sys.argv) > 1 else 'data/combined_features.csv'

    df, X_all, y_true, feature_cols, labels = load_xy(input_path)
    mask = train_mask(df)
    X_train = X_all[mask]

    print(f"[+] Rows: {len(df)} | Features: {len(feature_cols)} | "
          f"Train (Normal subsample): {mask.sum()} | "
          f"Anomaly prevalence: {y_true.mean()*100:.1f}%")

    results: List[Dict[str, Any]] = []
    for name, model, needs_scale, note in build_models():
        print(f"\n[~] Fitting {name}...")
        try:
            y_pred, fit_s, score_s = run_detector(name, model, X_train, X_all, needs_scale)
        except Exception as exc:
            print(f"[-] {name} failed: {exc}")
            continue

        metrics = binary_metrics(y_true, y_pred)
        class_recall = per_class_recall(labels, y_pred)
        row = {
            'model': name,
            'capability': note,
            'n_train': int(mask.sum()),
            'n_eval': int(len(df)),
            'n_features': len(feature_cols),
            'fit_s': fit_s,
            'score_s': score_s,
            'total_s': fit_s + score_s,
            'scaled_features': needs_scale,
            'recall_by_class': class_recall,
            **metrics,
        }
        results.append(row)
        print(f"    F1={metrics['f1']:.4f}  FPR={metrics['fpr']:.4f}  "
              f"fit={fit_s:.3f}s  score={score_s:.3f}s")

    if not results:
        print("[-] No detectors completed successfully.")
        sys.exit(1)

    print_table(results)

    out_path = 'data/detector_benchmark.json'
    os.makedirs('data', exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump({'results': results}, f, indent=2)
    print(f"\n[+] Wrote {out_path}")


if __name__ == '__main__':
    main()
