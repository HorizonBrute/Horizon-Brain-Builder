#!/usr/bin/env python3
r"""
seed_neuron_tokens.py - provision-time auto-mint of a brain's NAMED neuron tokens.
==================================================================================

The config-flow refactor made neuron tokens NAMED references (Phase 3): a neuron declares
`gateway_token: <name>` in the brain.env YAML zone, and gateway_config.resolve_neuron_tokens
fails CLOSED if that name isn't in the gateway token_registry. This tool is the PROVISION-TIME
bootstrap that lets a fresh install "just work": it walks the neuron zone and mints any named
token that does not yet exist, with the type-default grant (input = chroma:writer, action =
chroma:reader; both + ollama:use so the neuron can embed / synthesize). Idempotent - a label that
already exists is REUSED untouched (a working token is never rotated here).

WHY A SEPARATE STEP (not folded into gateway_config generate)
-------------------------------------------------------------
generate stays fail-closed on purpose: an operator who edits brain.env, adds a neuron, and
re-applies must get a loud "create it with gateway_tokens.py" - not a silent mint of a wrong
token. Auto-mint is therefore an EXPLICIT deploy step (the orchestrators call it before generate),
so only a full (re)deploy seeds the shipped brain's tokens; the day-to-day edit path keeps its
guardrail. This is the deploy-time companion to Phase 3, not a re-introduction of add_neuron_bundle's
old auto-mint (which minted brain-wide shared tokens and wrote dead flat keys into brain.env).

ACTION ADMISSION TOKEN (--action-caller)
----------------------------------------
A neuron's own `gateway_token:` is its BACKEND bearer (an action neuron reads chroma). The world ->
:8443 query-API admission is a SEPARATE `action:call` grant (see gateway_tokens GRANT_MAPS). With
--action-caller this tool also ensures ONE such admission token exists and PRINTS it once, so a
LAN/world client can reach a published action neuron. Minted only when the zone has an action
neuron; the raw secret is shown once (save it) and never rotated on re-run.

Runs HOST-SIDE as the brain's operator (writes the operator-owned seam:
brain_etc/gateway/token_registry + the generated *_tokens.map). Same registry model as
gateway_tokens.py (ADR 0010) - list/rotate/revoke there interoperate byte-for-byte.
"""
from __future__ import annotations

import argparse
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

import brain_env               # sibling: the two-zone brain.env loader + neuron accessor API
import gateway_tokens          # sibling: the unified token registry + nginx map generator (ADR 0010)

HERE = Path(__file__).resolve().parent
BRAIN_DIR = HERE.parent.parent

# The label of the shared world->action-API admission token (--action-caller). One per brain:
# it gates WORLD -> :8443 admission, not any single neuron (each action neuron still carries its
# own scoped reader token via its `gateway_token:`).
ACTION_CALLER_LABEL = "action-caller"
ACTION_CALLER_GRANTS = ["action:call"]

# Every neuron token also carries ollama:use so the neuron can reach the model server through the
# gateway (input neurons embed; action neurons synthesize). The chroma tier comes from the type
# default (gateway_tokens.DEFAULT_NEURON_GRANT): input -> chroma:writer, action -> chroma:reader.
_OLLAMA_GRANT = "ollama:use"


def info(m): print(f"[seed-tokens] {m}")
def die(m): print(f"[seed-tokens] ERROR: {m}", file=sys.stderr); sys.exit(1)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _neuron_grants(ntype: str) -> list:
    """The default grant set a neuron of this type is minted with: its chroma tier (writer for
    input, reader for action) plus ollama:use. An unknown type falls back to reader-only (safe)."""
    chroma = gateway_tokens.DEFAULT_NEURON_GRANT.get(ntype, "chroma:reader")
    return [chroma, _OLLAMA_GRANT]


def seed(bd=BRAIN_DIR, action_caller=False) -> dict:
    """Ensure every named neuron token in the brain.env YAML zone exists in the registry.

    Returns a summary { "minted": [...], "reused": [...], "action_caller": token_or_None }.
    Mints missing labels with the type-default grant; reuses existing labels untouched. Writes the
    registry + regenerates the nginx maps ONCE at the end (only if something changed, but the maps
    are always regenerated so a fresh deploy has current artifacts)."""
    try:
        neurons = brain_env.load_neurons(brain_env.brain_env_path(bd))
    except brain_env.BrainEnvError as e:
        die(f"cannot read the neuron zone of brain.env: {e}")

    entries = gateway_tokens.read_registry(bd)
    by_label = {e.label: e for e in entries if e.label}
    minted, reused, changed = [], [], False

    for bundle, n in brain_env.iter_neurons(neurons):
        label = n.get("gateway_token")
        nname, ntype = n.get("name"), n.get("type")
        if not label:
            info(f"neuron {nname!r} (bundle {bundle.get('name')!r}) names no gateway_token - "
                 f"skipping (it will reach no token-gated backend).")
            continue
        if label in by_label:
            reused.append(label)
            continue
        grants = _neuron_grants(ntype)
        entry = gateway_tokens.Entry(secrets.token_hex(32), grants, label, _now())
        entries.append(entry)
        by_label[label] = entry
        minted.append((label, grants))
        changed = True
        info(f"minted {label!r} ({gateway_tokens.fingerprint(entry.token)}) "
             f"grants={grants} for {ntype} neuron {nname!r}")

    caller_token = None
    if action_caller and any(True for _b, _n in brain_env.iter_neurons(neurons, "action")):
        existing = by_label.get(ACTION_CALLER_LABEL)
        if existing:
            caller_token = existing.token
            reused.append(ACTION_CALLER_LABEL)
        else:
            entry = gateway_tokens.Entry(secrets.token_hex(32), list(ACTION_CALLER_GRANTS),
                                         ACTION_CALLER_LABEL, _now())
            entries.append(entry)
            caller_token = entry.token
            minted.append((ACTION_CALLER_LABEL, ACTION_CALLER_GRANTS))
            changed = True
            info(f"minted {ACTION_CALLER_LABEL!r} ({gateway_tokens.fingerprint(entry.token)}) "
                 f"grants={ACTION_CALLER_GRANTS} - world->action-API admission")

    if changed:
        gateway_tokens.write_registry(entries, bd)
    # Always regenerate the maps so a fresh deploy has current artifacts even when nothing minted.
    counts = gateway_tokens.generate(entries, gateway_tokens.gateway_dir(bd))
    info(f"registry: {len(minted)} minted, {len(reused)} reused; "
         f"maps -> {', '.join(f'{k}={v}' for k, v in counts.items())}")
    return {"minted": minted, "reused": reused, "action_caller": caller_token}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Provision-time auto-mint of a brain's NAMED neuron tokens (config-flow "
                    "Phase 3). Idempotent; run host-side as the brain's operator before "
                    "gateway_config generate.")
    ap.add_argument("--brain-dir", default=None, help="brain root (default: this tool's brain)")
    ap.add_argument("--action-caller", action="store_true",
                    help="also ensure a world->action-API admission token (action:call), shown once")
    args = ap.parse_args(argv)
    bd = Path(args.brain_dir) if args.brain_dir else BRAIN_DIR

    result = seed(bd, action_caller=args.action_caller)
    caller = result.get("action_caller")
    if caller:
        print("\n" + "=" * 64)
        print(f"  ACTION admission token  (label={ACTION_CALLER_LABEL})  grant=action:call")
        print(f"  Authorization: Bearer {caller}")
        print("  ^ present this to reach a published action neuron's query API (:8443).")
        print("    Shown ONCE - save it; rotate/revoke via gateway_tokens.py.")
        print("=" * 64 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
