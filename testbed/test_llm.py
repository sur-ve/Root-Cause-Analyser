#!/usr/bin/env python3
"""Unit tests for the RCA LLM review layer (ragExplainer).

Tests OpenAI-compatible completions, local Ollama endpoints, fallback handling,
and disabled behavior without needing external internet or real LLM services.
"""

import os
import sys
import json
import threading
import http.server
import unittest
from typing import Dict, Any

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ragExplainer


class MockLLMHandler(http.server.BaseHTTPRequestHandler):
    mode = "openai"  # "openai", "ollama_chat", "ollama_fallback", "error"

    def log_message(self, format, *args):
        pass  # Quiet logging

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        req_data = json.loads(body) if body else {}

        if self.mode == "error":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": "Internal server error"}')
            return

        if self.mode == "ollama_fallback":
            if self.path.endswith("/v1/chat/completions"):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"404 page not found")
                return
            elif self.path.endswith("/api/chat"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                resp = {
                    "model": req_data.get("model", "llama3.2"),
                    "message": {
                        "role": "assistant",
                        "content": "Likely root cause: CPU Contention\nKey evidence: PSI avg10 > 20%\nSuggested action: Check CPU quota",
                    },
                    "done": True,
                }
                self.wfile.write(json.dumps(resp).encode("utf-8"))
                return

        if self.mode == "ollama_chat":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = {
                "message": {
                    "role": "assistant",
                    "content": "Likely root cause: Memory Pressure\nKey evidence: Swap in/out\nSuggested action: Increase memory",
                }
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        # Default: OpenAI format
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        resp = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Likely root cause: CPU Competition\nKey evidence: stress-ng-cpu blocked runqueue\nSuggested action: Reduce parallel workers",
                    }
                }
            ]
        }
        self.wfile.write(json.dumps(resp).encode("utf-8"))


class TestLLMLayer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockLLMHandler)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.orig_env = os.environ.copy()
        for k in ("RCA_LLM_API_KEY", "RCA_LLM_BASE_URL", "RCA_LLM_MODEL"):
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_env)

    def test_is_llm_enabled(self):
        # Disabled when no vars set
        self.assertFalse(ragExplainer.is_llm_enabled())

        # Enabled when API key set
        os.environ["RCA_LLM_API_KEY"] = "sk-test12345"
        self.assertTrue(ragExplainer.is_llm_enabled())
        os.environ.pop("RCA_LLM_API_KEY")

        # Enabled when localhost URL set
        os.environ["RCA_LLM_BASE_URL"] = "http://localhost:11434"
        self.assertTrue(ragExplainer.is_llm_enabled())

        os.environ["RCA_LLM_BASE_URL"] = "http://127.0.0.1:8000/v1"
        self.assertTrue(ragExplainer.is_llm_enabled())

    def test_openai_compatible_query(self):
        MockLLMHandler.mode = "openai"
        os.environ["RCA_LLM_API_KEY"] = "sk-mock-key"
        os.environ["RCA_LLM_BASE_URL"] = f"http://127.0.0.1:{self.port}/v1"
        os.environ["RCA_LLM_MODEL"] = "gpt-4o-mini"

        review = ragExplainer.review_report_with_llm("=== RCA Report ===\nLikely root cause: CPU")
        self.assertIsNotNone(review)
        self.assertIn("Likely root cause: CPU Competition", review)
        self.assertIn("stress-ng-cpu", review)

    def test_local_ollama_query(self):
        MockLLMHandler.mode = "ollama_chat"
        # Notice: no API key required for local Ollama
        os.environ["RCA_LLM_BASE_URL"] = f"http://127.0.0.1:{self.port}/v1"

        review = ragExplainer.query_llm("Review memory pressure draft")
        self.assertIsNotNone(review)
        self.assertIn("Memory Pressure", review)

    def test_ollama_fallback_query(self):
        MockLLMHandler.mode = "ollama_fallback"
        os.environ["RCA_LLM_BASE_URL"] = f"http://127.0.0.1:{self.port}/v1"

        review = ragExplainer.query_llm("Review CPU contention draft")
        self.assertIsNotNone(review)
        self.assertIn("CPU Contention", review)

    def test_failure_graceful_fallback(self):
        MockLLMHandler.mode = "error"
        os.environ["RCA_LLM_API_KEY"] = "sk-mock-key"
        os.environ["RCA_LLM_BASE_URL"] = f"http://127.0.0.1:{self.port}/v1"

        # Should return None and not throw
        review = ragExplainer.review_report_with_llm("=== RCA Report ===")
        self.assertIsNone(review)

    def test_unreachable_endpoint_graceful_fallback(self):
        os.environ["RCA_LLM_API_KEY"] = "sk-mock-key"
        # Non-listening port
        os.environ["RCA_LLM_BASE_URL"] = "http://127.0.0.1:59999/v1"

        review = ragExplainer.review_report_with_llm("=== RCA Report ===")
        self.assertIsNone(review)


if __name__ == "__main__":
    unittest.main()
