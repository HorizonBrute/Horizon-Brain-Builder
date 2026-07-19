#!/usr/bin/env python3
r"""action.py — the action neuron entrypoint (READ side of a bundle, ADR-0015 / ADR-0017).
=========================================================================================

Two modes (config comes ENTIRELY from the compose `environment:` block):

    action.py --query "how do I ...?" [--k 5] [--json]   # one-shot: retrieve+synthesize, print
    action.py --serve [--host 0.0.0.0] [--port 8080]     # long-running query API (action_api)

The one-shot form is driven by `docker compose ... run --rm --no-deps <bundle>_action_1
--query "..."`; the daemon form is the <bundle>_action_api service (compose overrides CMD).
Reader token only: this side physically cannot write to Chroma.
"""
from __future__ import annotations

import argparse
import json
import sys

from action_common import ChromaClient, Config, GatewayError, OllamaClient, die, log
from retrieve import answer_question


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Horizon AIOS action neuron (read side).")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--query", metavar="TEXT", help="one-shot question; print the answer and exit")
    mode.add_argument("--serve", action="store_true", help="run the long-running query API")
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

    # One-shot query.
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
