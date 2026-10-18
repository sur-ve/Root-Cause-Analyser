import os
import sys
import pandas as pd
from sklearn.preprocessing import StandardScaler
from sklearn.svm import OneClassSVM

# Production detector (selected via detectorBenchmark.py):
# OneClassSVM outperformed IsolationForest and LocalOutlierFactor on labeled
# stressor data (higher F1, much lower false-positive rate on Normal).
DEFAULT_NU = 0.05
DEFAULT_KERNEL = 'rbf'
MAX_TRAIN_SAMPLES = 3000
RANDOM_STATE = 42


def run_anomaly_detection(input_file: str = 'data/combined_features.csv', output_file: str = 'data/scored_features.csv'):
    if not os.path.exists(input_file):
        print(f"[-] Feature file '{input_file}' not found.")
        sys.exit(1)

    df = pd.read_csv(input_file)
    if df.empty:
        print(f"[-] Feature file '{input_file}' is empty.")
        sys.exit(1)

    non_feature_cols = {'window_start', 'window_end', 'timestamp', 'comm', 'root_cause', 'run_id'}
    # Filter to purely numeric features
    candidate_cols = [c for c in df.columns if c not in non_feature_cols]
    numeric_df = df[candidate_cols].select_dtypes(include=['number'])
    feature_cols = numeric_df.columns.tolist()

    if not feature_cols:
        print("[-] No numeric features available for anomaly detection.")
        sys.exit(1)

    normal_mask = (df['root_cause'] == 'Normal') if 'root_cause' in df.columns else pd.Series(False, index=df.index)
    if normal_mask.sum() > 10:
        X = df.loc[normal_mask, feature_cols].fillna(0)
        # Cap training size so RBF OneClassSVM stays tractable
        if len(X) > MAX_TRAIN_SAMPLES:
            X = X.sample(n=MAX_TRAIN_SAMPLES, random_state=RANDOM_STATE)
        print(f"[+] Training OneClassSVM (kernel={DEFAULT_KERNEL}, nu={DEFAULT_NU}) "
              f"on {len(X)} 'Normal' baseline rows across {len(feature_cols)} features...")
    else:
        print(f"[!] Warning: Insufficient 'Normal' rows ({normal_mask.sum()}). Training on all available data.")
        X = df[feature_cols].fillna(0)
        if len(X) > MAX_TRAIN_SAMPLES:
            X = X.sample(n=MAX_TRAIN_SAMPLES, random_state=RANDOM_STATE)

    y = df[feature_cols].fillna(0)

    # Scale features — required for RBF kernel distance geometry
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    y_scaled = scaler.transform(y)

    model = OneClassSVM(kernel=DEFAULT_KERNEL, gamma='scale', nu=DEFAULT_NU)
    model.fit(X_scaled)

    # continuous anomaly score: negative = anomaly, positive = normal (sklearn convention)
    df['anomaly_score'] = model.decision_function(y_scaled)
    df['predicted_anomaly'] = model.predict(y_scaled) == -1

    if 'root_cause' in df.columns:
        summary = df.groupby('root_cause')['predicted_anomaly'].agg(['sum', 'count', 'mean'])
        summary.columns = ['flagged', 'total_windows', 'flagged_rate']
        print('\nSummary of all classes:')
        print(summary)

        print("\nRecall by fault type:")
        recall = summary[summary.index != 'Normal']['flagged_rate']
        print(recall)

        if 'Normal' in summary.index:
            print(f"\nFalse positive rate (Normal): {summary.loc['Normal', 'flagged_rate']:.3f}")

    total_flagged = df['predicted_anomaly'].sum()
    print(f"\n[+] Total anomalous windows flagged: {total_flagged} / {len(df)} ({total_flagged/len(df)*100:.1f}%)")
    df.to_csv(output_file, index=False)
    print(f"[+] Saved scored features to {output_file}")
    return df

def main():
    input_file = sys.argv[1] if len(sys.argv) > 1 else 'data/combined_features.csv'
    output_file = sys.argv[2] if len(sys.argv) > 2 else 'data/scored_features.csv'
    run_anomaly_detection(input_file, output_file)

if __name__ == '__main__':
    main()
