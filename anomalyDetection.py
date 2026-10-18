#!/usr/bin/env python3
"""
anomalyDetection.py — production anomaly detector (StandardScaler + OneClassSVM).

Two-phase usage with persisted artifacts:

    python3 anomalyDetection.py --train      # fit on 'Normal' rows, save model
    python3 anomalyDetection.py --score      # score data/combined_features.csv
    python3 anomalyDetection.py              # = --score (auto-trains when no model yet)

Files:
    data/model/ocsvm.joblib   scaler + model + feature list + training metadata
    data/scored_features.csv  input rows + anomaly_score + predicted_anomaly

Training uses ONLY rows labeled 'Normal' (baseline runs, e.g.
`sudo python3 parentProcess.py default1`). With too few Normal rows the script
refuses to train — a model fit on fault traffic would flag everything or
nothing.
"""

import argparse
import os
import sys
import time

import joblib
import pandas as pd
from sklearn.preprocessing import StandardScaler
from sklearn.svm import OneClassSVM

DEFAULT_INPUT = 'data/combined_features.csv'
DEFAULT_OUTPUT = 'data/scored_features.csv'
DEFAULT_MODEL = 'data/model/ocsvm.joblib'

DEFAULT_NU = 0.05
DEFAULT_KERNEL = 'rbf'
MAX_TRAIN_SAMPLES = 3000
RANDOM_STATE = 42
MIN_NORMAL_ROWS = 50

NON_FEATURE_COLS = {'window_start', 'window_end', 'timestamp', 'comm', 'root_cause', 'run_id'}
NORMAL_LABEL = 'Normal'


def get_feature_cols(df: pd.DataFrame):
    candidate = [c for c in df.columns if c not in NON_FEATURE_COLS]
    return df[candidate].select_dtypes(include=['number']).columns.tolist()


def train_model(input_file: str = DEFAULT_INPUT, model_file: str = DEFAULT_MODEL,
                nu: float = DEFAULT_NU) -> dict:
    if not os.path.exists(input_file):
        print(f"[-] Feature file '{input_file}' not found.")
        sys.exit(1)

    df = pd.read_csv(input_file)
    feature_cols = get_feature_cols(df)
    if not feature_cols:
        print("[-] No numeric features available for training.")
        sys.exit(1)

    normal = df[df['root_cause'] == NORMAL_LABEL] if 'root_cause' in df.columns else df.iloc[0:0]
    if len(normal) < MIN_NORMAL_ROWS:
        print(f"[-] Only {len(normal)} '{NORMAL_LABEL}' rows in '{input_file}' "
              f"(need >= {MIN_NORMAL_ROWS}).")
        print("    Collect baselines first, e.g.:  sudo python3 parentProcess.py default1")
        sys.exit(1)

    X = normal[feature_cols].fillna(0)
    if len(X) > MAX_TRAIN_SAMPLES:
        X = X.sample(n=MAX_TRAIN_SAMPLES, random_state=RANDOM_STATE)

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    model = OneClassSVM(kernel=DEFAULT_KERNEL, gamma='scale', nu=nu)
    model.fit(X_scaled)

    os.makedirs(os.path.dirname(model_file), exist_ok=True)
    artifact = {
        'scaler': scaler,
        'model': model,
        'feature_cols': feature_cols,
        'trained_at': time.time(),
        'n_train_rows': int(len(X)),
        'nu': nu,
        'kernel': DEFAULT_KERNEL,
        'input_file': input_file,
    }
    joblib.dump(artifact, model_file)
    print(f"[+] Trained OneClassSVM (kernel={DEFAULT_KERNEL}, nu={nu}) on {len(X)} "
          f"'{NORMAL_LABEL}' rows across {len(feature_cols)} features -> {model_file}")
    return artifact


def load_artifact(model_file: str = DEFAULT_MODEL):
    if not os.path.exists(model_file):
        return None
    try:
        return joblib.load(model_file)
    except Exception as e:
        print(f"[!] Could not load model '{model_file}': {e}")
        return None


def score_features(input_file: str = DEFAULT_INPUT, output_file: str = DEFAULT_OUTPUT,
                   model_file: str = DEFAULT_MODEL) -> pd.DataFrame:
    if not os.path.exists(input_file):
        print(f"[-] Feature file '{input_file}' not found.")
        sys.exit(1)

    df = pd.read_csv(input_file)
    if df.empty:
        print(f"[-] Feature file '{input_file}' is empty.")
        sys.exit(1)

    artifact = load_artifact(model_file)
    if artifact is None:
        print(f"[!] No model at '{model_file}' — training one now (persisted for later scoring).")
        artifact = train_model(input_file, model_file)

    # Align columns to the training feature list: missing -> 0, extras ignored
    feature_cols = artifact['feature_cols']
    X = pd.DataFrame(0.0, index=df.index, columns=feature_cols)
    for col in feature_cols:
        if col in df.columns:
            X[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

    X_scaled = artifact['scaler'].transform(X)
    # sklearn convention: negative decision_function = anomaly
    df['anomaly_score'] = artifact['model'].decision_function(X_scaled)
    df['predicted_anomaly'] = artifact['model'].predict(X_scaled) == -1

    print(f"[+] Scored {len(df)} rows with '{model_file}' "
          f"(trained on {artifact['n_train_rows']} {NORMAL_LABEL} rows)")

    if 'root_cause' in df.columns:
        summary = df.groupby('root_cause')['predicted_anomaly'].agg(['sum', 'count', 'mean'])
        summary.columns = ['flagged', 'total_windows', 'flagged_rate']
        print('\nSummary of all classes:')
        print(summary)

        recall = summary[~summary.index.isin([NORMAL_LABEL, 'Unlabeled'])]['flagged_rate']
        if not recall.empty:
            print("\nRecall (flagged rate) by fault type:")
            print(recall)
        if NORMAL_LABEL in summary.index:
            print(f"\nFalse positive rate ({NORMAL_LABEL}): "
                  f"{summary.loc[NORMAL_LABEL, 'flagged_rate']:.3f}")

    total_flagged = int(df['predicted_anomaly'].sum())
    print(f"\n[+] Total anomalous windows flagged: {total_flagged} / {len(df)} "
          f"({total_flagged/len(df)*100:.1f}%)")
    df.to_csv(output_file, index=False)
    print(f"[+] Saved scored features to {output_file}")
    return df


def run_anomaly_detection(input_file: str = DEFAULT_INPUT, output_file: str = DEFAULT_OUTPUT):
    """Backward-compatible entry point: score (auto-training when needed)."""
    return score_features(input_file, output_file, DEFAULT_MODEL)


def main():
    parser = argparse.ArgumentParser(description='OneClassSVM anomaly detection with persisted artifacts')
    parser.add_argument('--train', action='store_true',
                        help='fit the model on Normal rows and persist it, then exit')
    parser.add_argument('--score', action='store_true',
                        help='score features with the persisted model (default action)')
    parser.add_argument('--input', default=DEFAULT_INPUT, help=f'features CSV (default: {DEFAULT_INPUT})')
    parser.add_argument('--output', default=DEFAULT_OUTPUT, help=f'scored CSV (default: {DEFAULT_OUTPUT})')
    parser.add_argument('--model', default=DEFAULT_MODEL, help=f'model artifact (default: {DEFAULT_MODEL})')
    parser.add_argument('--nu', type=float, default=DEFAULT_NU,
                        help=f'OneClassSVM nu for --train (default: {DEFAULT_NU})')
    args = parser.parse_args()

    if args.train:
        train_model(args.input, args.model, nu=args.nu)
    else:
        score_features(args.input, args.output, args.model)


if __name__ == '__main__':
    main()
