import os
import sys
import glob
import json
import pandas as pd

from labels import ROOT_CAUSES


def label_for_run_id(run_id: str) -> str:
    """Meta-first label lookup; unknown runs become 'Unlabeled' (never 'Custom (...)).

    Ground-truth labels are used for evaluation/training only — the production
    explainer must not read them (see PLAN.md phase 2).
    """
    try:
        with open(f'data/meta_{run_id}.json', 'r') as f:
            label = json.load(f).get('label')
        if label:
            return label
    except Exception:
        pass
    return ROOT_CAUSES.get(run_id, 'Unlabeled')

def load_jsonl(path: str):
    records = []
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return records
    with open(path, 'r') as file:
        for line in file:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records

def merge_pair(run_id: str):
    probeFile = f'data/probeData_{run_id}.jsonl'
    procfsFile = f'data/procfsData_{run_id}.jsonl'

    if not os.path.exists(probeFile):
        print(f"[-] Skipping {run_id}: probe file '{probeFile}' not found.")
        return None
    if not os.path.exists(procfsFile):
        print(f"[-] Skipping {run_id}: procfs file '{procfsFile}' not found.")
        return None

    bpftrace_records = load_jsonl(probeFile)
    psi_vmstat_records = load_jsonl(procfsFile)

    if not bpftrace_records:
        print(f"[-] Skipping {run_id}: probe file '{probeFile}' has no records.")
        return None
    if not psi_vmstat_records:
        print(f"[-] Skipping {run_id}: procfs file '{procfsFile}' has no records.")
        return None

    flat_rows = []
    for rec in bpftrace_records:
        system_fields = {k: v for k, v in rec.items() if k not in ('window_start', 'window_end', 'processes')}
        for comm, metrics in rec.get('processes', {}).items():
            row = {
                'window_start': rec.get('window_start'),
                'window_end': rec.get('window_end'),
                'comm': comm,
                'run_id': run_id
            }
            if isinstance(metrics, dict):
                row.update(metrics)
            row.update(system_fields)
            flat_rows.append(row)

    if not flat_rows:
        print(f"[-] Skipping {run_id}: no process records found in probe data.")
        return None

    bpftrace_df = pd.DataFrame(flat_rows)
    psi_vmstat_df = pd.json_normalize(psi_vmstat_records, sep='_')

    if 'window_start' not in bpftrace_df.columns or 'timestamp' not in psi_vmstat_df.columns:
        print(f"[-] Skipping {run_id}: missing timestamp columns for merge.")
        return None

    bpftrace_df = bpftrace_df.sort_values('window_start')
    psi_vmstat_df = psi_vmstat_df.sort_values('timestamp')

    # Combining data frame by timestamp nearest tolerance = 2.0s
    combined_df = pd.merge_asof(
        bpftrace_df,
        psi_vmstat_df,
        left_on='window_start',
        right_on='timestamp',
        direction='nearest',
        tolerance=2.0
    )

    combined_df['run_id'] = run_id
    combined_df['root_cause'] = label_for_run_id(run_id)
    print(f"[+] Processed {run_id}: {len(combined_df)} records labeled as '{combined_df['root_cause'].iloc[0]}'")
    return combined_df

def main():
    os.makedirs('data', exist_ok=True)
    # Check if a specific run_id was provided
    if len(sys.argv) > 1:
        run_ids = [sys.argv[1]]
    else:
        # Discover all available runs or default to all keys in ROOT_CAUSES
        discovered = set()
        for p in glob.glob('data/probeData_*.jsonl'):
            base = os.path.basename(p)
            r_id = base.replace('probeData_', '').replace('.jsonl', '')
            discovered.add(r_id)
        # Order known runs first, then any extra discovered
        run_ids = [r for r in ROOT_CAUSES.keys() if r in discovered]
        for r in sorted(discovered):
            if r not in run_ids:
                run_ids.append(r)
        if not run_ids:
            run_ids = list(ROOT_CAUSES.keys())

    df_list = []
    for r_id in run_ids:
        df = merge_pair(r_id)
        if df is not None and not df.empty:
            df_list.append(df)

    if not df_list:
        print("[-] No feature data could be extracted.")
        sys.exit(1)

    features = pd.concat(df_list, ignore_index=True)
    output_file = 'data/combined_features.csv'
    features.to_csv(output_file, index=False)
    print(f"\n[+] Successfully saved {len(features)} rows across {features['root_cause'].nunique()} root causes to {output_file}")
    print("Class distribution:")
    print(features['root_cause'].value_counts())

if __name__ == '__main__':
    main()