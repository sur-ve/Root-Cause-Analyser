#!/usr/bin/env python3
"""
rcaDaemon.py — two-phase background RCA monitor.

    sudo .venv/bin/python rcaDaemon.py --learn 300     # Phase A: learn normal, train model
    sudo .venv/bin/python rcaDaemon.py --monitor       # Phase B: continuous monitoring

Monitor cycle (every --interval seconds, default 30 s):
    1. read only the NEW probe windows / procfs samples appended since the
       previous cycle (byte offsets per run) and featurize them
    2. score with the persisted model (never refits)
    3. merge consecutive flagged windows into episodes and, per episode,
       build the causal graph + grounded report
    4. append one alert line per anomalous window to data/alerts.jsonl and
       the readable report to data/rca_report.txt (+ rca_report.json history)

A window counts as anomalous when --flag-fraction (default 50%) of its
~290 process-rows are flagged. The row-level "any" rule used by the offline
benchmarks would alert on every idle window at ~3% row-level FPR (baseline
windows peak at 0.44 flagged rows/window, fault windows are >= 0.94).

Stage invocation (plan 3.1): all stages run as *in-process function calls*
with explicit data paths — `run_id` is never passed where a stage expects a
file path. The `--no-collect` smoke test exercises every stage on existing
runs and logs one line per stage (featurize / score / graph / explain).

Modes:
    --learn SECONDS   collect SECONDS of (assumed normal) activity, append the
                      rows to --baseline-csv and train --model on it
    --monitor         continuous monitoring (default when no mode flag is given)
    --no-collect      analyse existing runs only (no root required); combine
                      with --runs a,b,c [--reprocess] for offline smoke tests

Housekeeping (plan 3.5): dead collectors are restarted as a new run, run files
older than --retain-hours are pruned (only files the daemon knows about, never
the batch corpus), and data/daemon_status.json carries a heartbeat. Shutdown
(SIGINT/SIGTERM) stops collectors, finalizes the active run and persists the
last-scored offsets (plan 3.7) — restarting never reprocesses old windows.

Model resolution: --model (default data/model/ocsvm_daemon.joblib), falling
back to the batch model data/model/ocsvm.joblib when no daemon model exists.
"""

import argparse
import datetime
import glob
import json
import os
import signal
import subprocess
import sys
import time
import traceback

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')

# Stage modules are imported in-process so the daemon can call their functions
# with explicit data paths (see "Stage invocation" above).
sys.path.insert(0, BASE_DIR)

import featureExtractor      # noqa: E402
import anomalyDetection      # noqa: E402
import analyzeCausalGraphs   # noqa: E402
import ragExplainer          # noqa: E402
import diagnosis             # noqa: E402

EDGE_TRACKER = 'wakeupTrackerMonitor.bt'
STATE_FILE = os.path.join(DATA_DIR, 'daemon_state.json')
STATUS_FILE = os.path.join(DATA_DIR, 'daemon_status.json')
ALERTS_FILE = os.path.join(DATA_DIR, 'alerts.jsonl')
REPORT_TXT = os.path.join(DATA_DIR, 'rca_report.txt')
REPORT_JSON = os.path.join(DATA_DIR, 'rca_report.json')
DAEMON_MODEL = os.path.join(DATA_DIR, 'model/ocsvm_daemon.joblib')
BATCH_MODEL = os.path.join(DATA_DIR, 'model/ocsvm.joblib')
DEFAULT_BASELINE_CSV = os.path.join(DATA_DIR, 'daemon_baseline.csv')
LOG_FILE = os.path.join(DATA_DIR, 'daemon.log')

MERGE_PAD_SEC = 1.0             # same window padding as analyzeCausalGraphs.py
FLAG_FRACTION_DEFAULT = 0.5     # window flagged iff >= this fraction of rows is flagged
COLLECTOR_GRACE_SEC = 15.0      # a run dying sooner than this = fast failure
MAX_FAST_FAILURES = 3
MAX_REPORTS_KEPT = 500
REPORT_TXT_MAX_BYTES = 2 * 1024 * 1024

_running = True
_shutdown_signals = 0


# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------

def log(msg: str):
    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{stamp}] [rcaDaemon] {msg}"
    print(line, flush=True)
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5 * 1024 * 1024:
            os.replace(LOG_FILE, LOG_FILE + '.1')
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def fmt_ts(ts) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError, OSError):
        return str(ts)


def atomic_json_dump(obj, path: str):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def dir_size_mb(path: str) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total / 1e6


def interruptible_sleep(seconds: float):
    end = time.time() + seconds
    while _running:
        remaining = end - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.5, remaining))


def make_run_id(prefix: str) -> str:
    return f"{prefix}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"


def handle_signal(signum, frame):
    global _running, _shutdown_signals
    _shutdown_signals += 1
    if _shutdown_signals >= 2:
        log(f"second signal ({signum}) — exiting immediately")
        os._exit(1)
    _running = False
    log(f"signal {signum} received — finishing this cycle, then shutting down")


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


# --------------------------------------------------------------------------
# state + status
# --------------------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            state = json.load(f)
    except Exception:
        state = {}
    state.setdefault('version', 1)
    state.setdefault('processed', {})        # run_id -> last processed window_end
    state.setdefault('last_run_id', None)
    state.setdefault('collector_pids', [])
    totals = state.setdefault('totals', {})
    for key in ('windows_scored', 'windows_flagged', 'alerts'):
        totals.setdefault(key, 0)
    return state


def save_state(state: dict):
    state['updated_at'] = time.time()
    try:
        atomic_json_dump(state, STATE_FILE)
    except Exception as e:
        log(f"WARN: could not save state: {e}")


# --------------------------------------------------------------------------
# collectors (procExtractor + probeExtractor + bpftrace edge tracker)
# --------------------------------------------------------------------------

def start_collectors(run_id: str, label: str = 'Unlabeled') -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    boot_time_epoch = time.time() - time.monotonic()
    meta = {
        'run_id': run_id,
        'label': label,               # rolling monitor runs carry no ground truth
        'boot_time_epoch': boot_time_epoch,
        'start_time': time.time(),
        'stressor_command': None,
    }
    for path in (os.path.join(DATA_DIR, f'meta_{run_id}.json'),
                 os.path.join(DATA_DIR, 'meta_latest.json')):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, indent=2)

    proc1 = subprocess.Popen([sys.executable, 'procExtractor.py', run_id])
    proc2 = subprocess.Popen([sys.executable, 'probeExtractor.py', run_id])
    edges_file = open(os.path.join(DATA_DIR, f'edges_{run_id}.jsonl'), 'w')
    proc3 = subprocess.Popen(
        ['stdbuf', '-oL', 'bpftrace', '-q', EDGE_TRACKER],
        stdout=edges_file,
        text=True,
    )
    coll = {
        'run_id': run_id,
        'started': time.time(),
        'procs': [('procExtractor', proc1), ('probeExtractor', proc2),
                  (EDGE_TRACKER, proc3)],
        'edges_file': edges_file,
    }
    log(f"collectors started for run {run_id} (pids: {[p.pid for _, p in coll['procs']]})")
    return coll


def _kill_if_ours(pid, needles) -> bool:
    """SIGTERM (+SIGKILL after 3 s) a pid whose /proc cmdline matches a needle."""
    try:
        pid = int(pid)
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            cmdline = f.read().decode('utf-8', 'replace')
    except (OSError, ValueError):
        return False
    if not any(n in cmdline for n in needles):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            time.sleep(0.1)
            os.kill(pid, 0)
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    return True


def kill_stray_bpftrace():
    """Kill bpftrace processes still attached to our scripts (crash leftovers)."""
    try:
        out = subprocess.run(['pgrep', '-af', 'bpftrace'],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return
    for line in out.splitlines():
        pid_str, _, cmd = line.partition(' ')
        if 'probes.bt' in cmd or 'wakeupTracker' in cmd:
            _kill_if_ours(pid_str, ('probes.bt', 'wakeupTracker'))


def stop_collectors(coll):
    if not coll:
        return
    log(f"stopping collectors for run {coll['run_id']}...")
    for _, p in coll['procs']:
        try:
            p.send_signal(signal.SIGINT)
        except (ProcessLookupError, OSError):
            pass
    for _, p in coll['procs']:
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    try:
        coll['edges_file'].close()
    except Exception:
        pass
    # probeExtractor signals its own bpftrace child on SIGINT; sweep anyway so
    # a hard-killed child can never linger (acceptance: no orphan processes).
    kill_stray_bpftrace()
    log("collectors stopped")


def cleanup_orphans(state: dict):
    """Reap collectors left behind by a previous daemon session (SIGKILL etc.)."""
    killed = 0
    for pid in state.get('collector_pids', []):
        if _kill_if_ours(pid, ('procExtractor.py', 'probeExtractor.py', EDGE_TRACKER,
                               'wakeupTracker.bt')):
            killed += 1
    if killed:
        log(f"cleaned up {killed} orphan collector process(es) from a previous session")
    kill_stray_bpftrace()
    state['collector_pids'] = []


# --------------------------------------------------------------------------
# model loading + scoring
# --------------------------------------------------------------------------

def resolve_model_path(opts) -> str:
    if os.path.exists(opts.model):
        return os.path.abspath(opts.model)
    if os.path.abspath(opts.model) == os.path.abspath(DAEMON_MODEL) \
            and os.path.exists(BATCH_MODEL):
        return os.path.abspath(BATCH_MODEL)
    return None


def score_rows(df: pd.DataFrame, artifact) -> pd.DataFrame:
    """In-memory scoring with the persisted scaler + OneClassSVM.

    Mirrors anomalyDetection.score_features (missing columns -> 0, extras
    ignored) without touching the batch CSVs.
    """
    feature_cols = artifact['feature_cols']
    X = pd.DataFrame(0.0, index=df.index, columns=feature_cols)
    for col in feature_cols:
        if col in df.columns:
            X[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
    X_scaled = artifact['scaler'].transform(X)
    df = df.copy()
    df['anomaly_score'] = artifact['model'].decision_function(X_scaled)
    df['predicted_anomaly'] = artifact['model'].predict(X_scaled) == -1
    return df


# --------------------------------------------------------------------------
# incremental per-run ingestion
# --------------------------------------------------------------------------

class RunIngestor:
    """Incremental reader/scorer for one run's probe + procfs files.

    Only lines appended since the previous call are parsed (byte offsets), the
    resulting windows are filtered against the last-processed offset from
    daemon_state.json, and consecutive flagged windows are merged into
    episodes exactly like analyzeCausalGraphs merges batch windows.
    """

    def __init__(self, run_id: str, start_after: float = 0.0,
                 flag_fraction: float = FLAG_FRACTION_DEFAULT):
        self.run_id = run_id
        self.flag_fraction = flag_fraction
        self.probe_path = os.path.join(DATA_DIR, f'probeData_{run_id}.jsonl')
        self.procfs_path = os.path.join(DATA_DIR, f'procfsData_{run_id}.jsonl')
        self.probe_off = 0
        self.procfs_off = 0
        self.probe_carry = ''
        self.procfs_carry = ''
        self.procfs_records = []
        self.rows = None                     # all scored rows of this run (in memory)
        self.episodes = []
        self.episode_seq = 0
        self.last_processed = float(start_after or 0.0)
        self.newest_window_start = 0.0
        self.windows_seen = 0

    @staticmethod
    def _read_lines(path, off, carry):
        if not os.path.exists(path):
            return [], off, carry
        with open(path, encoding='utf-8') as f:
            f.seek(off)
            chunk = f.read()
            new_off = f.tell()
        if not chunk:
            return [], new_off, carry
        lines = (carry + chunk).split('\n')
        carry = lines.pop()                  # keep the possibly-partial last line
        out = []
        for line in lines:
            line = line.strip()
            if not line.startswith('{'):
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out, new_off, carry

    def ingest(self, artifact):
        """Featurize + score windows appended since the previous call.

        Returns (new_windows, n_rows, n_flagged).
        """
        probe_records, self.probe_off, self.probe_carry = self._read_lines(
            self.probe_path, self.probe_off, self.probe_carry)
        procfs_records, self.procfs_off, self.procfs_carry = self._read_lines(
            self.procfs_path, self.procfs_off, self.procfs_carry)
        if procfs_records:
            self.procfs_records.extend(procfs_records)
        if not probe_records or not self.procfs_records:
            return 0, 0, 0

        df = featureExtractor.records_to_rows(self.run_id, probe_records,
                                              self.procfs_records)
        if df is None or df.empty:
            return 0, 0, 0

        df = df[df['window_end'] > self.last_processed]
        if df.empty:
            return 0, 0, 0

        df['root_cause'] = featureExtractor.label_for_run_id(self.run_id)
        df = score_rows(df, artifact)

        n_windows = int(df[['window_start', 'window_end']].drop_duplicates().shape[0])
        self.windows_seen += n_windows
        self.last_processed = max(self.last_processed, float(df['window_end'].max()))
        self.newest_window_start = max(self.newest_window_start,
                                       float(df['window_start'].max()))
        self.rows = df if self.rows is None else pd.concat([self.rows, df], ignore_index=True)

        # Window-level decision. The batch pipeline treats ANY flagged row as
        # an anomaly ("any" rule — fine for offline fault-window evaluation).
        # A live monitor must not alert on idle traffic: at ~3% row-level FPR,
        # every ~290-row window contains a few flagged rows. Measured on this
        # corpus, baseline windows peak at 0.44 flagged rows/window while
        # fault windows are >= 0.94, so the default majority rule separates
        # them cleanly (see --flag-fraction).
        per_window = (df.groupby(['window_start', 'window_end'])['predicted_anomaly']
                      .agg(['mean', 'count']))
        flagged_windows = per_window[per_window['mean'] >= self.flag_fraction]
        for (ws, we), _ in flagged_windows.iterrows():
            self._append_flagged(float(ws), float(we))
        return n_windows, len(df), int(len(flagged_windows))

    def _append_flagged(self, ws: float, we: float):
        # Same merge rule as analyzeCausalGraphs._merge_intervals: pad both
        # sides by MERGE_PAD_SEC and join overlapping intervals.
        if self.episodes and ws - MERGE_PAD_SEC <= self.episodes[-1]['end'] + MERGE_PAD_SEC:
            ep = self.episodes[-1]
            ep['end'] = max(ep['end'], we)
            ep['windows'].append((ws, we))
        else:
            self.episodes.append({'start': ws, 'end': we,
                                  'windows': [(ws, we)], 'finalized': False})

    def ready_episodes(self, force: bool = False):
        """Episodes that can no longer be extended by future windows."""
        return [ep for ep in self.episodes
                if not ep['finalized']
                and (force or self.newest_window_start - ep['end'] > 2 * MERGE_PAD_SEC)]


# --------------------------------------------------------------------------
# monitor
# --------------------------------------------------------------------------

class Monitor:
    def __init__(self, opts, state: dict, artifact, model_path: str):
        self.opts = opts
        self.state = state
        self.artifact = artifact
        self.model_path = model_path
        self.ingestors = {}
        self.totals = state['totals']
        self.cycle_no = 0
        self.collectors = None
        self.fast_failures = 0
        self.corpus = None
        self.reports = []
        self.session_start = time.time()
        self.last_cycle_sec = 0.0

    # ------------------------------------------------------------- ingestion

    def ingestor_for(self, run_id: str) -> RunIngestor:
        if run_id not in self.ingestors:
            start_after = self.state['processed'].get(run_id, 0.0)
            self.ingestors[run_id] = RunIngestor(
                run_id, start_after=start_after,
                flag_fraction=self.opts.flag_fraction)
        return self.ingestors[run_id]

    def process_run(self, run_id: str, force: bool = False):
        """One ingestion pass over a run; finalizes episodes that are complete."""
        ing = self.ingestor_for(run_id)
        # Re-read the run's edge file this cycle so newly appended edges are
        # visible (analyzeCausalGraphs caches parsed edge files per run).
        analyzeCausalGraphs._RAW_EDGE_CACHE.pop(f'data/edges_{run_id}.jsonl', None)

        n_windows, n_rows, n_flagged = ing.ingest(self.artifact)
        if n_windows:
            self.totals['windows_scored'] += n_windows
            self.totals['windows_flagged'] += n_flagged
            self.state['processed'][run_id] = ing.last_processed
            log(f"stage featurize ok: {run_id} +{n_windows} windows -> {n_rows} rows")
            log(f"stage score ok: {n_flagged}/{n_windows} windows flagged "
                f"(model={os.path.basename(self.model_path)})")

        for ep in ing.ready_episodes(force=force):
            self.finalize_episode(run_id, ep)
        return n_windows, n_flagged

    def drop_ingestor(self, run_id: str):
        self.ingestors.pop(run_id, None)
        analyzeCausalGraphs._RAW_EDGE_CACHE.pop(f'data/edges_{run_id}.jsonl', None)
        save_state(self.state)

    # -------------------------------------------------------------- reporting

    def finalize_episode(self, run_id: str, ep: dict):
        ing = self.ingestors[run_id]
        ing.episode_seq += 1
        ep_id = f"{run_id}_w{ing.episode_seq}"
        try:
            win_meta = {'window_id': ep_id, 'run_id': run_id,
                        'start': ep['start'], 'end': ep['end']}
            edges = analyzeCausalGraphs.edges_for_window(run_id, win_meta)
            rows = ing.rows
            w_df = rows[(rows['window_start'] >= ep['start'])
                        & (rows['window_end'] <= ep['end'])]
            graph, summary = analyzeCausalGraphs.analyze_anomaly_graph(
                ep_id, win_meta, edges, w_df)
            log(f"stage graph ok: {ep_id} nodes={graph.number_of_nodes()} "
                f"edges={graph.number_of_edges()} "
                f"window={fmt_ts(ep['start'])} -> {fmt_ts(ep['end'])}")

            if self.corpus is None:
                self.corpus = ragExplainer.KnowledgeCorpus()
            query = ragExplainer.build_retrieval_query(summary)
            docs = self.corpus.retrieve(query, top_k=2) if query.strip() else []
            report_text = ragExplainer.synthesize_grounded_report(summary, docs)
            diag = diagnosis.classify_cause(summary.get('top_metrics', {}),
                                            summary.get('top_processes', []))
            log(f"stage explain ok: {ep_id} family={diag['family']} "
                f"docs={','.join(d['id'] for d in docs) if docs else '-'}")

            self.write_report(ep_id, summary, report_text, docs)
            self.emit_alerts(run_id, ep, summary, report_text)
            if self.opts.notify:
                self.notify(summary)
        except Exception:
            log(f"ERROR while finalizing {ep_id}; marking done to avoid a poison episode:")
            log(traceback.format_exc())
        ep['finalized'] = True

    def write_report(self, ep_id: str, summary: dict, report_text: str, docs):
        header = (f"\n{'=' * 72}\n"
                  f"[{fmt_ts(time.time())}] daemon alert {ep_id}\n"
                  f"{'=' * 72}\n")
        try:
            if os.path.exists(REPORT_TXT) and os.path.getsize(REPORT_TXT) > REPORT_TXT_MAX_BYTES:
                os.replace(REPORT_TXT, REPORT_TXT + '.1')
            with open(REPORT_TXT, 'a', encoding='utf-8') as f:
                f.write(header + report_text)
        except Exception as e:
            log(f"WARN: could not append {REPORT_TXT}: {e}")

        self.reports.append({
            'window_id': ep_id,
            'run_id': summary.get('run_id'),
            'start': summary.get('start'),
            'end': summary.get('end'),
            'report': report_text,
            'retrieved_docs': [d['id'] for d in docs],
        })
        self.reports = self.reports[-MAX_REPORTS_KEPT:]
        try:
            atomic_json_dump(self.reports, REPORT_JSON)
        except Exception as e:
            log(f"WARN: could not write {REPORT_JSON}: {e}")

    def emit_alerts(self, run_id: str, ep: dict, summary: dict, report_text: str):
        """One alert line per anomalous window (plan 3.4)."""
        ing = self.ingestors[run_id]
        rows = ing.rows

        remediation = 'Investigate system logs and process resource limits.'
        for line in report_text.splitlines():
            if line.startswith('Suggested action:'):
                remediation = line.split('Suggested action:', 1)[1].strip()
                break

        roots = summary.get('candidate_roots') or []
        root = roots[0] if roots else ''
        alert_ts = time.time()
        count = 0
        try:
            with open(ALERTS_FILE, 'a', encoding='utf-8') as f:
                for (ws, we) in ep['windows']:
                    wrows = rows[(rows['window_start'] == ws) & (rows['window_end'] == we)]
                    wm = diagnosis.aggregate_window_metrics(wrows)
                    comms = diagnosis.top_process_comms(wrows)
                    diag = diagnosis.classify_cause(wm, comms)
                    culprit = comms[0] if comms else (root.split(':')[0] if root else '')
                    f.write(json.dumps({
                        'timestamp': alert_ts,
                        'time_human': fmt_ts(alert_ts),
                        'run_id': run_id,
                        'episode_id': summary.get('window_id'),
                        'window_start': ws,
                        'window_end': we,
                        'window_time_human': f"{fmt_ts(ws)} -> {fmt_ts(we)}",
                        'cause': diag['cause'],
                        'cause_family': diag['family'],
                        'culprit': culprit,
                        'root': root,
                        'remediation': remediation,
                        'basis': diag['basis'],
                    }) + '\n')
                    count += 1
        except Exception as e:
            log(f"WARN: could not append {ALERTS_FILE}: {e}")
            return
        self.totals['alerts'] += count
        log(f"alerts: {count} line(s) appended for {summary.get('window_id')}")

    def notify(self, summary: dict):
        text = (f"{summary.get('window_id')}: anomaly episode "
                f"{fmt_ts(summary.get('start'))} -> {fmt_ts(summary.get('end'))}")
        user = os.environ.get('SUDO_USER')
        try:
            if user:
                subprocess.run(['sudo', '-u', user, 'notify-send', '-u', 'critical',
                                'RCA alert', text], timeout=10, capture_output=True)
            else:
                subprocess.run(['notify-send', '-u', 'critical', 'RCA alert', text],
                               timeout=10, capture_output=True)
        except Exception:
            pass

    # ------------------------------------------------------------ housekeeping

    def write_status(self, mode: str):
        status = {
            'pid': os.getpid(),
            'mode': mode,
            'started_at': self.session_start,
            'started_at_human': fmt_ts(self.session_start),
            'updated_at': time.time(),
            'updated_at_human': fmt_ts(time.time()),
            'cycle': self.cycle_no,
            'interval_sec': self.opts.interval,
            'rotate_sec': self.opts.rotate,
            'model': self.model_path,
            'active_run': self.collectors['run_id'] if self.collectors else None,
            'collectors_alive': (all(p.poll() is None for _, p in self.collectors['procs'])
                                 if self.collectors else None),
            'collector_pids': ([p.pid for _, p in self.collectors['procs']]
                               if self.collectors else []),
            'windows_scored': self.totals['windows_scored'],
            'windows_flagged': self.totals['windows_flagged'],
            'alerts': self.totals['alerts'],
            'last_cycle_sec': round(self.last_cycle_sec, 2),
            'data_dir_mb': round(dir_size_mb(DATA_DIR), 1),
        }
        try:
            atomic_json_dump(status, STATUS_FILE)
        except Exception as e:
            log(f"WARN: could not write status: {e}")

    def prune_retention(self):
        """Delete run files older than --retain-hours (daemon-created runs only)."""
        if self.opts.retain_hours <= 0:
            return
        cutoff = time.time() - self.opts.retain_hours * 3600
        active = self.collectors['run_id'] if self.collectors else None
        removed = 0
        for run_id in list(self.state['processed'].keys()):
            if run_id == active:
                continue
            files = [os.path.join(DATA_DIR, f'{kind}_{run_id}{ext}')
                     for kind, ext in (('probeData', '.jsonl'), ('procfsData', '.jsonl'),
                                       ('edges', '.jsonl'))]
            files.append(os.path.join(DATA_DIR, f'meta_{run_id}.json'))
            existing = [p for p in files if os.path.exists(p)]
            if not existing:
                self.state['processed'].pop(run_id, None)
                continue
            if all(os.path.getmtime(p) < cutoff for p in existing):
                for p in existing:
                    try:
                        os.remove(p)
                        removed += 1
                    except OSError:
                        pass
                self.state['processed'].pop(run_id, None)
        if removed:
            log(f"retention: removed {removed} file(s) older than {self.opts.retain_hours:g}h")

    # --------------------------------------------------------- run management

    def start_new_run(self):
        run_id = make_run_id('rolling')
        self.collectors = start_collectors(run_id, label='Unlabeled')
        self.state['last_run_id'] = run_id
        self.state['collector_pids'] = [p.pid for _, p in self.collectors['procs']]
        save_state(self.state)
        return run_id

    def check_collectors(self):
        if not self.collectors:
            return
        dead = [name for name, p in self.collectors['procs'] if p.poll() is not None]
        if not dead:
            return
        run_id = self.collectors['run_id']
        uptime = time.time() - self.collectors['started']
        log(f"collector(s) exited unexpectedly: {dead} — restarting collection")
        stop_collectors(self.collectors)
        self.collectors = None
        self.state['collector_pids'] = []
        self.process_run(run_id, force=True)
        self.drop_ingestor(run_id)
        self.fast_failures = self.fast_failures + 1 if uptime < COLLECTOR_GRACE_SEC else 0
        if self.fast_failures >= MAX_FAST_FAILURES:
            log(f"collectors failed {self.fast_failures}x right after start — aborting")
            raise SystemExit(1)
        self.start_new_run()

    def rotate_if_needed(self):
        if not self.collectors:
            return
        if time.time() - self.collectors['started'] < self.opts.rotate:
            return
        run_id = self.collectors['run_id']
        log(f"rotation: run {run_id} reached {self.opts.rotate}s — starting a new run")
        stop_collectors(self.collectors)
        self.collectors = None
        self.state['collector_pids'] = []
        self.process_run(run_id, force=True)
        self.drop_ingestor(run_id)
        self.start_new_run()

    # ------------------------------------------------------------------ modes

    def cycle(self):
        t0 = time.time()
        self.cycle_no += 1
        self.check_collectors()
        if self.collectors:
            self.process_run(self.collectors['run_id'], force=False)
        self.rotate_if_needed()
        if self.cycle_no % 10 == 1:
            self.prune_retention()
        self.last_cycle_sec = time.time() - t0
        self.write_status('monitor')
        save_state(self.state)
        log(f"cycle {self.cycle_no} done in {self.last_cycle_sec:.2f}s "
            f"(windows={self.totals['windows_scored']} flagged={self.totals['windows_flagged']} "
            f"alerts={self.totals['alerts']})")

    def run_monitor(self):
        # Finish any runs a previous session left unprocessed (crash recovery).
        for run_id in list(self.state['processed'].keys()):
            n_windows, _ = self.process_run(run_id, force=True)
            self.drop_ingestor(run_id)
            if n_windows:
                log(f"recovered {n_windows} unprocessed window(s) from previous run {run_id}")

        self.start_new_run()
        log(f"monitor started: interval={self.opts.interval}s rotate={self.opts.rotate}s "
            f"retain={self.opts.retain_hours:g}h model={os.path.basename(self.model_path)} "
            f"(Ctrl+C to stop)")
        try:
            while _running and (self.opts.max_cycles is None
                                or self.cycle_no < self.opts.max_cycles):
                if self.cycle_no:
                    interruptible_sleep(self.opts.interval)
                    if not _running:
                        break
                self.cycle()
        finally:
            self.shutdown()

    def shutdown(self):
        log("shutting down: stopping collectors and finalizing the active run")
        if self.collectors:
            run_id = self.collectors['run_id']
            stop_collectors(self.collectors)
            self.collectors = None
            self.state['collector_pids'] = []
            self.process_run(run_id, force=True)
            self.drop_ingestor(run_id)
        save_state(self.state)
        self.write_status('monitor')
        log(f"monitor stopped: cycles={self.cycle_no} "
            f"windows={self.totals['windows_scored']} "
            f"flagged={self.totals['windows_flagged']} alerts={self.totals['alerts']}")

    def run_once(self, run_ids, reprocess: bool = False):
        """Offline pass over existing runs (--no-collect smoke test)."""
        if not run_ids:
            log("no runs to process (use --runs or collect data first)")
            return
        if reprocess:
            for run_id in run_ids:
                self.state['processed'].pop(run_id, None)
        for run_id in run_ids:
            log(f"stage run started: {run_id}")
            n_windows, n_flagged = self.process_run(run_id, force=True)
            self.drop_ingestor(run_id)
            if not n_windows:
                log(f"no new windows for {run_id} (already processed; use --reprocess to redo)")
        save_state(self.state)
        self.write_status('no-collect')
        log(f"offline pass complete: runs={len(run_ids)} "
            f"windows={self.totals['windows_scored']} "
            f"flagged={self.totals['windows_flagged']} alerts={self.totals['alerts']}")


def discover_runs():
    runs = set()
    for path in glob.glob(os.path.join(DATA_DIR, 'probeData_*.jsonl')):
        runs.add(os.path.basename(path)[len('probeData_'):-len('.jsonl')])
    return runs


# --------------------------------------------------------------------------
# learn mode
# --------------------------------------------------------------------------

def run_learn(opts, state: dict):
    if os.geteuid() != 0:
        log("ERROR: --learn needs root (sudo) to start the bpftrace collectors")
        sys.exit(1)
    run_id = make_run_id('learn')
    log(f"learn: collecting ~{opts.learn}s of (assumed normal) activity as {run_id}")
    coll = start_collectors(run_id, label='Normal')
    state['collector_pids'] = [p.pid for _, p in coll['procs']]
    save_state(state)
    atomic_json_dump({
        'pid': os.getpid(), 'mode': 'learn', 'phase': 'collecting',
        'active_run': run_id, 'learn_seconds': opts.learn,
        'collector_pids': state['collector_pids'],
        'updated_at': time.time(), 'updated_at_human': fmt_ts(time.time()),
    }, STATUS_FILE)

    deadline = time.time() + opts.learn
    try:
        while _running and time.time() < deadline:
            if any(p.poll() is not None for _, p in coll['procs']):
                log("ERROR: a collector exited during --learn")
                break
            time.sleep(1)
    finally:
        stop_collectors(coll)
        state['collector_pids'] = []

    df = featureExtractor.merge_pair(run_id)
    if df is None or df.empty:
        log("ERROR: no features were collected during --learn")
        sys.exit(1)
    baseline = opts.baseline_csv
    df.to_csv(baseline, mode='a', header=not os.path.exists(baseline), index=False)
    log(f"learn: appended {len(df)} Normal rows to {baseline}")

    artifact = anomalyDetection.train_model(baseline, opts.model)
    state['processed'][run_id] = float(df['window_end'].max())
    save_state(state)
    atomic_json_dump({
        'pid': os.getpid(), 'mode': 'learn', 'phase': 'done',
        'active_run': run_id, 'model': opts.model,
        'n_train_rows': artifact['n_train_rows'],
        'updated_at': time.time(), 'updated_at_human': fmt_ts(time.time()),
    }, STATUS_FILE)
    log(f"learn complete: model -> {opts.model} "
        f"({artifact['n_train_rows']} Normal rows, {len(artifact['feature_cols'])} features)")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='rcaDaemon — two-phase background RCA monitor',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument('--learn', type=int, metavar='SECONDS', default=None,
                        help='collect SECONDS of normal activity, append to the baseline '
                             'CSV, train --model, then exit')
    parser.add_argument('--monitor', action='store_true',
                        help='continuous monitoring mode (default when no mode flag given)')
    parser.add_argument('--no-collect', action='store_true',
                        help='analyse existing runs only (no collectors, no root needed)')
    parser.add_argument('--runs', type=str, default='',
                        help='comma-separated run_ids for --no-collect '
                             '(default: runs from state, else all discovered)')
    parser.add_argument('--reprocess', action='store_true',
                        help='with --no-collect: ignore saved offsets and re-analyse the runs')
    parser.add_argument('--model', type=str, default=DAEMON_MODEL,
                        help=f'model artifact (default: {DAEMON_MODEL}, '
                             f'fallback: {BATCH_MODEL})')
    parser.add_argument('--baseline-csv', type=str, default=DEFAULT_BASELINE_CSV,
                        help=f'learning corpus for --learn (default: {DEFAULT_BASELINE_CSV})')
    parser.add_argument('--interval', type=int, default=30,
                        help='analysis cycle interval in seconds (default: 30)')
    parser.add_argument('--rotate', type=int, default=900,
                        help='rotate collector run files every N seconds (default: 900)')
    parser.add_argument('--retain-hours', type=float, default=24.0,
                        help='delete daemon run files older than N hours (default: 24)')
    parser.add_argument('--flag-fraction', type=float, default=FLAG_FRACTION_DEFAULT,
                        help='a window is anomalous iff at least this fraction of its '
                             f'rows is flagged (default: {FLAG_FRACTION_DEFAULT}; keep the '
                             'batch pipeline\'s "any" rule for offline benchmarks)')
    parser.add_argument('--max-cycles', type=int, default=None,
                        help='exit after N cycles (testing)')
    parser.add_argument('--notify', action='store_true',
                        help='send a desktop notification per alert episode')
    opts = parser.parse_args()

    os.chdir(BASE_DIR)                       # stage modules use relative data/ paths
    os.makedirs(DATA_DIR, exist_ok=True)
    state = load_state()

    if os.geteuid() == 0:
        cleanup_orphans(state)               # reap crash leftovers before any collection

    if opts.learn is not None:
        run_learn(opts, state)
        return

    if not opts.no_collect and os.geteuid() != 0:
        print("[rcaDaemon] ERROR: live collection needs root: "
              "sudo .venv/bin/python rcaDaemon.py --monitor")
        sys.exit(1)

    model_path = resolve_model_path(opts)
    if model_path is None:
        log(f"ERROR: no model found at '{opts.model}' (and no batch model at "
            f"'{BATCH_MODEL}'). Run '--learn N' first.")
        sys.exit(1)
    artifact = anomalyDetection.load_artifact(model_path)
    if artifact is None:
        log(f"ERROR: could not load model '{model_path}'")
        sys.exit(1)
    log(f"model ready: {model_path} "
        f"({len(artifact.get('feature_cols', []))} features, "
        f"{artifact.get('n_train_rows')} training rows)")

    monitor = Monitor(opts, state, artifact, model_path)

    if opts.no_collect:
        run_ids = [r.strip() for r in opts.runs.split(',') if r.strip()]
        if not run_ids:
            run_ids = sorted(state['processed'].keys()) or sorted(discover_runs())
        monitor.run_once(run_ids, reprocess=opts.reprocess)
    else:
        monitor.run_monitor()


if __name__ == '__main__':
    main()
