#!/usr/bin/env python3
r"""doc_provider_script.py — the DOCS source for input_neuron_example (impulses = YOUR code).
=============================================================================================

Config-flow refactor, Phase 8 — the SHIPPED example. This is a `consumption: script` source
(brain.env ===NEURONS=== zone, bundle `example_neuron_bundle`, neuron `input_neuron_example`,
source `docs`). A scripted source is a DATA SOURCE ONLY: it never touches Chroma or Ollama and
never writes vectors. The input neuron runs it and turns each record into a document.

CONTRACT (matches input_neurons/manifest.py::_iter_scripted):
  * The neuron invokes `python <this file>` and reads STDOUT.
  * Each non-empty stdout line is ONE JSON object → one document:
        {"path": "<id-ish relative path>", "text": "<the document text>"}
    `path` becomes the doc id suffix; `text` is chunked → embedded → upserted by the neuron.
  * Anything on STDERR is logging only (ignored by the consumer).

WHERE IT READS (Phase 8 fixture): knowledge/brain_ro/example_input_files/docs — 3 staged sample
docs (*.md / *.txt). In-container KNOWLEDGE_ROOT is /knowledge (the RO brain_ro mount), so the
default docs dir resolves to /knowledge/example_input_files/docs; override with DOCS_DIR.
"""
from __future__ import annotations

import json
import os
import sys

# ingest_scope.include in the zone already narrows to these; we mirror it so a direct
# `python doc_provider_script.py` on the host behaves the same as under the neuron.
TEXT_EXTS = (".md", ".txt")


def docs_dir() -> str:
    explicit = os.environ.get("DOCS_DIR")
    if explicit:
        return explicit
    root = os.environ.get("KNOWLEDGE_ROOT", "/knowledge")
    return os.path.join(root, "example_input_files", "docs")


def emit(path: str, text: str) -> None:
    sys.stdout.write(json.dumps({"path": path, "text": text}) + "\n")


def main() -> int:
    base = docs_dir()
    if not os.path.isdir(base):
        # A DATA SOURCE with nothing to offer is not an error — the neuron ingests 0 docs.
        sys.stderr.write(f"[doc_provider] docs dir absent: {base} — 0 docs\n")
        return 0
    n = 0
    for dirpath, _dirs, files in os.walk(base):
        for fn in sorted(files):
            if not fn.lower().endswith(TEXT_EXTS):
                continue
            abspath = os.path.join(dirpath, fn)
            rel = os.path.relpath(abspath, base).replace(os.sep, "/")
            try:
                text = open(abspath, encoding="utf-8", errors="replace").read()
            except OSError as e:
                sys.stderr.write(f"[doc_provider] skip {rel}: {e}\n")
                continue
            emit(rel, text)
            n += 1
    sys.stderr.write(f"[doc_provider] emitted {n} doc record(s) from {base}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
