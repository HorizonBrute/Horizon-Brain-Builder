#!/usr/bin/env python3
r"""ingest_common.py — the shared substrate for the input neuron.
================================================================

Env-driven config, the two gateway'd backend clients (Chroma v2 REST + Ollama), an
OFFLINE chunker, a content-hash dedup state, and the ADR-0015 attribution headers. Every
backend call goes to the gateway (http://chroma:8000 / http://ollama:11434 resolve to the
gateway on neuron_net) carrying the scoped NEURON_GATEWAY_TOKEN and the three X-Neuron-*
headers the gateway's content-capture log attributes by.

Design note (deliberate deviation from the compose comment's "LlamaIndex + chroma client"):
we speak the Chroma v2 REST API directly (proven end-to-end by the through-gateway upload
smoke test), because a CUSTOM bearer routed through the neuron_net gateway alias is far
cleaner over the REST surface than through the chromadb HttpClient, and it keeps the image
tiny and 100% offline at runtime.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import requests


# --------------------------------------------------------------------------- #
# Logging (stderr; stdout is reserved for scripted-adapter JSON-Lines)
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(f"[neuron] {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[valid-type]
    print(f"[neuron] ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


class GatewayError(RuntimeError):
    """A backend call through the gateway failed (non-2xx, or transport error after retries)."""


# --------------------------------------------------------------------------- #
# Config (env — the compose `environment:` block is the single source of truth)
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    chroma_url: str
    ollama_url: str
    token: str
    knowledge_root: str
    manifest_path: str
    state_dir: str
    bundle: str
    role: str
    name: str
    collection: str                 # the bundle's DEFAULT collection (a source may override)
    image_embed: str                # "caption" | "clip"
    image_caption_model: str
    tenant: str = "default_tenant"
    database: str = "default_database"

    @classmethod
    def from_env(cls) -> "Config":
        def req(k: str) -> str:
            v = os.environ.get(k, "").strip()
            if not v:
                die(f"required env var {k} is unset")
            return v

        return cls(
            chroma_url=os.environ.get("CHROMA_URL", "http://chroma:8000").rstrip("/"),
            ollama_url=os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/"),
            token=req("NEURON_GATEWAY_TOKEN"),
            knowledge_root=os.environ.get("KNOWLEDGE_ROOT", "/knowledge").rstrip("/"),
            manifest_path=os.environ.get("NEURON_MANIFEST", "/etc/neuron/sources.yaml"),
            state_dir=os.environ.get("NEURON_STATE_DIR", "/state").rstrip("/"),
            bundle=os.environ.get("NEURON_BUNDLE", "default"),
            role=os.environ.get("NEURON_ROLE", "input"),
            name=os.environ.get("NEURON_NAME", "input_1"),
            collection=os.environ.get("BUNDLE_COLLECTION", "docs"),
            image_embed=os.environ.get("NEURON_IMAGE_EMBED", "caption").lower(),
            image_caption_model=os.environ.get("NEURON_IMAGE_CAPTION_MODEL", "moondream"),
            tenant=os.environ.get("CHROMA_TENANT", "default_tenant"),
            database=os.environ.get("CHROMA_DATABASE", "default_database"),
        )

    def attribution_headers(self) -> dict[str, str]:
        """The scoped bearer + the three X-Neuron-* headers the gateway attributes captures by."""
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Neuron-Bundle": self.bundle,
            "X-Neuron-Role": self.role,
            "X-Neuron-Name": self.name,
        }


# --------------------------------------------------------------------------- #
# HTTP with bounded retries (transient 5xx / connection resets during warm-up)
# --------------------------------------------------------------------------- #
def _request(session: requests.Session, method: str, url: str, headers: dict,
             json_body: Any = None, timeout: float = 120.0, retries: int = 4) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            r = session.request(method, url, headers=headers, json=json_body, timeout=timeout)
        except requests.RequestException as e:
            last_exc = e
            if attempt == retries:
                raise GatewayError(f"{method} {url} transport error after {retries} tries: {e}") from e
            time.sleep(min(2 ** attempt, 10))
            continue
        # Retry only on transient upstream states (gateway/backends warming up).
        if r.status_code in (502, 503, 504) and attempt < retries:
            time.sleep(min(2 ** attempt, 10))
            continue
        return r
    raise GatewayError(f"{method} {url} failed: {last_exc}")


# --------------------------------------------------------------------------- #
# Chroma v2 REST (through the gateway) — proven path (see testupload_through_gateway.py)
# --------------------------------------------------------------------------- #
class ChromaClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.base = (f"{cfg.chroma_url}/api/v2/tenants/{cfg.tenant}"
                     f"/databases/{cfg.database}")
        self.headers = {**cfg.attribution_headers(), "Content-Type": "application/json"}

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        r = _request(self.session, method, self.base + path, self.headers, body)
        if r.status_code >= 400:
            raise GatewayError(f"chroma {method} {path} -> {r.status_code}: {r.text[:400]}")
        raw = r.text
        return (r.json() if raw.strip() else None)

    def get_or_create_collection(self, name: str, metadata: dict | None = None) -> str:
        body: dict[str, Any] = {"name": name, "get_or_create": True}
        if metadata:
            body["metadata"] = metadata
        res = self._call("POST", "/collections", body)
        cid = (res or {}).get("id")
        if not cid:
            raise GatewayError(f"chroma create collection '{name}' returned no id: {res}")
        return cid

    def add(self, cid: str, ids: list[str], embeddings: list[list[float]],
            documents: list[str], metadatas: list[dict]) -> None:
        # Chroma `add` upserts by id in practice for the v2 add endpoint; we DELETE stale ids
        # first (see DedupState) so a changed doc's old chunks never linger.
        self._call("POST", f"/collections/{cid}/add", {
            "ids": ids, "embeddings": embeddings, "documents": documents, "metadatas": metadatas,
        })

    def delete(self, cid: str, ids: list[str]) -> None:
        if ids:
            self._call("POST", f"/collections/{cid}/delete", {"ids": ids})

    def delete_where(self, cid: str, where: dict) -> None:
        self._call("POST", f"/collections/{cid}/delete", {"where": where})

    def count(self, cid: str) -> int:
        return int(self._call("GET", f"/collections/{cid}/count") or 0)

    def get_ids_where(self, cid: str, where: dict) -> list[str]:
        res = self._call("POST", f"/collections/{cid}/get", {"where": where, "include": []})
        return list((res or {}).get("ids", []) or [])


# --------------------------------------------------------------------------- #
# Ollama (through the gateway) — embeddings + generate (captioning)
# --------------------------------------------------------------------------- #
class OllamaClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.headers = cfg.attribution_headers()  # ollama:use scope; no Content-Type needed by requests(json=)

    def embed(self, model: str, text: str) -> list[float]:
        r = _request(self.session, "POST", f"{self.cfg.ollama_url}/api/embeddings",
                     self.headers, {"model": model, "prompt": text})
        if r.status_code >= 400:
            raise GatewayError(f"ollama embeddings ({model}) -> {r.status_code}: {r.text[:300]}")
        vec = r.json().get("embedding")
        if not vec:
            raise GatewayError(f"ollama embeddings ({model}) returned no vector for a {len(text)}-char input")
        return vec

    def generate(self, model: str, prompt: str, images: list[str] | None = None,
                 options: dict | None = None) -> str:
        body: dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
        if images:
            body["images"] = images
        if options:
            body["options"] = options
        r = _request(self.session, "POST", f"{self.cfg.ollama_url}/api/generate",
                     self.headers, body, timeout=300.0)
        if r.status_code >= 400:
            raise GatewayError(f"ollama generate ({model}) -> {r.status_code}: {r.text[:300]}")
        return (r.json().get("response") or "").strip()


# --------------------------------------------------------------------------- #
# Offline chunker — sentence-aware, token-BUDGETED without a tokenizer download.
# chunk_size / overlap are in TOKENS (sources.yaml defaults: 800 / 100). With no runtime
# tokenizer available (no internet on neuron_net), we budget by a stable chars-per-token
# estimate; deterministic and dependency-free.
# --------------------------------------------------------------------------- #
_CHARS_PER_TOKEN = 4  # conservative English average; keeps chunks comfortably under model limits
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n{2,}")


def chunk_text(text: str, chunk_size_tokens: int = 800, overlap_tokens: int = 100) -> list[str]:
    text = text.strip()
    if not text:
        return []
    budget = max(1, chunk_size_tokens) * _CHARS_PER_TOKEN
    overlap = max(0, overlap_tokens) * _CHARS_PER_TOKEN
    # Split into sentence-ish units; hard-wrap any single unit longer than the budget.
    units: list[str] = []
    for piece in _SENT_SPLIT.split(text):
        piece = piece.strip()
        if not piece:
            continue
        while len(piece) > budget:
            units.append(piece[:budget])
            piece = piece[budget:]
        units.append(piece)

    chunks: list[str] = []
    cur = ""
    for u in units:
        if cur and len(cur) + 1 + len(u) > budget:
            chunks.append(cur)
            # carry the tail as overlap for retrieval continuity
            cur = (cur[-overlap:] + " " + u).strip() if overlap else u
        else:
            cur = (cur + " " + u).strip() if cur else u
    if cur:
        chunks.append(cur)
    return chunks


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# Dedup state — content-hash per (collection, doc_id). Re-embed only changed docs;
# a changed doc's OLD chunks are deleted before the new ones are added.
# Persisted under NEURON_STATE_DIR (a rebuildable named volume), keyed per neuron so
# neurons sharing the volume never collide.
# --------------------------------------------------------------------------- #
import json  # noqa: E402  (kept local to the state section for clarity)


@dataclass
class DedupState:
    cfg: Config
    collection: str
    _path: str = field(init=False)
    _map: dict[str, str] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        d = os.path.join(self.cfg.state_dir, self.cfg.name)
        os.makedirs(d, exist_ok=True)
        self._path = os.path.join(d, f"{self.collection}.json")
        if os.path.isfile(self._path):
            try:
                self._map = json.loads(open(self._path, encoding="utf-8").read()) or {}
            except (OSError, ValueError):
                self._map = {}

    def unchanged(self, doc_id: str, h: str) -> bool:
        return self._map.get(doc_id) == h

    def record(self, doc_id: str, h: str) -> None:
        self._map[doc_id] = h

    def known_ids(self) -> set[str]:
        return set(self._map)

    def forget(self, doc_id: str) -> None:
        self._map.pop(doc_id, None)

    def save(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(self._map, indent=2, sort_keys=True))
        os.replace(tmp, self._path)
