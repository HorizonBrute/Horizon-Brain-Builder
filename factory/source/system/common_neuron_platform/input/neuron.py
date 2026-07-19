#!/usr/bin/env python3
r"""neuron.py — the input neuron entrypoint (WRITE side of a bundle, ADR-0015 / ADR-0017).
=========================================================================================

Batch job, not a daemon. Config comes ENTIRELY from the compose `environment:` block
(Config.from_env); this only parses the PHASE flags:

    neuron.py --ingest-only            # default: read brain_ro sources -> vectors in Chroma
    neuron.py --deliver-only           # write phase: git-clone/refresh sources into brain_ro
    neuron.py [--ingest-only] --tags daily   # narrow to sources carrying a cadence tag
    neuron.py ... --source <name>      # narrow to a single named source

Both phases act ONLY on this neuron's bundle (NEURON_BUNDLE); unlabeled sources belong to the
default bundle. Every backend hop goes through the gateway with the scoped bearer + X-Neuron-*
attribution. Exit code: 0 if every processed source finished without a per-doc error, else 1.
"""
from __future__ import annotations

import argparse
import sys

from ingest_common import ChromaClient, Config, OllamaClient, die, log
from ingest_docs import ingest_source
from manifest import deliver, load_manifest, resolve_sources


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Horizon AIOS input neuron (write side).")
    phase = ap.add_mutually_exclusive_group()
    phase.add_argument("--ingest-only", action="store_true",
                       help="(default) read delivered sources and write vectors to Chroma")
    phase.add_argument("--deliver-only", action="store_true",
                       help="write phase: fetch/refresh git sources into brain_ro, then stop")
    ap.add_argument("--tags", nargs="*", default=None,
                    help="only sources carrying one of these cadence tags")
    ap.add_argument("--source", default=None, help="only the named source")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    if cfg.role != "input":
        log(f"warning: NEURON_ROLE={cfg.role!r} (expected 'input') — proceeding as a writer anyway")

    man = load_manifest(cfg)
    sources = resolve_sources(man, cfg, args.tags)
    if args.source:
        sources = [s for s in sources if s.name == args.source]
    if not sources:
        log(f"no sources for bundle '{cfg.bundle}'"
            + (f" with tags {args.tags}" if args.tags else "")
            + (f" named '{args.source}'" if args.source else "")
            + " — nothing to do")
        return 0

    deliver_phase = args.deliver_only
    log(f"neuron '{cfg.name}' bundle '{cfg.bundle}' role '{cfg.role}': "
        f"{'DELIVER' if deliver_phase else 'INGEST'} {len(sources)} source(s) "
        f"-> collection default '{cfg.collection}'")

    if deliver_phase:
        for s in sources:
            deliver(cfg, s)
        log("deliver phase complete")
        return 0

    # Ingest phase.
    chroma = ChromaClient(cfg)
    ollama = OllamaClient(cfg)
    total_errors = 0
    for s in sources:
        r = ingest_source(cfg, s, chroma, ollama)
        total_errors += r.errors
        log(f"summary[{s.name}]: seen={r.docs_seen} ingested={r.docs_ingested} "
            f"unchanged={r.docs_unchanged} pruned={r.docs_deleted} "
            f"chunks={r.chunks_written} errors={r.errors} -> {r.collection}")
        try:
            log(f"collection '{r.collection}' now holds {chroma.count(chroma.get_or_create_collection(r.collection))} record(s)")
        except Exception as e:  # noqa: BLE001 — count is informational
            log(f"(count unavailable for {r.collection}: {e})")

    if total_errors:
        die(f"ingest finished with {total_errors} per-doc error(s) — see log above", code=1)
    log("ingest complete — all sources OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
