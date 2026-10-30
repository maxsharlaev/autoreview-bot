#!/usr/bin/env python3
"""Minimal echo/fake OpenAI-compatible proxy for M3 Codex spike. Logs requests; no real key."""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

LOG_PATH = Path("/tmp/fake_proxy_requests.jsonl")
PORT = int(__import__("os").environ.get("FAKE_PROXY_PORT") or "18080")


def _append(rec: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # quieter
        return

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _record(self, body: bytes) -> dict:
        auth = self.headers.get("Authorization") or self.headers.get("authorization") or ""
        auth_meta = {
            "present": bool(auth),
            "scheme": auth.split(" ", 1)[0] if auth else "",
            "token_prefix": (auth.split(" ", 1)[1][:12] + "…")
            if " " in auth and len(auth.split(" ", 1)[1]) > 12
            else (auth.split(" ", 1)[1] if " " in auth else ""),
            "raw_len": len(auth),
        }
        headers = {k: v for k, v in self.headers.items() if k.lower() not in {"authorization"}}
        # keep interesting auth-adjacent headers names only
        for name in ("openai-organization", "openai-project", "x-api-key", "api-key"):
            if self.headers.get(name):
                headers[name] = "<redacted-present>"
        rec = {
            "ts": time.time(),
            "method": self.command,
            "path": self.path,
            "auth": auth_meta,
            "content_type": self.headers.get("Content-Type"),
            "accept": self.headers.get("Accept"),
            "headers": headers,
            "body_len": len(body),
            "body_preview": body[:800].decode("utf-8", errors="replace"),
        }
        _append(rec)
        print(
            json.dumps(
                {"method": rec["method"], "path": rec["path"], "auth": rec["auth"], "body_len": rec["body_len"]},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return rec

    def _send(self, code: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        body = self._read_body()
        rec = self._record(body)
        path = urlparse(self.path).path.rstrip("/") or "/"
        stream = False
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            stream = bool(payload.get("stream"))
        except Exception:
            payload = {}

        if self.command == "GET" and path.endswith("/models"):
            data = {
                "object": "list",
                "data": [{"id": "gpt-4.1-mini", "object": "model", "owned_by": "fake-proxy"}],
            }
            self._send(200, json.dumps(data).encode())
            return

        if self.command == "POST" and path.endswith("/chat/completions"):
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                chunks = [
                    {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                    },
                    {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": {"content": '{"findings":[]}'}, "finish_reason": None}],
                    },
                    {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    },
                ]
                for ch in chunks:
                    self.wfile.write(f"data: {json.dumps(ch)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                _append({**rec, "responded": "chat.completions.stream"})
                return
            data = {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"findings":[]}'},
                        "finish_reason": "stop",
                    }
                ],
            }
            self._send(200, json.dumps(data).encode())
            return

        if self.command == "POST" and (path.endswith("/responses") or path.endswith("/v1/responses")):
            # OpenAI Responses API shape (simplified)
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                events = [
                    (
                        "response.created",
                        {"type": "response.created", "response": {"id": "resp_fake", "status": "in_progress"}},
                    ),
                    ("response.output_text.delta", {"type": "response.output_text.delta", "delta": '{"findings":[]}'}),
                    (
                        "response.completed",
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp_fake",
                                "status": "completed",
                                "output": [
                                    {"type": "message", "content": [{"type": "output_text", "text": '{"findings":[]}'}]}
                                ],
                            },
                        },
                    ),
                ]
                for evt, payload_evt in events:
                    self.wfile.write(f"event: {evt}\ndata: {json.dumps(payload_evt)}\n\n".encode())
                    self.wfile.flush()
                _append({**rec, "responded": "responses.stream"})
                return
            data = {
                "id": "resp_fake",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": '{"findings":[]}'}],
                    }
                ],
            }
            self._send(200, json.dumps(data).encode())
            return

        # Fallback: 404 with logged path so we discover unknown endpoints
        err = {"error": {"message": f"fake-proxy: unhandled {self.command} {path}", "type": "fake_proxy_unhandled"}}
        self._send(404, json.dumps(err).encode())

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle()


def main() -> None:
    LOG_PATH.write_text("", encoding="utf-8")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"fake-proxy listening on 0.0.0.0:{PORT} log={LOG_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
