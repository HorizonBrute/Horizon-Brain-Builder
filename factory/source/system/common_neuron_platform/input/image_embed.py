#!/usr/bin/env python3
r"""image_embed.py — turn an image into something embeddable.
============================================================

Two strategies (NEURON_IMAGE_EMBED, a source may override):

  caption (default) — an Ollama VISION model (NEURON_IMAGE_CAPTION_MODEL, default moondream)
                      describes the image; the caption is returned as text and embedded by the
                      normal text path (nomic-embed-text). The vision model must be pre-pulled
                      into ollama out-of-band (admin-only, through the gateway) — the neuron has
                      no internet to pull it at run.
  clip              — TRUE image embeddings. STUB: needs a gateway'd CLIP sidecar; raises a
                      clear error so a misconfigured source fails loudly rather than silently.
"""
from __future__ import annotations

import base64
import os

from ingest_common import Config, GatewayError, OllamaClient, log

_CAPTION_PROMPT = (
    "Describe this image in detail for a search index. Include any visible text verbatim, "
    "the subject, notable objects, and context. Be factual and concise."
)


def caption_image(path: str, ollama: OllamaClient, model: str) -> str:
    with open(path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    caption = ollama.generate(model, _CAPTION_PROMPT, images=[b64],
                              options={"temperature": 0.0})
    if not caption:
        raise GatewayError(f"caption model '{model}' returned an empty description for {path}")
    return caption


def image_to_text(path: str, cfg: Config, src_image_embed: str | None,
                  src_caption_model: str | None, ollama: OllamaClient) -> str:
    """Resolve the strategy (source override > bundle default) and return embeddable text."""
    strategy = (src_image_embed or cfg.image_embed or "caption").lower()
    if strategy == "clip":
        raise GatewayError(
            f"image_embed=clip is a STUB (needs a gateway'd CLIP sidecar); "
            f"set NEURON_IMAGE_EMBED=caption for {os.path.basename(path)}")
    if strategy != "caption":
        raise GatewayError(f"unknown image_embed strategy '{strategy}' for {os.path.basename(path)}")
    model = src_caption_model or cfg.image_caption_model or "moondream"
    log(f"caption[{os.path.basename(path)}]: vision model {model}")
    return caption_image(path, ollama, model)
