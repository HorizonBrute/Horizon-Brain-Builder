#!/usr/bin/env python3
"""
gateway_token.py — Chroma reader/writer bearer tokens (COMPAT shim over the registry).
======================================================================================

As of ADR projects/2ndbraindevelopment/decisions/0010-unified-bearer-token-registry.md,
the single source of truth for EVERY bearer token (chroma reader/writer AND ollama
use/admin) is `brain_etc/gateway/token_registry`, and the per-service nginx `map`
files are GENERATED from it by `gateway_tokens.py`. This tool is kept as a
back-compatible surface for the common Chroma case so existing operator habit and
scripts keep working:

    gateway_token.py list
    gateway_token.py create --label obsidian-readback [--role writer|reader]
    gateway_token.py rotate --label obsidian-readback
    gateway_token.py revoke --label obsidian-readback

It simply translates the old role vocabulary to registry grants and delegates to the
gateway_tokens library:

    --role writer  ->  grant chroma:writer
    --role reader  ->  grant chroma:reader

For multi-service grants (a single token that reads Chroma AND runs Ollama inference,
an Ollama-admin token, etc.) use `gateway_tokens.py grant --grant chroma:reader
--grant ollama:use …` directly — that is the full registry-native tool.

Everything else — WHERE it runs (host-side, as the brain's OPERATOR), the probe-write
ACL gate, LF discipline, print-the-secret-once hygiene, and the sync+force-recreate to
go live — is unchanged and now lives in gateway_tokens.py (imported below).
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gateway_tokens as gt   # shared registry model + generator + apply  # noqa: E402

ROLE_GRANT = {"writer": "chroma:writer", "reader": "chroma:reader"}


def main():
    ap = argparse.ArgumentParser(
        description="Manage Chroma gateway reader/writer tokens (compat shim over the "
                    "unified token_registry —; use gateway_tokens.py for "
                    "multi-service grants). Run host-side as the brain's operator.")
    ap.add_argument("--brain", default=gt.BRAIN_DIR.name,
                    help=f"brain name (default from this tool's folder: {gt.BRAIN_DIR.name})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show labels + fingerprints + grants (never the tokens)")

    c = sub.add_parser("create", help="mint a new token (printed once)")
    c.add_argument("--label", required=True, help="unique human label, e.g. obsidian-readback")
    c.add_argument("--role", choices=("writer", "reader"), default="writer",
                   help="writer (default) or reader")

    r = sub.add_parser("revoke", help="remove a token by label + regenerate")
    r.add_argument("--label", required=True)

    ro = sub.add_parser("rotate", help="revoke + re-mint under the same label/grants")
    ro.add_argument("--label", required=True)

    args = ap.parse_args()

    if args.cmd == "list":
        return gt.cmd_list(args)

    gt.require_operator()
    if args.cmd == "create":
        args.grant = [ROLE_GRANT[args.role]]
        return gt.cmd_grant(args)
    if args.cmd == "revoke":
        return gt.cmd_revoke(args)
    if args.cmd == "rotate":
        args.grant = []          # keep the existing grants on rotate
        return gt.cmd_rotate(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
