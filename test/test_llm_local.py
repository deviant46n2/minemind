"""Local llama.cpp provider for agent/llm.py (OpenAI-compatible endpoint).

Run: python3 test/test_llm_local.py
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

import llm  # noqa: E402

SAVED = {}
CAPTURED = {}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        CAPTURED["path"] = self.path
        CAPTURED["body"] = json.loads(self.rfile.read(length))
        payload = json.dumps({
            "choices": [{"message": {"content": '{"steps": []}'}}],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence
        pass


def _with_local_env(url: str) -> None:
    for key in ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL", "RECLAMATION_LLM",
                "GEMINI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY"):
        SAVED[key] = os.environ.pop(key, None)
    os.environ["LOCAL_LLM_URL"] = url
    os.environ["LOCAL_LLM_MODEL"] = "qwen3-test"


def _restore_env() -> None:
    for key, val in SAVED.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


def test_provider_prefers_local() -> None:
    _with_local_env("http://127.0.0.1:1/v1/chat/completions")
    try:
        assert llm.provider() == "local", f"provider() = {llm.provider()!r}"
    finally:
        _restore_env()


def test_local_complete_roundtrip() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
    _with_local_env(url)
    try:
        assert llm.provider() == "local"
        out = llm.complete("you are a planner", "give me a plan", max_tokens=128)
        assert out == '{"steps": []}', f"got {out!r}"
        assert CAPTURED["path"] == "/v1/chat/completions"
        body = CAPTURED["body"]
        assert body["model"] == "qwen3-test", f"model {body['model']!r}"
        assert body["chat_template_kwargs"] == {"enable_thinking": False}, \
            "thinking must be off — reasoning steals planner token budget"
        roles = [m["role"] for m in body["messages"]]
        assert roles == ["system", "user"], f"roles {roles}"
        assert body["max_tokens"] == 128
    finally:
        _restore_env()
        server.shutdown()


def main() -> None:
    test_provider_prefers_local()
    print("ok  test_provider_prefers_local")
    test_local_complete_roundtrip()
    print("ok  test_local_complete_roundtrip")
    print("PASS")


if __name__ == "__main__":
    main()
