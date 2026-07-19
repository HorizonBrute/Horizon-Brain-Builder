#!/usr/bin/env python3
r"""ingest_docs.py — the WRITE pipeline: source docs -> chunks -> vectors -> Chroma.
===================================================================================

For each source in this bundle:
  1. enumerate docs (manifest.iter_documents, ingest_scope already applied);
  2. dedup by content hash — skip unchanged docs; for a CHANGED doc, delete its old chunks first;
  3. chunk text offline (images are captioned to text first, see ingest_images);
  4. embed each chunk via Ollama nomic-embed-text THROUGH THE GATEWAY;
  5. upsert (ids/embeddings/documents/metadatas) into the bundle's collection THROUGH THE GATEWAY.

Every backend call carries the scoped writer bearer + X-Neuron-* attribution headers, so the
gateway's content-capture log attributes each write per bundle/role/neuron (ADR-0015).
"""
from __future__ import annotations

from dataclasses import dataclass

from ingest_common import (ChromaClient, Config, DedupState, OllamaClient,
                           chunk_text, content_hash, log)
from ingest_images import image_to_text
from manifest import Source, iter_documents


@dataclass
class SourceResult:
    source: str
    collection: str
    docs_seen: int = 0
    docs_ingested: int = 0
    docs_unchanged: int = 0
    docs_deleted: int = 0
    chunks_written: int = 0
    errors: int = 0


def ingest_source(cfg: Config, src: Source, chroma: ChromaClient,
                  ollama: OllamaClient) -> SourceResult:
    res = SourceResult(source=src.name, collection=src.collection)
    cid = chroma.get_or_create_collection(
        src.collection,
        metadata={"bundle": src.bundle, "embed_model": src.embed_model},
    )
    state = DedupState(cfg, src.collection)
    prior_ids = state.known_ids()   # doc_ids from a previous run — an update, not a first write
    seen_ids: set[str] = set()

    for doc_id, kind, payload, meta in iter_documents(cfg, src):
        res.docs_seen += 1
        seen_ids.add(doc_id)
        try:
            if kind == "image":
                text = image_to_text(payload, cfg, src.image_embed,
                                     src.image_caption_model, ollama)
                meta = {**meta, "captioned": True, "image_path": meta.get("path")}
            else:
                text = payload

            h = content_hash(text.encode("utf-8"))
            if state.unchanged(doc_id, h):
                res.docs_unchanged += 1
                continue

            # Only an UPDATE (doc_id seen in a prior run) needs its old chunks dropped first;
            # a brand-new doc has nothing to delete (and first-ingest then never needs delete perms).
            if doc_id in prior_ids:
                chroma.delete_where(cid, {"doc_id": doc_id})

            chunks = chunk_text(text, src.chunk_size, src.chunk_overlap)
            if not chunks:
                state.record(doc_id, h)
                continue

            ids, embs, docs, metas = [], [], [], []
            for i, ch in enumerate(chunks):
                ids.append(f"{doc_id}::{i}")
                embs.append(ollama.embed(src.embed_model, ch))
                docs.append(ch)
                metas.append({**meta, "doc_id": doc_id, "chunk": i, "chunks": len(chunks)})
            chroma.add(cid, ids, embs, docs, metas)

            state.record(doc_id, h)
            res.docs_ingested += 1
            res.chunks_written += len(chunks)
            log(f"ingest[{src.name}]: {doc_id} -> {len(chunks)} chunk(s)")
        except Exception as e:  # noqa: BLE001 — one bad doc must not sink the whole run
            res.errors += 1
            log(f"ingest[{src.name}]: FAILED {doc_id}: {e}")

    # Prune docs that vanished from the source since last run (their chunks + state entry).
    # SCOPE to THIS source: doc_ids are namespaced `<source>::<path>` (manifest.iter_documents),
    # and DedupState is keyed by COLLECTION — so two sources sharing one collection see each
    # other's ids in known_ids(). Pruning the unfiltered difference makes each source delete the
    # OTHER's records every run (root-caused 2026-07-13: the `images` source wiped the `docs`
    # source's records from example_docs, so /ask retrieved only image captions). Only prune ids
    # this source owns.
    src_prefix = f"{src.name}::"
    for gone in {i for i in state.known_ids() if i.startswith(src_prefix)} - seen_ids:
        chroma.delete_where(cid, {"doc_id": gone})
        state.forget(gone)
        res.docs_deleted += 1
        log(f"ingest[{src.name}]: pruned removed doc {gone}")

    state.save()
    return res
