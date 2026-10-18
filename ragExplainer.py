import os
import sys
import glob
import json
import urllib.request
import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from typing import List, Dict, Any, Optional

KNOWLEDGE_DIR = 'knowledge'
CAUSAL_SUMMARIES_FILE = 'data/causal_summaries.json'
SCORED_FEATURES_FILE = 'data/scored_features.csv'
REPORT_TXT_FILE = 'data/rca_report.txt'
REPORT_JSON_FILE = 'data/rca_report.json'

class KnowledgeCorpus:
    def __init__(self, knowledge_dir: str = KNOWLEDGE_DIR):
        self.knowledge_dir = knowledge_dir
        self.documents: List[Dict[str, str]] = []
        self.vectorizer: Optional[TfidfVectorizer] = None
        self.doc_vectors = None
        self._load_and_index()

    def _load_and_index(self):
        if not os.path.exists(self.knowledge_dir):
            os.makedirs(self.knowledge_dir, exist_ok=True)

        for path in glob.glob(os.path.join(self.knowledge_dir, '*.md')):
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
                name = os.path.splitext(os.path.basename(path))[0]
                self.documents.append({
                    'id': name,
                    'path': path,
                    'content': content
                })

        if self.documents:
            texts = [d['content'] for d in self.documents]
            self.vectorizer = TfidfVectorizer(stop_words='english')
            self.doc_vectors = self.vectorizer.fit_transform(texts)
            print(f"[+] Indexed {len(self.documents)} knowledge documents in '{self.knowledge_dir}'.")
        else:
            print(f"[!] Warning: No knowledge documents found in '{self.knowledge_dir}'.")

    def retrieve(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        if not self.documents or self.vectorizer is None or self.doc_vectors is None:
            return []

        query_vec = self.vectorizer.transform([query])
        sims = cosine_similarity(query_vec, self.doc_vectors)[0]
        ranked_indices = np.argsort(sims)[::-1][:top_k]

        results = []
        for idx in ranked_indices:
            score = float(sims[idx])
            results.append({
                'id': self.documents[idx]['id'],
                'path': self.documents[idx]['path'],
                'content': self.documents[idx]['content'],
                'score': score
            })
        return results

# --------------------------------------------------------------------------
# Optional online LLM (OpenAI-compatible chat completions API)
#
# Enabled by setting RCA_LLM_API_KEY. Works with OpenAI, Groq, OpenRouter,
# Together, etc. — any provider exposing /chat/completions:
#
#   export RCA_LLM_API_KEY=sk-...
#   export RCA_LLM_BASE_URL=https://api.groq.com/openai/v1   # optional
#   export RCA_LLM_MODEL=llama-3.3-70b-versatile             # optional
#
# No local installation or model download is needed.
# --------------------------------------------------------------------------

LLM_BASE_URL_DEFAULT = 'https://api.openai.com/v1'
LLM_MODEL_DEFAULT = 'gpt-4o-mini'

LLM_SYSTEM_PROMPT = (
    "You are a Linux performance root-cause analysis assistant. "
    "Use ONLY the evidence given in the user message. Do not invent metrics, "
    "process names, or runbook content. If the evidence is insufficient, say "
    "so explicitly. Reply with at most three short sections: "
    "Likely root cause, Key evidence, Suggested action."
)


def query_online_llm(prompt: str) -> Optional[str]:
    """Ask an OpenAI-compatible /chat/completions API.

    Returns None when disabled (RCA_LLM_API_KEY unset) or when the call fails
    for any reason, so callers can fall back to the grounded template report.
    """
    api_key = os.environ.get('RCA_LLM_API_KEY', '').strip()
    if not api_key:
        return None

    base_url = os.environ.get('RCA_LLM_BASE_URL', LLM_BASE_URL_DEFAULT).rstrip('/')
    model = os.environ.get('RCA_LLM_MODEL', LLM_MODEL_DEFAULT)
    payload = json.dumps({
        'model': model,
        'messages': [
            {'role': 'system', 'content': LLM_SYSTEM_PROMPT},
            {'role': 'user', 'content': prompt},
        ],
        'temperature': 0.2,
    }).encode('utf-8')
    req = urllib.request.Request(
        f'{base_url}/chat/completions',
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return data['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f"[!] Online LLM call failed ({type(e).__name__}: {e}) — using grounded template only.")
        return None

LABEL_TO_DOC_ID = {
    'CPU Competition':            'cpu_competition',
    'Process Throttled':          'cgroup_throttling',
    'Baseline CPU Usage':         'cpu_competition',
    'Memory Swapping':            'memory_pressure',
    'OOM Kill':                   'oom',
    'Continuous Heap Expansion':  'heap_expansion',
    'Userspace Contention':       'lock_contention',
    'Kernel-Internal Contention': 'lock_contention',
    'Small Random Writes':        'io_saturation',
    'Large Sequential Writes':    'io_saturation',
}

LABEL_TO_CAUSE = {
    'CPU Competition':            'CPU Competition / Scheduling Starvation',
    'Process Throttled':          'CPU Quota Throttling (cgroup)',
    'Baseline CPU Usage':         'Moderate CPU Load',
    'Memory Swapping':            'Memory Pressure & Swapping',
    'OOM Kill':                   'OOM Kill (Memory Exhaustion)',
    'Continuous Heap Expansion':  'Continuous Heap Expansion (brk)',
    'Userspace Contention':       'Userspace Lock Contention (Futex)',
    'Kernel-Internal Contention': 'Kernel VFS / Lock Contention',
    'Small Random Writes':        'Disk I/O Saturation (Random Writes)',
    'Large Sequential Writes':    'Disk I/O Saturation (Sequential Writes)',
}

def build_retrieval_query(window: Dict[str, Any]) -> str:
    tokens = []

    # Labeled causes are the strongest semantic signal — weight them first
    for cause in window.get('labeled_causes', []):
        doc_key = LABEL_TO_DOC_ID.get(cause, '')
        if doc_key:
            tokens.extend([cause, doc_key.replace('_', ' ')])
        else:
            tokens.append(cause)

    # Candidate root process names
    for r in window.get('candidate_roots', [])[:3]:
        comm = r.split(':')[0]
        tokens.append(comm)

    # Sample path textual hints
    for p in window.get('sample_paths', [])[:2]:
        tokens.append(p.replace('->', ' '))

    # Supplement with metric-derived keywords only when labels are absent
    top_metrics = window.get('top_metrics', {})
    if not window.get('labeled_causes'):
        if top_metrics.get('oom_kill_count', 0) > 0:
            tokens.extend(['oom', 'out of memory', 'kill', 'memory exhaustion'])
        elif top_metrics.get('swap_in_count', 0) > 100 or top_metrics.get('swap_out_count', 0) > 100:
            tokens.extend(['memory pressure', 'swapping', 'thrashing', 'allocstall'])
        elif top_metrics.get('sector_count', 0) > 5000 or top_metrics.get('iowait_time', 0) > 0:
            tokens.extend(['io saturation', 'disk writes', 'iowait', 'storage'])
        elif top_metrics.get('futex_wait_count', 0) > 500:
            tokens.extend(['futex', 'lock', 'userspace contention'])
        elif top_metrics.get('locktime', 0) > 1e6:
            tokens.extend(['lock', 'kernel contention', 'dentry', 'vfs'])
        else:
            tokens.extend(['cpu competition', 'runqueue wait', 'scheduling'])

    return " ".join(tokens)

def synthesize_grounded_report(window: Dict[str, Any], retrieved_docs: List[Dict[str, Any]]) -> str:
    win_id = window.get('window_id', 'unknown')
    roots = window.get('candidate_roots', [])
    top_root = roots[0] if roots else "Unidentified Process"
    culprit_comm = top_root.split(':')[0]
    sample_paths = window.get('sample_paths', [])
    metrics = window.get('top_metrics', {})
    labeled = window.get('labeled_causes', [])

    evidence_points = []
    remediation = "Investigate system logs and process resource limits."

    # --- Classification: labeled_causes first (ground-truth from stressor), then metric heuristics ---
    if labeled:
        primary_label = labeled[0]
        classified_cause = LABEL_TO_CAUSE.get(primary_label, primary_label)
        # Optionally prefer retrieved doc remediation
    elif metrics.get('oom_kill_count', 0) > 0:
        classified_cause = "OOM Kill (Memory Exhaustion)"
    elif metrics.get('swap_in_count', 0) > 100 or metrics.get('swap_out_count', 0) > 100:
        classified_cause = "Memory Pressure & Swapping"
    elif metrics.get('sector_count', 0) > 5000 and not metrics.get('iowait_time'):
        classified_cause = "Disk I/O Saturation (Sequential Writes)"
    elif metrics.get('iowait_time', 0) > 0:
        classified_cause = "Disk I/O Saturation (Random Writes)"
    elif metrics.get('futex_wait_count', 0) > 1000:
        classified_cause = "Userspace Lock Contention (Futex)"
    elif metrics.get('locktime', 0) > 1e6:
        classified_cause = "Kernel VFS / Lock Contention"
    elif metrics.get('cpu_some_avg10', 0) > 50:
        classified_cause = "CPU Competition / Starvation"
    elif retrieved_docs:
        classified_cause = retrieved_docs[0]['id'].replace('_', ' ').title()
    else:
        classified_cause = "System Anomaly (Unclassified)"

    # --- Collect metric evidence (all relevant, regardless of classification) ---
    if metrics.get('oom_kill_count', 0) > 0:
        evidence_points.append(f"Kernel vmstat reported {int(metrics['oom_kill_count'])} OOM kills.")
        remediation = "Set strict memory limits via cgroups (`memory.max`) and profile process memory consumption."
    if metrics.get('swap_in_count', 0) > 0 or metrics.get('swap_out_count', 0) > 0:
        evidence_points.append(
            f"Swap activity: {int(metrics.get('swap_in_count',0))} pswpin / {int(metrics.get('swap_out_count',0))} pswpout."
        )
    if metrics.get('major_fault_count', 0) > 0:
        evidence_points.append(f"Major page faults: {int(metrics['major_fault_count'])}.")
    if metrics.get('allocstall_count', 0) > 0:
        evidence_points.append(f"Direct-reclaim stalls: {int(metrics['allocstall_count'])}.")
    if metrics.get('sector_count', 0) > 0:
        evidence_points.append(f"Block I/O: {int(metrics['sector_count'])} sectors completed.")
    if metrics.get('futex_wait_count', 0) > 0:
        evidence_points.append(
            f"Futex waits: {int(metrics['futex_wait_count'])} calls, {metrics.get('futex_wait_time',0):.0f} ns total."
        )
    if metrics.get('locktime', 0) > 0:
        evidence_points.append(f"Kernel lock hold time: {metrics['locktime']:.0f} ns.")
    if metrics.get('cpu_some_avg10', 0) > 0:
        evidence_points.append(f"PSI CPU pressure avg10 = {metrics['cpu_some_avg10']}%.")
    if metrics.get('mem_some_avg10', 0) > 0:
        evidence_points.append(f"PSI Memory pressure avg10 = {metrics['mem_some_avg10']}%.")
    if metrics.get('io_some_avg10', 0) > 0:
        evidence_points.append(f"PSI I/O pressure avg10 = {metrics['io_some_avg10']}%.")
    if metrics.get('runtime', 0) > 0:
        evidence_points.append(f"Peak task scheduled runtime: {metrics['runtime']:.0f} ns.")

    # --- Graph evidence ---
    if roots:
        evidence_points.append(
            f"Causal graph terminal blocker: `{top_root}` (most-blocked root in window)."
        )
    if sample_paths:
        evidence_points.append(f"Causal path: {sample_paths[0]}")

    # --- Knowledge retrieval evidence + remediation from runbook ---
    if retrieved_docs:
        best_doc = retrieved_docs[0]
        evidence_points.append(f"Matched knowledge runbook '{best_doc['id']}' (similarity: {best_doc['score']:.2f}).")
        if '## Remediation' in best_doc['content']:
            rem_section = best_doc['content'].split('## Remediation')[-1].strip()
            bullets = [ln.strip().lstrip('-* ') for ln in rem_section.split('\n') if ln.strip().startswith(('-', '*'))]
            if bullets:
                remediation = bullets[0]

    report = []
    report.append(f"=== RCA Report: {win_id} ===")
    report.append(
        f"Time Range: {window.get('start', 0):.2f} -> {window.get('end', 0):.2f}"
        f"  (Duration: {window.get('duration_sec', 0)}s)"
    )
    report.append(f"Likely root cause: {classified_cause}")
    if culprit_comm and culprit_comm != "Unidentified Process":
        report.append(f"Primary culprit process: {culprit_comm}")
    report.append("Evidence:")
    for ev in evidence_points:
        report.append(f"  - {ev}")
    report.append(f"Suggested action: {remediation}")
    report.append("")
    return "\n".join(report)


def explain_anomalies(causal_summaries_file: str = CAUSAL_SUMMARIES_FILE, knowledge_dir: str = KNOWLEDGE_DIR) -> List[Dict[str, Any]]:
    if not os.path.exists(causal_summaries_file):
        print(f"[-] Causal summaries file '{causal_summaries_file}' not found. Run analyzeCausalGraphs.py first.")
        return []

    with open(causal_summaries_file, 'r') as f:
        windows = json.load(f)

    if not windows:
        print("[-] No anomaly windows in causal summaries.")
        return []

    corpus = KnowledgeCorpus(knowledge_dir)
    if os.environ.get('RCA_LLM_API_KEY', '').strip():
        print(f"[i] Online LLM enabled: {os.environ.get('RCA_LLM_MODEL', LLM_MODEL_DEFAULT)} "
              f"@ {os.environ.get('RCA_LLM_BASE_URL', LLM_BASE_URL_DEFAULT)}")
    else:
        print("[i] Online LLM disabled (set RCA_LLM_API_KEY to enable; template report only).")
    reports = []
    full_text_report = []

    print(f"\n=======================================================")
    print(f"       ROOT CAUSE ANALYSIS (RAG EXPLAINER)            ")
    print(f"=======================================================\n")

    for win in windows:
        # If window had no edges and no candidate roots, but had anomaly flags:
        query = build_retrieval_query(win)
        retrieved_docs = corpus.retrieve(query, top_k=2) if query.strip() else []

        report_text = synthesize_grounded_report(win, retrieved_docs)

        # Optional online LLM review (skipped when RCA_LLM_API_KEY is unset)
        llm_response = None
        if os.environ.get('RCA_LLM_API_KEY', '').strip():
            prompt = (
                "Below is an evidence-grounded RCA draft produced by rule-based analysis "
                "of kernel telemetry for ONE anomaly window. Review it strictly against "
                "the evidence it cites.\n\n"
                f"{report_text}\n"
                "State the most likely root cause and the single most informative piece "
                "of evidence for it. If the draft is not supported by the evidence, say "
                "what is missing instead of guessing."
            )
            llm_response = query_online_llm(prompt)

        if llm_response:
            report_text += f"\nLLM review (online API):\n{llm_response}\n"

        print(report_text)
        full_text_report.append(report_text)
        reports.append({
            'window_id': win.get('window_id'),
            'start': win.get('start'),
            'end': win.get('end'),
            'report': report_text,
            'retrieved_docs': [d['id'] for d in retrieved_docs],
            'llm_review': llm_response,
        })

    os.makedirs('data', exist_ok=True)
    with open(REPORT_TXT_FILE, 'w', encoding='utf-8') as f:
        f.write("\n\n".join(full_text_report))
    with open(REPORT_JSON_FILE, 'w', encoding='utf-8') as f:
        json.dump(reports, f, indent=2)

    print(f"[+] RCA reports saved to '{REPORT_TXT_FILE}' and '{REPORT_JSON_FILE}'.")
    return reports

def main():
    causal_file = sys.argv[1] if len(sys.argv) > 1 else CAUSAL_SUMMARIES_FILE
    explain_anomalies(causal_file)

if __name__ == '__main__':
    main()
