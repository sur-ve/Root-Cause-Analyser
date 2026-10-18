"""
analyzeCausalGraphs.py — builds per-window causal graphs from scored anomalies.

Edge source: data/edges_<run_id>.jsonl — raw wakeupTracker.bt lines (sampled
1-in-100 in-kernel, nanosecond timestamps since boot). The run's boot epoch is
read from data/meta_<run_id>.json, so each window only ever sees its own run's
edges; no global time-alignment heuristics are needed.
"""

import os
import sys
import json
import pandas as pd
import networkx as nx
from typing import List, Dict, Tuple, Any, Optional

NOISE_COMM_PREFIXES = (
    'swapper/',
    'bpftrace',
    'ksoftirqd/',
    'migration/',
    'rcu_preempt',
    'rcu_sched',
    'cpuhp/',
)

def is_noise_edge(edge: Dict[str, Any]) -> bool:
    waker = edge.get('waker_comm', '')
    wakee = edge.get('wakee_comm', '')
    if any(waker.startswith(p) for p in NOISE_COMM_PREFIXES):
        return True
    if any(wakee.startswith(p) for p in NOISE_COMM_PREFIXES):
        return True
    return False


# loading anomaly windows from scored_features.csv and merging overlapping intervals
def _merge_intervals(intervals: List[Dict[str, float]], padding_sec: float) -> List[Dict[str, float]]:
    """Pad and merge overlapping [start, end] intervals."""
    padded = sorted(
        ({'start': iv['start'] - padding_sec, 'end': iv['end'] + padding_sec} for iv in intervals),
        key=lambda w: w['start'],
    )
    merged: List[Dict[str, float]] = []
    for win in padded:
        if merged and win['start'] <= merged[-1]['end']:
            merged[-1]['end'] = max(merged[-1]['end'], win['end'])
        else:
            merged.append({'start': win['start'], 'end': win['end']})
    return merged


def load_anomaly_windows(scored_features_path: str, padding_sec: float = 1.0) -> Tuple[List[Dict[str, Any]], pd.DataFrame]:
    if not os.path.exists(scored_features_path):
        print(f"[-] Scored features file '{scored_features_path}' not found.")
        return [], pd.DataFrame()

    df = pd.read_csv(scored_features_path)
    if 'predicted_anomaly' not in df.columns:
        print("[-] 'predicted_anomaly' column missing from scored features.")
        return [], df

    anomalies = df[df['predicted_anomaly'] == True]
    if anomalies.empty:
        print("[-] No anomalies detected in scored_features.csv.")
        return [], df

    # Build windows per run_id: edges are collected per run, so windows must not
    # span runs (this also keeps each window traceable to its own edge file and
    # removes the need for global boot-time alignment heuristics).
    if 'run_id' in anomalies.columns:
        grouped = list(anomalies.groupby('run_id', sort=False))
    else:
        grouped = [('all', anomalies)]

    merged_windows: List[Dict[str, Any]] = []
    raw_interval_count = 0
    for run_id, run_df in grouped:
        unique_windows = run_df[['window_start', 'window_end']].drop_duplicates().sort_values('window_start')
        raw_interval_count += len(unique_windows)
        intervals = [
            {'start': float(row['window_start']), 'end': float(row['window_end'])}
            for _, row in unique_windows.iterrows()
        ]
        for i, w in enumerate(_merge_intervals(intervals, padding_sec)):
            merged_windows.append({
                'window_id': f"{run_id}_w{i + 1}",
                'run_id': run_id,
                'start': w['start'],
                'end': w['end'],
            })

    merged_windows.sort(key=lambda w: w['start'])
    print(f"[-] Found {len(anomalies)} anomaly records across {raw_interval_count} intervals "
          f"-> {len(merged_windows)} window(s) across {len(grouped)} run(s).")
    return merged_windows, df


def load_boot_epoch(run_id: str) -> Optional[float]:
    """boot_time_epoch recorded in data/meta_<run_id>.json (written at collection start)."""
    try:
        with open(f"data/meta_{run_id}.json", 'r') as mf:
            return float(json.load(mf)['boot_time_epoch'])
    except Exception:
        return None


# Parsed edge files are cached so several windows in the same run read the file once.
_RAW_EDGE_CACHE: Dict[str, List[Dict[str, Any]]] = {}


def load_run_edges(run_id: str) -> List[Dict[str, Any]]:
    """Load data/edges_<run_id>.jsonl, tagging each edge with absolute epoch seconds."""
    path = f"data/edges_{run_id}.jsonl"
    if path in _RAW_EDGE_CACHE:
        return _RAW_EDGE_CACHE[path]

    edges: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        print(f"[-] Edge file '{path}' not found.")
        _RAW_EDGE_CACHE[path] = edges
        return edges

    boot = load_boot_epoch(run_id)
    if boot is None:
        print(f"[!] data/meta_{run_id}.json missing — cannot align '{path}' to wall-clock time.")
        _RAW_EDGE_CACHE[path] = edges
        return edges

    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line.startswith('{'):
                continue
            try:
                edge = json.loads(line)
            except json.JSONDecodeError:
                continue
            if is_noise_edge(edge):
                continue
            raw_ts = float(edge.get('timestamp', 0))
            edge['_epoch_sec'] = boot + raw_ts / 1e9  # tracker timestamps are ns since boot
            edges.append(edge)

    _RAW_EDGE_CACHE[path] = edges
    return edges


def edges_for_window(run_id: str, win: Dict[str, Any]) -> List[Dict[str, Any]]:
    """All sampled wakeup edges from one run that fall inside `win`."""
    return [e for e in load_run_edges(run_id) if win['start'] <= e['_epoch_sec'] <= win['end']]


def parse_edge_nodes(edge: Dict[str, Any]) -> Tuple[str, str]:
    wakee_comm = edge.get('wakee_comm', 'unknown')
    wakee_tid  = edge.get('wakee_tid', '')
    waker_comm = edge.get('waker_comm', 'unknown')
    waker_tid  = edge.get('waker_tid', '')

    wakee_node = f"{wakee_comm}:{wakee_tid}" if wakee_tid != '' else wakee_comm
    waker_node = f"{waker_comm}:{waker_tid}" if waker_tid != '' else waker_comm
    return wakee_node, waker_node


def extract_window_top_metrics(window_df: pd.DataFrame) -> Dict[str, Any]:
    if window_df.empty:
        return {}

    candidate_metrics = [
        'runtime', 'rq_time', 'futex_wait_time', 'futex_wait_count',
        'oom_time', 'iowait_time', 'sector_count', 'locktime',
        'cpu_some_avg10', 'mem_some_avg10', 'io_some_avg10',
        'allocstall_count', 'major_fault_count', 'swap_in_count', 'swap_out_count', 'oom_kill_count'
    ]
    top_metrics = {}
    for m in candidate_metrics:
        if m in window_df.columns:
            val = float(window_df[m].max())
            if val > 0:
                top_metrics[m] = round(val, 3)

    return top_metrics


def analyze_anomaly_graph(window_id: str, win_meta: Dict[str, Any], edges: List[Dict[str, Any]], window_df: pd.DataFrame) -> Tuple[nx.DiGraph, Dict[str, Any]]:
    G = nx.DiGraph()

    for edge in edges:
        wakee_node, waker_node = parse_edge_nodes(edge)
        weight = int(edge.get('count', 1))
        if G.has_edge(wakee_node, waker_node):
            G[wakee_node][waker_node]['weight'] += weight
        else:
            G.add_edge(wakee_node, waker_node, weight=weight)

    print(f"\n==========================================")
    print(f" GRAPH ANALYSIS: {window_id} ({win_meta['start']:.2f} -> {win_meta['end']:.2f})")
    print(f" Total Nodes: {G.number_of_nodes()} | Total Edges: {G.number_of_edges()}")
    print(f"==========================================")

    top_metrics = extract_window_top_metrics(window_df)
    # Prefer majority label when a merged window spans multiple stressors
    if 'root_cause' in window_df.columns and not window_df.empty:
        labeled_causes = window_df['root_cause'].dropna().value_counts().index.tolist()
    else:
        labeled_causes = []

    summary = {
        'window_id': window_id,
        'run_id': win_meta.get('run_id', 'unknown'),
        'start': win_meta['start'],
        'end': win_meta['end'],
        'duration_sec': round(win_meta['end'] - win_meta['start'], 2),
        'node_count': G.number_of_nodes(),
        'edge_count': G.number_of_edges(),
        'candidate_roots': [],
        'sample_paths': [],
        'top_metrics': top_metrics,
        'labeled_causes': labeled_causes
    }

    if G.number_of_nodes() == 0:
        print("[-] No edges captured during this window.")
        return G, summary

    # Candidate root causes: nodes with in_degree > 0 and out_degree == 0 (terminal blockers)
    candidate_root_causes = [
        node for node in G.nodes()
        if G.out_degree(node) == 0 and G.in_degree(node) > 0
    ]
    # Sort candidate root causes by incoming edge count (most blocked callers)
    candidate_root_causes.sort(key=lambda n: G.in_degree(n), reverse=True)
    summary['candidate_roots'] = candidate_root_causes[:10]

    print("\n[!] Candidate Root Cause(s) (Terminal Blockers):")
    if candidate_root_causes:
        for root in candidate_root_causes[:5]:
            unblocked_count = G.in_degree(root)
            print(f"  ---> Node: {root} (Blocked/unblocked {unblocked_count} upstream task(s))")
    else:
        print("  None detected (graph may contain cycles).")

    # Trace paths from symptom nodes to root causes
    symptom_nodes = [node for node in G.nodes() if G.in_degree(node) == 0 and G.out_degree(node) > 0]
    sample_paths = []
    if symptom_nodes and candidate_root_causes:
        print("\n[*] Sample Causal Dependency Paths (Symptom -> ... -> Root Cause):")
        for symptom in symptom_nodes[:5]:
            for root in candidate_root_causes[:5]:
                if nx.has_path(G, symptom, root):
                    path = nx.shortest_path(G, symptom, root)
                    path_str = " -> ".join(path)
                    sample_paths.append(path_str)
                    print(f"  Path: {path_str}")
                    break
            if len(sample_paths) >= 5:
                break

    summary['sample_paths'] = sample_paths
    return G, summary


def _graph_to_jsonable(G: nx.DiGraph) -> Dict[str, Any]:
    """Serialize a NetworkX graph, compatible across NX 2.x / 3.x APIs."""
    try:
        return nx.node_link_data(G, edges='links')
    except TypeError:
        return nx.node_link_data(G)


def main():
    scored_features_file = sys.argv[1] if len(sys.argv) > 1 else 'data/scored_features.csv'
    summary_output_file = 'data/causal_summaries.json'

    anomaly_windows, df_scored = load_anomaly_windows(scored_features_file, padding_sec=1.0)
    if not anomaly_windows:
        return []

    summaries = []
    graphs_data = {}

    for win in anomaly_windows:
        w_id = win['window_id']
        run_id = win.get('run_id')
        edges = edges_for_window(run_id, win) if run_id else []

        # Extract rows for this window
        if not df_scored.empty:
            w_df = df_scored[(df_scored['window_start'] >= win['start']) & (df_scored['window_end'] <= win['end'])]
        else:
            w_df = pd.DataFrame()

        G, summary = analyze_anomaly_graph(w_id, win, edges, w_df)
        summaries.append(summary)
        graphs_data[w_id] = _graph_to_jsonable(G)

    os.makedirs('data', exist_ok=True)
    with open(summary_output_file, 'w') as sf:
        json.dump(summaries, sf, indent=2)
    print(f"\n[+] Saved {len(summaries)} causal summaries to {summary_output_file}")

    with open('data/causal_graphs.json', 'w') as gf:
        json.dump(graphs_data, gf, indent=2)
    print(f"[+] Saved NetworkX graph data to data/causal_graphs.json")

    return summaries

if __name__ == '__main__':
    main()
