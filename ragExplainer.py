import os
import sys
import glob
import json
import datetime
import urllib.request
import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from typing import List, Dict, Any, Optional

import diagnosis

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
# Optional LLM layer (OpenAI-compatible API or local Ollama / vLLM)
#
# Enabled by setting RCA_LLM_API_KEY (for cloud APIs) or RCA_LLM_BASE_URL
# (for local/custom endpoints like Ollama at http://localhost:11434/v1):
#
#   export RCA_LLM_API_KEY=sk-...
#   export RCA_LLM_BASE_URL=https://api.groq.com/openai/v1   # optional
#   export RCA_LLM_MODEL=llama-3.3-70b-versatile             # optional
#
# Or with a local Ollama instance:
#   export RCA_LLM_BASE_URL=http://localhost:11434/v1
#   export RCA_LLM_MODEL=llama3.2
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


def is_llm_enabled() -> bool:
    """Return True if an API key is set or a local LLM endpoint is configured."""
    api_key = os.environ.get('RCA_LLM_API_KEY', '').strip()
    base_url = os.environ.get('RCA_LLM_BASE_URL', '').strip().lower()
    if api_key:
        return True
    if base_url and any(h in base_url for h in ('localhost', '127.0.0.1', '11434', 'ollama')):
        return True
    return False


def query_llm(prompt: str) -> Optional[str]:
    """Ask an OpenAI-compatible /chat/completions API or local Ollama instance.

    Returns None when disabled or when the call fails for any reason, so callers
    can seamlessly fall back to the grounded template report.
    """
    if not is_llm_enabled():
        return None

    api_key = os.environ.get('RCA_LLM_API_KEY', '').strip()
    base_url = os.environ.get('RCA_LLM_BASE_URL', '').strip() or LLM_BASE_URL_DEFAULT
    base_url = base_url.rstrip('/')

    # For local servers (Ollama, vLLM, local stubs) without an explicit key, use a dummy token
    if not api_key:
        api_key = 'local'

    model = os.environ.get('RCA_LLM_MODEL', '').strip()
    if not model:
        if '11434' in base_url or 'ollama' in base_url.lower():
            model = 'llama3.2'
        else:
            model = LLM_MODEL_DEFAULT

    # Determine endpoint URL
    if base_url.endswith('/chat/completions'):
        url = base_url
    elif base_url.endswith('/v1'):
        url = f'{base_url}/chat/completions'
    elif ':11434' in base_url and not base_url.endswith('/api'):
        # Ollama root host -> standard OpenAI compatibility path
        url = f'{base_url}/v1/chat/completions'
    else:
        url = f'{base_url}/chat/completions'

    payload = json.dumps({
        'model': model,
        'messages': [
            {'role': 'system', 'content': LLM_SYSTEM_PROMPT},
            {'role': 'user', 'content': prompt},
        ],
        'temperature': 0.2,
    }).encode('utf-8')

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        # Standard OpenAI schema
        if 'choices' in data and len(data['choices']) > 0:
            msg = data['choices'][0].get('message', {})
            return msg.get('content', '').strip()
        # Native Ollama /api/chat schema
        if 'message' in data and 'content' in data['message']:
            return data['message']['content'].strip()
        # Native Ollama /api/generate schema
        if 'response' in data:
            return data['response'].strip()
        return None
    except urllib.error.HTTPError as e:
        # If 404 on local /v1/chat/completions, attempt fallback to native /api/chat
        if e.code == 404 and (':11434' in base_url or 'ollama' in base_url.lower() or 'localhost' in base_url or '127.0.0.1' in base_url):
            try:
                clean_root = base_url.split('/v1')[0]
                native_url = f"{clean_root}/api/chat"
                native_payload = json.dumps({
                    'model': model,
                    'messages': [
                        {'role': 'system', 'content': LLM_SYSTEM_PROMPT},
                        {'role': 'user', 'content': prompt},
                    ],
                    'stream': False,
                }).encode('utf-8')
                native_req = urllib.request.Request(
                    native_url,
                    data=native_payload,
                    headers={'Content-Type': 'application/json'},
                )
                with urllib.request.urlopen(native_req, timeout=30) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                if 'message' in data and 'content' in data['message']:
                    return data['message']['content'].strip()
            except Exception as e_inner:
                print(f"[!] Ollama fallback failed ({type(e_inner).__name__}: {e_inner})")
        print(f"[!] LLM call failed ({type(e).__name__}: {e}) — using grounded template only.")
        return None
    except Exception as e:
        print(f"[!] LLM call failed ({type(e).__name__}: {e}) — using grounded template only.")
        return None


# Backward-compatible alias
query_online_llm = query_llm


def review_report_with_llm(report_text: str) -> Optional[str]:
    """Review an evidence-grounded RCA report with strictly bounded LLM reasoning.

    Returns the review text, or None if the LLM layer is disabled or unavailable.
    """
    if not is_llm_enabled():
        return None

    prompt = (
        "Below is an evidence-grounded RCA draft produced by rule-based analysis "
        "of kernel telemetry for ONE anomaly window. Review it strictly against "
        "the evidence it cites.\n\n"
        f"{report_text}\n"
        "State the most likely root cause and the single most informative piece "
        "of evidence for it. If the draft is not supported by the evidence, say "
        "what is missing instead of guessing."
    )
    return query_llm(prompt)

def build_retrieval_query(window: Dict[str, Any]) -> str:
    """Keyword query for TF-IDF retrieval.

    Ground-truth labels (`labeled_causes`) are intentionally ignored — they are
    for evaluation only, never for the production diagnosis path.
    """
    tokens = []

    # Candidate root process names
    for r in window.get('candidate_roots', [])[:3]:
        comm = r.split(':')[0]
        tokens.append(comm)

    # Sample path textual hints
    for p in window.get('sample_paths', [])[:2]:
        tokens.append(p.replace('->', ' '))

    # Metric-derived keywords from the shared label-free classifier
    diag = diagnosis.classify_cause(
        window.get('top_metrics', {}),
        culprit_comms=window.get('top_processes', []),
    )
    tokens.extend(QUERY_TOKENS.get(diag['family'], []))

    return " ".join(tokens)


# TF-IDF query tokens per diagnosed cause family (see diagnosis.classify_cause)
QUERY_TOKENS = {
    'cpu': ['cpu competition', 'runqueue wait', 'scheduling', 'cpu throttling', 'quota'],
    'memory': ['memory pressure', 'swapping', 'thrashing', 'allocstall'],
    'heap': ['heap expansion', 'brk', 'anonymous memory growth', 'major faults'],
    'io': ['io saturation', 'disk writes', 'iowait', 'storage'],
    'lock': ['futex', 'lock', 'kernel contention', 'dentry', 'vfs'],
    'oom': ['oom', 'out of memory', 'kill', 'memory exhaustion'],
    'unknown': ['anomaly', 'contention'],
}

def _fmt_ts(ts) -> str:
    """Local human-readable timestamp (epoch seconds -> 'YYYY-mm-dd HH:MM:SS')."""
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError, OSError):
        return str(ts)


def synthesize_grounded_report(window: Dict[str, Any], retrieved_docs: List[Dict[str, Any]]) -> str:
    win_id = window.get('window_id', 'unknown')
    roots = window.get('candidate_roots', [])
    top_root = roots[0] if roots else "Unidentified Process"
    culprit_comm = top_root.split(':')[0]
    sample_paths = window.get('sample_paths', [])
    metrics = window.get('top_metrics', {})

    evidence_points = []
    remediation = "Investigate system logs and process resource limits."

    # --- Classification from observed evidence only (metrics + identified
    #     culprit processes) via the shared label-free classifier.
    #     Ground-truth labels are for evaluation and are deliberately not used. ---
    diag = diagnosis.classify_cause(metrics, culprit_comms=window.get('top_processes', []))
    classified_cause = diag['cause']

    # --- Collect metric evidence (all relevant, regardless of classification) ---
    if metrics.get('oom_kill_count', 0) > 0:
        evidence_points.append(f"Kernel vmstat reported {int(metrics['oom_kill_count'])} OOM kills.")
        remediation = "Set strict memory limits via cgroups (`memory.max`) and profile process memory consumption."
    if metrics.get('oom_kills', 0) > 0:
        evidence_points.append(
            f"Kernel OOM-kill event(s) observed ({int(metrics['oom_kills'])} tracepoint hit(s))."
        )
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

    # Classification evidence trail (which signals fired, in which tier)
    evidence_points.append(f"Classification basis: {diag['basis']}.")

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
        f"Time Range: {_fmt_ts(window.get('start', 0))} -> {_fmt_ts(window.get('end', 0))}"
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
    if is_llm_enabled():
        base_url = os.environ.get('RCA_LLM_BASE_URL', LLM_BASE_URL_DEFAULT)
        model = os.environ.get('RCA_LLM_MODEL', LLM_MODEL_DEFAULT)
        print(f"[i] LLM review enabled: {model} @ {base_url}")
    else:
        print("[i] LLM review disabled (set RCA_LLM_API_KEY or RCA_LLM_BASE_URL to enable; template report only).")
    reports = []
    full_text_report = []

    print(f"\n=======================================================")
    print(f"       ROOT CAUSE ANALYSIS (RAG EXPLAINER)            ")
    print(f"=======================================================")

    for win in windows:
        # If window had no edges and no candidate roots, but had anomaly flags:
        query = build_retrieval_query(win)
        retrieved_docs = corpus.retrieve(query, top_k=2) if query.strip() else []

        report_text = synthesize_grounded_report(win, retrieved_docs)

        # Optional LLM review (OpenAI-compatible or local Ollama)
        llm_response = review_report_with_llm(report_text)
        if llm_response:
            report_text += f"\nLLM review:\n{llm_response}\n"

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
