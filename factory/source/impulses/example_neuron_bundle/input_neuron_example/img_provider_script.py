#!/usr/bin/env python3
r"""img_provider_script.py — the IMAGES source for input_neuron_example (impulses = YOUR code).
===============================================================================================

Config-flow refactor, Phase 8 — the SHIPPED example. This is the `images` source of neuron
`input_neuron_example` (brain.env ===NEURONS=== zone, `consumption: script`). Like its docs
sibling it is a DATA SOURCE ONLY: it never touches Chroma/Ollama and writes no vectors.

CONTRACT (matches input_neurons/manifest.py::_iter_scripted — the scripted path yields TEXT
records): each stdout line is one JSON object → one document:
        {"path": "<image relpath>", "text": "<embeddable text for this image>",
         "image_path": "<abs path>"}
  * `text` is the retrievable representation of the image. The current scripted consumer embeds
    THIS text directly (it does not run a vision model on scripted sources — that captioning path
    is the on-disk image branch, ingest_images.py). We therefore emit an HONEST, self-describing
    caption derived from real file facts (name + PNG pixel dimensions), so the smoke test ingests
    and answers with ZERO operator authoring and zero vision-model dependency.
  * `image_path` is carried as extra metadata (harmless to the current text-only consumer) so a
    future scripted-image contract can caption the real bytes instead — see the Phase 5 note.

WHERE IT READS: knowledge/brain_ro/example_input_files/imgs — 3 staged tiny PNGs. In-container
KNOWLEDGE_ROOT=/knowledge, so the default resolves to /knowledge/example_input_files/imgs;
override with IMGS_DIR.
"""
from __future__ import annotations

import json
import os
import struct
import sys

IMAGE_EXTS = (".png", ".jpg", ".jpeg")


def imgs_dir() -> str:
    explicit = os.environ.get("IMGS_DIR")
    if explicit:
        return explicit
    root = os.environ.get("KNOWLEDGE_ROOT", "/knowledge")
    return os.path.join(root, "example_input_files", "imgs")


def png_dimensions(path: str):
    """(width, height) from a PNG IHDR, or None if not a readable PNG (stdlib only, no PIL)."""
    try:
        with open(path, "rb") as fh:
            sig = fh.read(8)
            if sig != b"\x89PNG\r\n\x1a\n":
                return None
            fh.read(4)              # IHDR length
            if fh.read(4) != b"IHDR":
                return None
            w, h = struct.unpack(">II", fh.read(8))
            return w, h
    except OSError:
        return None


def caption(rel: str, dims) -> str:
    name = os.path.splitext(os.path.basename(rel))[0].replace("_", " ").replace("-", " ").strip()
    dim_str = f"{dims[0]}x{dims[1]} pixels" if dims else "unknown size"
    return (f"Sample image '{name}' ({rel}), a {dim_str} PNG staged as a first-run example "
            f"fixture for the Horizon AIOS example_neuron_bundle image-ingest smoke test.")


def emit(rel: str, text: str, abspath: str) -> None:
    sys.stdout.write(json.dumps({"path": rel, "text": text, "image_path": abspath}) + "\n")


def main() -> int:
    base = imgs_dir()
    if not os.path.isdir(base):
        sys.stderr.write(f"[img_provider] imgs dir absent: {base} — 0 images\n")
        return 0
    n = 0
    for dirpath, _dirs, files in os.walk(base):
        for fn in sorted(files):
            if not fn.lower().endswith(IMAGE_EXTS):
                continue
            abspath = os.path.join(dirpath, fn)
            rel = os.path.relpath(abspath, base).replace(os.sep, "/")
            emit(rel, caption(rel, png_dimensions(abspath)), abspath)
            n += 1
    sys.stderr.write(f"[img_provider] emitted {n} image record(s) from {base}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
