#!/usr/bin/env python3
r"""action_common.py — the shared substrate for the action (read-side) neuron.
=============================================================================

Env-driven config + the two gateway'd backend clients (Chroma v2 REST query/get + Ollama
embed/generate), carrying the scoped READER bearer and the X-Neuron-* attribution headers.
Self-contained (the action image is a SEPARATE build context from input_neurons), so the
minimal client code is duplicated here by design — the two images never share a filesystem.

The reader token has chroma:reader + ollama:use only, so this side PHYSICALLY cannot write
to Chroma; any write must go back through an input neuron (write-funnel invariant, enforced
by the gateway).
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from typing import Any

import requests


def log(msg: str) -> None:
    print(f"[action] {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[valid-type]
    print(f"[action] ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


class GatewayError(RuntimeError):
    """A backend call through the gateway failed."""


@dataclass
class Config:
    chroma_url: str
    ollama_url: str
    token: str
    bundle: str
    role: str
    name: str
    collection: str
    embed_model: str
    llm_model: str
    tenant: str = "default_tenant"
    database: str = "default_database"

    @classmethod
    def from_env(cls) -> "Config":
        tok = os.environ.get("ACTION_GATEWAY_TOKEN", "").strip()
        if not tok:
            die("required env var ACTION_GATEWAY_TOKEN is unset")
        return cls(
            chroma_url=os.environ.get("CHROMA_URL", "http://chroma:8000").rstrip("/"),
            ollama_url=os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/"),
            token=tok,
            bundle=os.environ.get("NEURON_BUNDLE", "default"),
            role=os.environ.get("NEURON_ROLE", "action"),
            name=os.environ.get("NEURON_NAME", "action_1"),
            collection=os.environ.get("BUNDLE_COLLECTION", "docs"),
            embed_model=os.environ.get("ACTION_EMBED_MODEL", "nomic-embed-text"),
            llm_model=os.environ.get("ACTION_LLM_MODEL", "qwen2.5:1.5b"),
            tenant=os.environ.get("CHROMA_TENANT", "default_tenant"),
            database=os.environ.get("CHROMA_DATABASE", "default_database"),
        )

    def attribution_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Neuron-Bundle": self.bundle,
            "X-Neuron-Role": self.role,
            "X-Neuron-Name": self.name,
        }


def _request(session: requests.Session, method: str, url: str, headers: dict,
             json_body: Any = None, timeout: float = 600.0, retries: int = 4) -> requests.Response:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            r = session.request(method, url, headers=headers, json=json_body, timeout=timeout)
        except requests.RequestException as e:
            last = e
            if attempt == retries:
                raise GatewayError(f"{method} {url} transport error after {retries} tries: {e}") from e
            time.sleep(min(2 ** attempt, 10))
            continue
        if r.status_code in (502, 503, 504) and attempt < retries:
            time.sleep(min(2 ** attempt, 10))
            continue
        return r
    raise GatewayError(f"{method} {url} failed: {last}")


class ChromaClient:
    """Read-only Chroma v2 REST access through the gateway (resolve collection, query, get)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.base = (f"{cfg.chroma_url}/api/v2/tenants/{cfg.tenant}"
                     f"/databases/{cfg.database}")
        self.headers = {**cfg.attribution_headers(), "Content-Type": "application/json"}
        self._cid: str | None = None

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        r = _request(self.session, method, self.base + path, self.headers, body)
        if r.status_code >= 400:
            raise GatewayError(f"chroma {method} {path} -> {r.status_code}: {r.text[:400]}")
        raw = r.text
        return (r.json() if raw.strip() else None)

    def collection_id(self, name: str) -> str:
        """Resolve a collection NAME to its id (reader-safe GET; cached)."""
        if self._cid:
            return self._cid
        res = self._call("GET", f"/collections/{name}")
        cid = (res or {}).get("id")
        if not cid:
            raise GatewayError(f"collection '{name}' not found (nothing ingested yet?)")
        self._cid = cid
        return cid

    def query(self, name: str, query_embedding: list[float], n_results: int) -> dict:
        cid = self.collection_id(name)
        return self._call("POST", f"/collections/{cid}/query", {
            "query_embeddings": [query_embedding],
            "n_results": n_results,
            "include": ["documents", "metadatas", "distances"],
        }) or {}

    def count(self, name: str) -> int:
        cid = self.collection_id(name)
        return int(self._call("GET", f"/collections/{cid}/count") or 0)


class OllamaClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.headers = cfg.attribution_headers()

    def embed(self, text: str) -> list[float]:
        r = _request(self.session, "POST", f"{self.cfg.ollama_url}/api/embeddings",
                     self.headers, {"model": self.cfg.embed_model, "prompt": text})
        if r.status_code >= 400:
            raise GatewayError(f"ollama embeddings -> {r.status_code}: {r.text[:300]}")
        vec = r.json().get("embedding")
        if not vec:
            raise GatewayError("ollama embeddings returned no vector")
        return vec

    def generate(self, prompt: str, options: dict | None = None) -> str:
        r = _request(self.session, "POST", f"{self.cfg.ollama_url}/api/generate", self.headers,
                     {"model": self.cfg.llm_model, "prompt": prompt, "stream": False,
                      "options": options or {"temperature": 0.1}})
        if r.status_code >= 400:
            raise GatewayError(f"ollama generate -> {r.status_code}: {r.text[:300]}")
        return (r.json().get("response") or "").strip()
