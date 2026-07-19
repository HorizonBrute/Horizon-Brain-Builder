#!/usr/bin/env python3
r"""retrieve.py — the RAG core: retrieve THROUGH the gateway, then synthesize.
============================================================================

    question --embed(nomic-embed-text)--> vector
             --chroma query (k nearest)--> context chunks
             --llama3.2:1b over the context--> grounded answer (+ the sources it used)

The query embedder MUST match the model the docs were embedded with (nomic-embed-text) — a
mismatch returns nonsense neighbours. Everything is remote over the gateway (reader token).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from action_common import ChromaClient, Config, OllamaClient, log

_SYSTEM = (
    "You are a retrieval-augmented assistant for a Horizon AIOS brain. Answer the question "
    "using ONLY the context passages below. If the answer is not in the context, say so plainly "
    "instead of guessing. Be concise and cite the source path(s) you used."
)


@dataclass
class Answer:
    question: str
    answer: str
    collection: str
    sources: list[str] = field(default_factory=list)
    contexts: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "collection": self.collection,
            "sources": self.sources,
            "contexts": self.contexts,
        }


def _build_prompt(question: str, passages: list[dict]) -> str:
    blocks = []
    for i, p in enumerate(passages, 1):
        src = p.get("source_path") or p.get("source") or "?"
        blocks.append(f"[{i}] (source: {src})\n{p['text']}")
    context = "\n\n".join(blocks) if blocks else "(no relevant context found)"
    return f"{_SYSTEM}\n\n=== CONTEXT ===\n{context}\n\n=== QUESTION ===\n{question}\n\n=== ANSWER ==="


def answer_question(cfg: Config, chroma: ChromaClient, ollama: OllamaClient,
                    question: str, n_results: int = 5) -> Answer:
    question = (question or "").strip()
    if not question:
        return Answer(question="", answer="(empty question)", collection=cfg.collection)

    qvec = ollama.embed(question)
    res = chroma.query(cfg.collection, qvec, n_results)

    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]

    passages: list[dict] = []
    sources: list[str] = []
    for text, meta, dist in zip(docs, metas, dists):
        meta = meta or {}
        src = meta.get("path") or meta.get("source") or "?"
        passages.append({
            "text": text,
            "source": meta.get("source"),
            "source_path": src,
            "distance": dist,
            "doc_id": meta.get("doc_id"),
            "chunk": meta.get("chunk"),
        })
        if src not in sources:
            sources.append(src)

    if not passages:
        log(f"query returned 0 passages from '{cfg.collection}' — answering not-found")
        return Answer(question=question,
                      answer="I don't have anything indexed that answers that yet.",
                      collection=cfg.collection, sources=[], contexts=[])

    prompt = _build_prompt(question, passages)
    answer_text = ollama.generate(prompt)
    return Answer(question=question, answer=answer_text, collection=cfg.collection,
                  sources=sources, contexts=passages)
