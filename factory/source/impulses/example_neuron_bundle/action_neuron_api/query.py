#!/usr/bin/env python3
r"""query.py — the action app for example_neuron_bundle (impulses = YOUR code, Phase 8).
========================================================================================

Config-flow refactor, Phase 8 — the SHIPPED example ACTION app. It is the `app.executable`
the brain.env ===NEURONS=== zone names for BOTH action neurons of `example_neuron_bundle`:

    action_neuron_cli  service_type: on-demand  (one-shot; NOT published)
    action_neuron_api  service_type: daemon     (served :8443 query API via the gateway)

Both neurons declare `dir:` empty, so each resolves to its OWN impulses dir
(impulses/example_neuron_bundle/action_neuron_{cli,api}/) — hence this file is staged in BOTH.
It is byte-identical in each; the ONLY difference between the two neurons is service_type, which
neuron_compose.py turns into the compose shape (one-shot `run` vs `--serve`). See the Phase 5 note
returned to the coordinator on collapsing the two copies once compose sources a shared app dir.

The READ side of the bundle: retrieve from the '{collection}' chroma collection + synthesize a
grounded answer with the neuron's synth_model, reaching chroma/ollama ONLY through the gateway
(ADR-0015) with a reader-scoped bearer — it physically cannot write. All wiring comes from the
compose `environment:` block (Config.from_env); this file only parses the run mode.

MODES (exactly the argv neuron_compose.py generates):
    query.py --query "how do I ...?" [--k 5] [--json]     # on-demand one-shot: print + exit
    query.py --serve [--host 0.0.0.0] [--port 8080]        # daemon: long-running query API

The shared action image (build context /opt/action_neurons) provides action_common / retrieve /
serve; this thin entrypoint reuses them — same contract as the platform action.py it supersedes.
"""
from __future__ import annotations

import argparse
import json
import sys

from action_common import ChromaClient, Config, GatewayError, OllamaClient, die, log
from retrieve import answer_question


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="example_neuron_bundle action app (read side).")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--query", metavar="TEXT", help="one-shot question; print the answer and exit")
    mode.add_argument("--serve", action="store_true", help="run the long-running query API (daemon)")
    ap.add_argument("--k", "--n-results", dest="k", type=int, default=5,
                    help="number of context chunks to retrieve (default 5)")
    ap.add_argument("--json", action="store_true", help="one-shot: print the full answer object as JSON")
    ap.add_argument("--host", default="0.0.0.0", help="serve bind host (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8080, help="serve bind port (default 8080)")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    if cfg.role != "action":
        log(f"warning: NEURON_ROLE={cfg.role!r} (expected 'action') — proceeding as a reader anyway")

    if args.serve:
        from serve import serve  # imported lazily so a one-shot query needs no http machinery
        return serve(cfg, args.host, args.port)

    chroma = ChromaClient(cfg)
    ollama = OllamaClient(cfg)
    try:
        ans = answer_question(cfg, chroma, ollama, args.query, n_results=args.k)
    except GatewayError as e:
        die(f"query failed: {e}")

    if args.json:
        print(json.dumps(ans.to_dict(), indent=2))
    else:
        print(ans.answer)
        if ans.sources:
            print("\n--- sources ---", file=sys.stderr)
            for s in ans.sources:
                print(f"  - {s}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
