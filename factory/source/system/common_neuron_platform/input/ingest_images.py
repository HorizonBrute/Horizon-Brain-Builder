#!/usr/bin/env python3
r"""ingest_images.py — the image side of the write pipeline.
===========================================================

An image is NOT embedded directly (the default stack has no image embedder). It is turned
into TEXT first (a caption from an Ollama vision model, see image_embed.py), and that text
then rides the exact same chunk -> embed(nomic-embed-text) -> upsert path as a document — so
image knowledge is retrievable by the same semantic query as prose. This module is the single
entry the docs pipeline calls for a `kind == "image"` doc; the strategy details live in
image_embed.py, the file-extension recognition in manifest.py.
"""
from __future__ import annotations

from ingest_common import Config, OllamaClient
from image_embed import image_to_text as _image_to_text
from manifest import IMAGE_EXTS  # noqa: F401 — re-exported for callers that classify by ext

__all__ = ["image_to_text", "IMAGE_EXTS"]


def image_to_text(path: str, cfg: Config, src_image_embed: str | None,
                  src_caption_model: str | None, ollama: OllamaClient) -> str:
    """Resolve the image to embeddable caption text (source override > bundle default)."""
    return _image_to_text(path, cfg, src_image_embed, src_caption_model, ollama)
