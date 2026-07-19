#!/usr/bin/env python3
r"""serve.py — the long-running query API (ADR-0017 §Next).
==========================================================

A stdlib HTTP server (no framework — keeps the image minimal + the read-only rootfs clean).
The gateway's :8443 path-router forwards /{bundle}/{neuron}/<suffix> to <neuron>:8080/<suffix>
(prefix stripped), so this app serves plain root paths:

    GET  /health           -> {"status":"ok", ...}
    GET  /ask?q=...&k=N     -> answer JSON
    POST /ask  {"question":"...", "n_results":N}  -> answer JSON

POC NOTE: the :8443 surface is deliberately unauthenticated (brain.env ACTION_*); the gateway
still INSPECTS it and this neuron holds only a reader token. Binds PLAIN on neuron_net; the
gateway terminates TLS.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from action_common import ChromaClient, Config, GatewayError, OllamaClient, log
from retrieve import answer_question

MAX_BODY = 64 * 1024  # a question is small; cap to avoid unbounded reads


class _Handler(BaseHTTPRequestHandler):
    # Injected on the server instance (see serve()).
    cfg: Config
    chroma: ChromaClient
    ollama: OllamaClient

    server_version = "aios-action/1.0"

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # route access logs to stderr, quietly
        log("http: " + (fmt % args))

    def _answer(self, question: str, k: int) -> None:
        try:
            ans = answer_question(self.cfg, self.chroma, self.ollama, question, n_results=k)
            self._json(200, ans.to_dict())
        except GatewayError as e:
            self._json(502, {"error": "backend error", "detail": str(e)})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": "internal error", "detail": str(e)})

    def do_GET(self) -> None:
        u = urlparse(self.path)
        if u.path in ("/health", "/healthz", "/"):
            try:
                n = self.chroma.count(self.cfg.collection)
                self._json(200, {"status": "ok", "bundle": self.cfg.bundle,
                                 "neuron": self.cfg.name, "collection": self.cfg.collection,
                                 "records": n})
            except GatewayError as e:
                # Healthy app, backend not ready / empty collection — still "up".
                self._json(200, {"status": "ok", "bundle": self.cfg.bundle,
                                 "neuron": self.cfg.name, "collection": self.cfg.collection,
                                 "records": None, "note": str(e)})
            return
        if u.path == "/ask":
            q = (parse_qs(u.query).get("q") or [""])[0]
            k = int((parse_qs(u.query).get("k") or ["5"])[0])
            if not q:
                self._json(400, {"error": "missing ?q="})
                return
            self._answer(q, k)
            return
        self._json(404, {"error": "not found", "routes": ["GET /health", "GET|POST /ask"]})

    def do_POST(self) -> None:
        u = urlparse(self.path)
        if u.path != "/ask":
            self._json(404, {"error": "not found", "routes": ["GET /health", "GET|POST /ask"]})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._json(400, {"error": f"body must be 1..{MAX_BODY} bytes"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"error": "body must be JSON"})
            return
        question = str(payload.get("question") or payload.get("q") or "")
        k = int(payload.get("n_results") or payload.get("k") or 5)
        if not question.strip():
            self._json(400, {"error": "missing 'question'"})
            return
        self._answer(question, k)


def serve(cfg: Config, host: str, port: int) -> int:
    chroma = ChromaClient(cfg)
    ollama = OllamaClient(cfg)

    class Handler(_Handler):
        pass
    Handler.cfg, Handler.chroma, Handler.ollama = cfg, chroma, ollama

    httpd = ThreadingHTTPServer((host, port), Handler)
    log(f"query API up on {host}:{port} — bundle '{cfg.bundle}' neuron '{cfg.name}' "
        f"collection '{cfg.collection}' (GET /health, GET|POST /ask)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        httpd.server_close()
    return 0
