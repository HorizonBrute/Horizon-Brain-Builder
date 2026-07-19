#!/usr/bin/env python3
r"""manifest.py — read sources.yaml, resolve THIS bundle's sources, run delivery adapters,
and enumerate the files a source contributes after ingest_scope filtering.
=============================================================================

One source entry binds:  source --(delivery adapter)--> neuron (named pipeline) --> collection.

  * DELIVERY (the write phase, --deliver-only) makes the source PRESENT under brain_ro:
      on_disk             — nothing to do (admin-curated tree already at KNOWLEDGE_ROOT/<name>)
      git                 — clone/pull into brain_ro/<name> (write-capable; auth per ../github/)
      scripted            — a data-source-only script; handled at INGEST time (see below), no write.
  * INGEST (the read phase, --ingest-only) reads brain_ro READ-ONLY and writes vectors:
      on_disk / git       — walk KNOWLEDGE_ROOT/<name>, filter by ingest_scope include/exclude.
      scripted            — run the script; each stdout JSON-Lines {"path":..,"text":..} is a doc.

A source belongs to a bundle via `bundle:` (unlabeled -> the default bundle). An input neuron
ingests ONLY its own bundle's sources. `--tags` narrows further to sources carrying that tag.
"""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Iterator

import yaml

from ingest_common import Config, die, log

# Default file selection when a source declares no ingest_scope.include.
DEFAULT_TEXT_GLOBS = ["*.md", "*.txt", "*.rst", "*.markdown"]
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


@dataclass
class Source:
    name: str
    neuron: str | None
    bundle: str
    collection: str
    tags: list[str]
    delivery: dict
    ingest_scope: dict
    embed_model: str
    chunk_size: int
    chunk_overlap: int
    image_embed: str | None
    image_caption_model: str | None
    raw: dict = field(default_factory=dict)

    @property
    def adapter(self) -> str:
        # Accept BOTH the legacy `delivery.adapter` vocabulary AND the brain.env zone's
        # top-level `consumption:` vocabulary (config-flow Phase 5). Zone spelling
        # (on-disk|script|git) maps onto the internal adapter names (on_disk|scripted|git).
        consumption = self.raw.get("consumption")
        if consumption:
            return {"on-disk": "on_disk", "script": "scripted", "git": "git"}.get(
                str(consumption).lower(), str(consumption).lower())
        return str(self.delivery.get("adapter", "on_disk")).lower()

    @property
    def script(self) -> str | None:
        # The provider script: zone top-level `script:` OR legacy `delivery.script`.
        return self.raw.get("script") or self.delivery.get("script")


@dataclass
class Manifest:
    version: int
    defaults: dict
    schedules: dict
    sources: list[Source]


def load_manifest(cfg: Config) -> Manifest:
    path = cfg.manifest_path
    if not os.path.isfile(path):
        die(f"source manifest not found at {path} (mount ./neuron -> /etc/neuron:ro)")
    raw = yaml.safe_load(open(path, encoding="utf-8")) or {}
    defaults = raw.get("defaults", {}) or {}
    default_bundle = cfg.bundle  # this neuron's bundle is the fallback for unlabeled sources
    sources: list[Source] = []
    for s in (raw.get("sources") or []):
        if not isinstance(s, dict) or not s.get("name"):
            continue
        name = str(s["name"])
        sources.append(Source(
            name=name,
            neuron=s.get("neuron"),
            bundle=str(s.get("bundle") or default_bundle),
            collection=str(s.get("collection") or cfg.collection),
            tags=[str(t) for t in (s.get("tags") or [])],
            delivery=dict(s.get("delivery") or {"adapter": "on_disk"}),
            ingest_scope=dict(s.get("ingest_scope") or {}),
            embed_model=str(s.get("embed_model") or defaults.get("embed_model") or "nomic-embed-text"),
            chunk_size=int(s.get("chunk_size") or defaults.get("chunk_size") or 800),
            chunk_overlap=int(s.get("chunk_overlap") or defaults.get("chunk_overlap") or 100),
            image_embed=s.get("image_embed"),
            image_caption_model=s.get("image_caption_model"),
            raw=s,
        ))
    return Manifest(
        version=int(raw.get("version", 1)),
        defaults=defaults,
        schedules=raw.get("schedules", {}) or {},
        sources=sources,
    )


def resolve_sources(man: Manifest, cfg: Config, tags: list[str] | None) -> list[Source]:
    """This neuron's bundle's sources, optionally narrowed to a cadence tag."""
    out = [s for s in man.sources if s.bundle == cfg.bundle]
    if tags:
        want = set(tags)
        out = [s for s in out if want & set(s.tags)]
    return out


# --------------------------------------------------------------------------- #
# Delivery (WRITE phase — only run under --deliver-only)
# --------------------------------------------------------------------------- #
def source_root(cfg: Config, src: Source) -> str:
    """The brain_ro directory a source's files live under (delivery target + ingest root)."""
    root = src.delivery.get("root")
    if root:
        return root if os.path.isabs(root) else os.path.join(cfg.knowledge_root, root)
    return os.path.join(cfg.knowledge_root, src.name)


def deliver(cfg: Config, src: Source) -> None:
    a = src.adapter
    if a == "on_disk":
        log(f"deliver[{src.name}]: on_disk — admin-curated tree, nothing to fetch")
        return
    if a == "scripted":
        log(f"deliver[{src.name}]: scripted — a data source produced at ingest time, nothing to fetch")
        return
    if a == "git":
        _deliver_git(cfg, src)
        return
    die(f"deliver[{src.name}]: unknown delivery adapter '{a}'")


def _deliver_git(cfg: Config, src: Source) -> None:
    """Clone/pull a repo into brain_ro/<name>. auth: public | operator-delivered | transient-cred.
    A transient token (github.env GITHUB_TOKEN_ENV, default GITHUB_TOKEN) is injected into the URL
    for the ONE clone and never persisted. ssh uses the pinned known_hosts at /etc/github."""
    d = src.delivery
    url = d.get("url")
    ref = d.get("ref", "main")
    auth = str(d.get("auth", "public")).lower()
    protocol = str(d.get("protocol", "https")).lower()
    dest = source_root(cfg, src)

    if auth == "operator-delivered":
        log(f"deliver[{src.name}]: git auth=operator-delivered — tree is placed out-of-band; skipping clone")
        return
    if not url:
        die(f"deliver[{src.name}]: git delivery needs a `url:`")

    clone_url = url
    env = dict(os.environ)
    if protocol == "https" and auth == "transient-cred":
        tok_env = os.environ.get("GITHUB_TOKEN_ENV", "GITHUB_TOKEN")
        tok = os.environ.get(tok_env, "").strip()
        if not tok:
            die(f"deliver[{src.name}]: auth=transient-cred but ${tok_env} is empty "
                f"(inject it only on the --deliver-only run)")
        clone_url = url.replace("https://", f"https://x-access-token:{tok}@", 1)
    if protocol == "ssh":
        # Pin server identity from the mounted known_hosts (non-secret); key comes from gh_auth vault.
        kh = "/etc/github/known_hosts"
        if os.path.isfile(kh):
            env["GIT_SSH_COMMAND"] = f"ssh -o UserKnownHostsFile={kh} -o StrictHostKeyChecking=yes"

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    try:
        if os.path.isdir(os.path.join(dest, ".git")):
            log(f"deliver[{src.name}]: git pull {ref} -> {dest}")
            subprocess.run(["git", "-C", dest, "fetch", "--depth", "1", "origin", ref],
                           check=True, env=env)
            subprocess.run(["git", "-C", dest, "checkout", "-f", ref], check=True, env=env)
            subprocess.run(["git", "-C", dest, "reset", "--hard", f"origin/{ref}"], check=False, env=env)
        else:
            log(f"deliver[{src.name}]: git clone --depth 1 {url} ({ref}) -> {dest}")
            subprocess.run(["git", "clone", "--depth", "1", "--branch", ref, clone_url, dest],
                           check=True, env=env)
    except subprocess.CalledProcessError as e:
        die(f"deliver[{src.name}]: git delivery failed (rc={e.returncode})")


# --------------------------------------------------------------------------- #
# Document enumeration (INGEST phase) — applies ingest_scope AFTER delivery.
# Yields (doc_id, kind, payload, metadata):
#   kind == "text"   -> payload is the text string
#   kind == "image"  -> payload is the absolute image path
# --------------------------------------------------------------------------- #
def _matches(name: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, g) for g in globs)


def iter_documents(cfg: Config, src: Source) -> Iterator[tuple[str, str, str, dict]]:
    if src.adapter == "scripted":
        yield from _iter_scripted(cfg, src)
        return
    yield from _iter_on_disk(cfg, src)


def _iter_on_disk(cfg: Config, src: Source) -> Iterator[tuple[str, str, str, dict]]:
    root = source_root(cfg, src)
    if not os.path.isdir(root):
        log(f"ingest[{src.name}]: source root {root} absent — 0 docs "
            f"(deliver it, or drop files under {root})")
        return
    include = src.ingest_scope.get("include") or DEFAULT_TEXT_GLOBS
    exclude = src.ingest_scope.get("exclude") or []
    want_images = cfg.image_embed and _globs_want_images(include)

    for dirpath, _dirs, files in os.walk(root):
        for fn in sorted(files):
            if exclude and _matches(fn, exclude):
                continue
            if not _matches(fn, include):
                continue
            abspath = os.path.join(dirpath, fn)
            rel = os.path.relpath(abspath, root).replace(os.sep, "/")
            doc_id = f"{src.name}::{rel}"
            ext = os.path.splitext(fn)[1].lower()
            meta = {"source": src.name, "path": rel, "bundle": src.bundle,
                    "neuron": cfg.name, "collection": src.collection}
            if ext in IMAGE_EXTS:
                if want_images:
                    yield doc_id, "image", abspath, {**meta, "kind": "image"}
                continue
            try:
                text = open(abspath, encoding="utf-8", errors="replace").read()
            except OSError as e:
                log(f"ingest[{src.name}]: skip {rel} ({e})")
                continue
            yield doc_id, "text", text, {**meta, "kind": "text"}


def _globs_want_images(include: list[str]) -> bool:
    return any(os.path.splitext(g)[1].lower() in IMAGE_EXTS for g in include)


def _iter_scripted(cfg: Config, src: Source) -> Iterator[tuple[str, str, str, dict]]:
    """Run the provider script; each stdout line is a JSON object {"path":..,"text":..}.
    The script is a DATA SOURCE ONLY (never touches Chroma/Ollama). Per config-flow Phase 5 the
    provider scripts live under the impulses code-in seam at impulses/<bundle>/<neuron>/, mounted
    read-only into the container at IMPULSES_ROOT (default /impulses). The script path resolves
    against IMPULSES_ROOT/<bundle>/<neuron>/ (<neuron> = this input neuron's NEURON_NAME)."""
    script = src.script
    if not script:
        die(f"ingest[{src.name}]: scripted source needs a `script:` (consumption: script)")
    impulses_root = os.environ.get("IMPULSES_ROOT", "/impulses").rstrip("/")
    base = os.path.join(impulses_root, src.bundle, cfg.name)
    script_path = script if os.path.isabs(script) else os.path.join(base, script)
    if not os.path.isfile(script_path):
        die(f"ingest[{src.name}]: delivery script not found: {script_path}")
    log(f"ingest[{src.name}]: running scripted source {script_path}")
    try:
        proc = subprocess.run(["python", script_path], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        die(f"ingest[{src.name}]: delivery script failed (rc={e.returncode}): {e.stderr[:300]}")
    n = 0
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            log(f"ingest[{src.name}]: skip non-JSON script line: {line[:80]!r}")
            continue
        path = str(rec.get("path") or f"record-{n}")
        text = rec.get("text")
        if text is None:
            continue
        n += 1
        doc_id = f"{src.name}::{path}"
        meta = {"source": src.name, "path": path, "bundle": src.bundle,
                "neuron": cfg.name, "collection": src.collection, "kind": "text"}
        yield doc_id, "text", str(text), meta
