import os
import sys
import glob
import json
import time
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

# Gap larger than this splits anomaly windows into separate boot-era clusters
CLUSTER_GAP_SEC = 3600.0


def is_noise_edge(edge: Dict[str, Any]) -> bool:
    waker = edge.get('waker_comm', '')
    wakee = edge.get('wakee_comm', '')
    if any(waker.startswith(p) for p in NOISE_COMM_PREFIXES):
        return True
    if any(wakee.startswith(p) for p in NOISE_COMM_PREFIXES):
        return True
    return False


def edge_boot_relative_sec(raw_ts: float) -> float:
    """Convert raw edge timestamp to seconds-since-boot (or pass through if already seconds)."""
    if raw_ts > 1e11:  # nanoseconds since boot
        return raw_ts / 1e9
    return raw_ts


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


def _load_meta_boot_times() -> List[float]:
    """Collect boot_time_epoch values from all meta JSON files."""
    boots: List[float] = []
    seen = set()
    for meta_path in sorted(glob.glob('data/meta_*.json')) + ['data/run_meta.json']:
        if not os.path.exists(meta_path) or os.path.getsize(meta_path) == 0:
            continue
        try:
            with open(meta_path, 'r') as mf:
                meta = json.load(mf)
            if 'boot_time_epoch' in meta:
                bt = float(meta['boot_time_epoch'])
                key = round(bt, 3)
                if key not in seen:
                    seen.add(key)
                    boots.append(bt)
        except Exception:
            continue
    return boots


def _sample_edge_boot_rel_times(edges_jsonl_path: str, max_samples: int = 50) -> Tuple[Optional[float], Optional[float], List[float]]:
    """Return (first_ts, last_ts, evenly-ish sampled boot-relative seconds)."""
    first_ts = None
    last_ts = None
    samples: List[float] = []
    if not os.path.exists(edges_jsonl_path):
        return None, None, samples

    # First pass: count valid lines roughly via file size heuristic is hard;
    # collect first N and track last, then sample by stride on a second light pass.
    valid_count = 0
    try:
        with open(edges_jsonl_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith('{'):
                    continue
                try:
                    d = json.loads(line)
                    ts = edge_boot_relative_sec(float(d['timestamp']))
                except Exception:
                    continue
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
                valid_count += 1
    except Exception:
        return first_ts, last_ts, samples

    if valid_count == 0:
        return first_ts, last_ts, samples

    stride = max(1, valid_count // max_samples)
    idx = 0
    try:
        with open(edges_jsonl_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith('{'):
                    continue
                try:
                    d = json.loads(line)
                    ts = edge_boot_relative_sec(float(d['timestamp']))
                except Exception:
                    continue
                if idx % stride == 0:
                    samples.append(ts)
                idx += 1
    except Exception:
        pass

    return first_ts, last_ts, samples


def _cluster_windows(anomaly_windows: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Split windows into clusters separated by large time gaps (different boots/sessions)."""
    if not anomaly_windows:
        return []
    ordered = sorted(anomaly_windows, key=lambda w: w['start'])
    clusters: List[List[Dict[str, Any]]] = [[ordered[0]]]
    for w in ordered[1:]:
        if w['start'] - clusters[-1][-1]['end'] > CLUSTER_GAP_SEC:
            clusters.append([w])
        else:
            clusters[-1].append(w)
    return clusters


def _boot_hit_score(boot: float, windows: List[Dict[str, Any]], sample_ts: List[float]) -> int:
    """How many sampled edge timestamps fall inside any window under this boot offset."""
    hits = 0
    for ts in sample_ts:
        ep = boot + ts
        for w in windows:
            if w['start'] <= ep <= w['end']:
                hits += 1
                break
    return hits


def resolve_boot_times(edges_jsonl_path: str, anomaly_windows: List[Dict[str, Any]]) -> List[float]:
    """
    Resolve one or more boot_time_epoch values for edge↔window alignment.

    A single global boot fails when scored features span multiple boots (e.g. old
    corpus + a fresh quick-start run). We cluster windows by time gap and pick a
    boot per cluster, verified by actual sample hits rather than a loose span check.
    """
    meta_boots = _load_meta_boot_times()
    current_boot = time.time() - time.monotonic()
    first_ts, last_ts, samples = _sample_edge_boot_rel_times(edges_jsonl_path)

    if not anomaly_windows:
        return meta_boots or [current_boot]

    clusters = _cluster_windows(anomaly_windows)
    chosen: List[float] = []
    seen = set()

    for cluster in clusters:
        c_start = min(w['start'] for w in cluster)
        c_end = max(w['end'] for w in cluster)
        candidates: List[float] = []
        candidates.extend(meta_boots)
        candidates.append(current_boot)
        if first_ts is not None:
            # Align first/last edge to cluster bounds (handles reboot after capture)
            candidates.append(c_start - first_ts)
            if last_ts is not None:
                candidates.append(c_end - last_ts)
                candidates.append((c_start + c_end) / 2.0 - (first_ts + last_ts) / 2.0)

        # Deduplicate roughly
        uniq: List[float] = []
        local_seen = set()
        for b in candidates:
            key = round(b, 2)
            if key not in local_seen:
                local_seen.add(key)
                uniq.append(b)

        if samples:
            best = max(uniq, key=lambda b: _boot_hit_score(b, cluster, samples))
            score = _boot_hit_score(best, cluster, samples)
            if score == 0 and first_ts is not None:
                # Fall back to first-edge alignment even if samples missed the short windows
                best = c_start - first_ts
                print(f"[!] No sampled edge hits for cluster "
                      f"[{c_start:.0f}..{c_end:.0f}]; using inferred boot {best:.2f}")
            elif abs(best - current_boot) > 1.0 and best not in meta_boots:
                print(f"[!] Aligned boot_time_epoch for cluster "
                      f"[{c_start:.0f}..{c_end:.0f}] -> {best:.2f} (hits={score})")
        else:
            best = meta_boots[0] if meta_boots else (c_start - first_ts if first_ts is not None else current_boot)

        key = round(best, 3)
        if key not in seen:
            seen.add(key)
            chosen.append(best)

    if not chosen:
        chosen = meta_boots or [current_boot]
    return chosen


def resolve_boot_time_epoch(edges_jsonl_path: str, anomaly_windows: List[Dict[str, Any]]) -> float:
    """Backward-compatible single boot resolver (returns the first resolved boot)."""
    boots = resolve_boot_times(edges_jsonl_path, anomaly_windows)
    return boots[0]


# reads jsonl file and matches edges into active windows
def extract_edges_for_windows(
    edges_jsonl_path: str,
    anomaly_windows: List[Dict[str, Any]],
    boot_time_epoch: float = None,
    filter_noise: bool = True,
    boot_times: List[float] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    window_edges: Dict[str, List[Dict[str, Any]]] = {w['window_id']: [] for w in anomaly_windows}

    if not os.path.exists(edges_jsonl_path):
        print(f"[-] Edges file '{edges_jsonl_path}' not found.")
        return window_edges

    if boot_times is None:
        if boot_time_epoch is not None:
            boot_times = [boot_time_epoch]
        else:
            boot_times = resolve_boot_times(edges_jsonl_path, anomaly_windows)

    with open(edges_jsonl_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or not line.startswith('{'):
                continue
            try:
                edge = json.loads(line)
            except json.JSONDecodeError:
                continue

            if filter_noise and is_noise_edge(edge):
                continue

            raw_ts = float(edge.get('timestamp', 0))
            boot_rel = edge_boot_relative_sec(raw_ts)

            matched = False
            for boot in boot_times:
                edge_epoch = boot + boot_rel if raw_ts > 1e11 else boot_rel
                # If timestamp was already wall-clock seconds, don't add boot
                if raw_ts <= 1e11:
                    edge_epoch = raw_ts
                for win in anomaly_windows:
                    if win['start'] <= edge_epoch <= win['end']:
                        # Shallow copy so multi-window / multi-boot doesn't share mutable state
                        tagged = dict(edge)
                        tagged['_epoch_sec'] = edge_epoch
                        window_edges[win['window_id']].append(tagged)
                        matched = True
                        break
                if matched:
                    break

    return window_edges


def _load_edge_data(path: str) -> List[Dict[str, Any]]:
    """Load one run's aggregated edge windows (data/edgeData_<run_id>.jsonl)."""
    records: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return records
    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and 'window_start' in rec and 'window_end' in rec:
                records.append(rec)
    return records


# Run edge files are small; cache parsed contents so several anomaly windows in
# the same run don't re-read the file.
_EDGE_DATA_CACHE: Dict[str, List[Dict[str, Any]]] = {}


def extract_edges_from_edge_data(
    edge_data_path: str,
    win: Dict[str, Any],
    filter_noise: bool = True,
) -> List[Dict[str, Any]]:
    """Aggregate comm-pair edge counts from one run's edge windows overlapping `win`."""
    if edge_data_path not in _EDGE_DATA_CACHE:
        _EDGE_DATA_CACHE[edge_data_path] = _load_edge_data(edge_data_path)

    pair_counts: Dict[Tuple[str, str], int] = {}
    for rec in _EDGE_DATA_CACHE[edge_data_path]:
        if rec['window_end'] < win['start'] or rec['window_start'] > win['end']:
            continue
        for entry in rec.get('edges', []):
            try:
                wakee, waker, count = str(entry[0]), str(entry[1]), int(entry[2])
            except (IndexError, TypeError, ValueError):
                continue
            edge = {'wakee_comm': wakee, 'waker_comm': waker, 'count': count}
            if filter_noise and is_noise_edge(edge):
                continue
            pair_counts[(wakee, waker)] = pair_counts.get((wakee, waker), 0) + count

    return [
        {'wakee_comm': key[0], 'waker_comm': key[1], 'count': count}
        for key, count in pair_counts.items()
    ]


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
    edges_file = sys.argv[2] if len(sys.argv) > 2 else 'data/edges.jsonl'
    summary_output_file = 'data/causal_summaries.json'

    anomaly_windows, df_scored = load_anomaly_windows(scored_features_file, padding_sec=1.0)
    if not anomaly_windows:
        return []

    # Prefer per-run aggregated edge files (data/edgeData_<run_id>.jsonl).
    # Legacy raw edges.jsonl remains supported as a fallback for old corpora.
    windows_needing_legacy = []
    for win in anomaly_windows:
        run_id = win.get('run_id')
        agg_path = f"data/edgeData_{run_id}.jsonl" if run_id else None
        if not agg_path or not os.path.exists(agg_path):
            windows_needing_legacy.append(win)

    legacy_edges_map: Dict[str, List[Dict[str, Any]]] = {}
    if windows_needing_legacy and os.path.exists(edges_file):
        boot_times = resolve_boot_times(edges_file, windows_needing_legacy)
        print(f"[+] Legacy edges.jsonl fallback for {len(windows_needing_legacy)} window(s) "
              f"({len(boot_times)} boot_time_epoch candidate(s))")
        legacy_edges_map = extract_edges_for_windows(
            edges_file, windows_needing_legacy, boot_times=boot_times, filter_noise=True
        )

    summaries = []
    graphs_data = {}

    for win in anomaly_windows:
        w_id = win['window_id']
        run_id = win.get('run_id')
        agg_path = f"data/edgeData_{run_id}.jsonl" if run_id else None
        if agg_path and os.path.exists(agg_path):
            edges = extract_edges_from_edge_data(agg_path, win, filter_noise=True)
        else:
            edges = legacy_edges_map.get(w_id, [])

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
