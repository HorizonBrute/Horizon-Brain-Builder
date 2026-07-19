#!/usr/bin/env python3
r"""
neuron_compose.py — render the DEFAULT bundle's compose blocks FROM the brain.env zone.
=======================================================================================

Config-flow refactor, Phase 4 (the Phase 4<->5 seam). The DEFAULT neuron bundle used to be
HAND-AUTHORED in brain_etc/docker/compose.yaml as three `__BRAIN_NAME__`-templated services
(`__BRAIN_NAME___input_1`, `__BRAIN_NAME___action_1`, `__BRAIN_NAME___action_api`). Those names
never matched the neuron zone (`example_neuron_bundle` / `input_neuron_example` / …), and their
tokens/model/collection were flat `.env` keys divorced from the neuron object. This tool GENERATES
the default bundle's service blocks from the brain.env YAML neuron zone — exactly like
add_neuron_bundle.py already generates the ADDITIONAL bundles — so names, collection, synthesis
model and the per-neuron gateway token all come from the neuron object (single source of truth).

WHAT IT RENDERS (into a managed region of compose.yaml, between the `>>> … DEFAULT bundle …`
markers — never hand-edit the region):

  * one INPUT service per input neuron of the default bundle (the FIRST carries the shared
    `build: /opt/input_neurons` context; the rest reuse the image);
  * one ACTION service per action neuron (the FIRST action carries `build: /opt/action_neurons`;
    the rest reuse the image). `app.service_type` selects the compose shape (ADR-0020 Q1):
        on-demand → restart:"no", no long-running command (driven by `docker compose run`);
        daemon    → restart:unless-stopped, `--serve --host 0.0.0.0 --port <INTERNAL_SERVE_PORT>`
                    (the gateway's :8443 path-router fronts it — publish_to_lan_ports).

Each neuron reads its gateway bearer from its OWN per-neuron key
`NEURON_TOKEN__<bundle>__<neuron>` (brain_env.neuron_token_env_key / .env.rendered appendix), so
two neurons never share a flat `NEURON_GATEWAY_TOKEN`/`ACTION_GATEWAY_TOKEN`. The container env var
NAME the neuron code reads is unchanged (`NEURON_GATEWAY_TOKEN` / `ACTION_GATEWAY_TOKEN`); only its
VALUE is now sourced per-neuron.

Wired into gateway_config.cmd_generate (the one host-side "materialize from brain.env" step both
the deploy and the reapply/keepalive call), so the default block is always in sync with the zone.
The shipped brain_etc.example/docker/compose.yaml carries a PRE-RENDERED region (identical bytes to
a generate run) so a bare `docker compose up` works before the first apply.

Everything is written as LF bytes (a stray CR has bitten this seam — see gateway_config).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import brain_env               # the two-zone brain.env loader + the per-neuron token key
import neuron_topology as nt   # the per-bundle neuron-net addressing scheme (ADR-0015)

HERE = Path(__file__).resolve().parent
BRAIN_DIR = HERE.parent.parent

COMPOSE_REL = "brain_etc/docker/compose.yaml"

# The daemon action neuron's INTERNAL serve port (plain HTTP on neuron_net). The gateway's
# :8443 path-router (action.conf) resolves `/{bundle}/{neuron}/…` to `<neuron>:<this port>`;
# route_registry's default is the SAME 8080, so a standard daemon needs no registry entry.
INTERNAL_SERVE_PORT = 8080

BEGIN = "  # >>> BEGIN generated DEFAULT bundle neurons (neuron_compose.py — do NOT hand-edit) >>>"
END = "  # >>> END generated DEFAULT bundle neurons <<<"


def info(m): print(f"[neuron-compose] {m}")
def die(m): print(f"[neuron-compose] ERROR: {m}", file=sys.stderr); sys.exit(1)


# --------------------------------------------------------------------------- #
# Resolve the default bundle out of the brain.env zone
# --------------------------------------------------------------------------- #
def resolve_default_bundle_obj(flat: dict, neurons: dict):
    """The DEFAULT bundle OBJECT from the neuron zone: the bundle whose name == DEFAULT_BUNDLE
    (brain.env), else the FIRST bundle in the zone (the sane fallback the shipped template relies
    on — the example zone declares exactly one bundle). Returns None if the zone has no bundles."""
    bundles = list(brain_env.iter_bundles(neurons))
    if not bundles:
        return None
    want = brain_env.default_bundle_name(flat)
    for b in bundles:
        if b.get("name") == want:
            return b
    return bundles[0]


def _token_ref(bundle_name: str, neuron_name: str) -> str:
    """The compose `${…:?}` reference a neuron reads its bearer from — the per-neuron key
    (brain_env.neuron_token_env_key). Fail-closed at `compose up` if the .env appendix is
    missing (gateway_config.resolve_neuron_tokens is the loud generate-time gate)."""
    key = brain_env.neuron_token_env_key(bundle_name, neuron_name)
    return "${" + key + ":?missing per-neuron token; run gateway_config generate to render ~/docker/.env}"


def _bundle_embed_model(bundle: dict) -> str:
    """The embed model the bundle's docs are written with = the FIRST input neuron's `embed_model`
    (default nomic-embed-text). The action side MUST query with the SAME model, so its
    ACTION_EMBED_MODEL is derived from here rather than hardcoded."""
    for n in (bundle.get("neurons") or []):
        if n.get("type") == "input" and n.get("embed_model"):
            return str(n["embed_model"])
    return "nomic-embed-text"


# --------------------------------------------------------------------------- #
# Service block renderers (faithful to the proven hand-authored default blocks,
# with names/collection/model/token now sourced from the neuron object)
# --------------------------------------------------------------------------- #
def render_input_block(bundle: dict, neuron: dict, *, net_key: str = "neuron_net",
                       with_build: bool = False, bundle_kind: str = "DEFAULT bundle") -> str:
    """Render one INPUT neuron's compose service block from the RICH zone objects. Shared by the
    DEFAULT-bundle region (neuron_compose.render_region) AND the additional-bundle region
    (add_neuron_bundle.render_region) — the ONE implementation of an input service block, so the
    two regions can never drift (config-flow P0 unification). Topology varies ONLY by `net_key`
    (default `neuron_net`; additional `<bundle>_neuron_net`) and `with_build` (only the default
    bundle's first input neuron carries the shared image build context). `bundle_kind` is a comment
    label ('DEFAULT bundle' | 'bundle') — its default keeps the default region byte-identical."""
    bundle_name = str(bundle.get("name"))
    name = str(neuron.get("name"))
    collection = brain_env.bundle_collection(bundle)
    build = "    build:\n      context: /opt/input_neurons\n" if with_build else ""
    build_note = ("carries the shared input-image build context"
                  if with_build else "reuses the shared input image")
    return f"""  # --- {name} (generated from the brain.env zone) — {bundle_kind} '{bundle_name}' INPUT neuron ---
  # WRITE side: turns knowledge/brain_ro into vectors in the '{collection}' collection. Reaches
  # chroma/ollama ONLY through the gateway (ADR-0015: neuron_net only), carrying its OWN named
  # bearer (Phase 4 per-neuron token). This neuron {build_note}.
  {name}:
    profiles: ["neurons"]
    image: ${{BRAIN_NAME}}-input_neurons
{build}    container_name: ${{BRAIN_NAME}}-{name}
    restart: "no"
    # Immutable execution surface: read-only rootfs so baked code can't be rewritten at runtime.
    read_only: true
    tmpfs:
      - /tmp
    depends_on:
      gateway:
        condition: service_started
    env_file:
      - ./github/github.env
    environment:
      CHROMA_URL: "http://chroma:8000"
      OLLAMA_URL: "http://ollama:11434"
      # Per-neuron bearer (config-flow Phase 4): the VALUE comes from this neuron's own
      # NEURON_TOKEN__{bundle_name}__{name} key in ~/docker/.env; the code still reads NEURON_GATEWAY_TOKEN.
      NEURON_GATEWAY_TOKEN: "{_token_ref(bundle_name, name)}"
      KNOWLEDGE_ROOT: "/knowledge"
      # Provider scripts (consumption: script) live under the impulses code-in seam,
      # mounted RO below; manifest.py resolves script: under IMPULSES_ROOT/<bundle>/<neuron>/.
      IMPULSES_ROOT: "/impulses"
      NEURON_MANIFEST: "/etc/neuron/sources.yaml"
      NEURON_STATE_DIR: "/state"
      # Attribution (X-Neuron-Bundle / -Role / -Name). NEURON_BUNDLE also filters which sources
      # this input neuron ingests (only bundle '{bundle_name}'s).
      NEURON_BUNDLE: "{bundle_name}"
      NEURON_ROLE: "input"
      NEURON_NAME: "{name}"
      BUNDLE_COLLECTION: "{collection}"
      NEURON_IMAGE_EMBED: "caption"
      NEURON_IMAGE_CAPTION_MODEL: "moondream"
    command: ["--ingest-only"]
    volumes:
      - ./neuron:/etc/neuron:ro
      - ./github:/etc/github:ro
      - /home/${{BRAIN_NAME}}/knowledge/brain_ro:/knowledge:ro
      # impulses code-in seam (config-flow Phase 5): YOUR provider scripts, RO.
      - /home/${{BRAIN_NAME}}/impulses:/impulses:ro
      - neuron_state:/state
    networks:
      - {net_key}
"""


def render_action_block(bundle: dict, neuron: dict, *, embed_model: str, net_key: str = "neuron_net",
                        with_build: bool = False, bundle_kind: str = "DEFAULT bundle") -> str:
    """Render one ACTION neuron's compose service block from the RICH zone objects. Shared by the
    DEFAULT-bundle region AND the additional-bundle region (config-flow P0 unification) — the ONE
    implementation of an action service block. `synth_model` (per-neuron) + `service_type`
    (daemon/on-demand shape) + the per-neuron token all come from the neuron object; `embed_model`
    is the bundle's (derived from its input neuron, so /ask queries with the SAME model the docs
    were embedded with — closes C8). Topology varies only by `net_key` + `with_build`; `bundle_kind`
    keeps the default region byte-identical."""
    bundle_name = str(bundle.get("name"))
    name = str(neuron.get("name"))
    collection = brain_env.bundle_collection(bundle)
    synth_model = neuron.get("synth_model") or "qwen2.5:1.5b"
    service_type = (neuron.get("app") or {}).get("service_type") or "on-demand"
    build = "    build:\n      context: /opt/action_neurons\n" if with_build else ""
    daemon = service_type == "daemon"
    if daemon:
        restart = "    restart: unless-stopped"
        command = (f'    # Long-running query API on {net_key}; the gateway proxies :8443 -> here\n'
                   f'    # (action.conf path-router). publish_to_lan_ports declares that publish.\n'
                   f'    command: ["--serve", "--host", "0.0.0.0", "--port", "{INTERNAL_SERVE_PORT}"]\n')
        shape = "daemon — served HTTP query API (gateway routes :8443 to it)"
    else:
        restart = '    restart: "no"'
        command = ('    # One-shot: no long-running command; driven by\n'
                   f'    #   docker compose run --rm --no-deps {name} --query "…"\n')
        shape = "on-demand — a one-shot CLI (docker compose run), never published"
    build_note = ("carries the shared action-image build context"
                  if with_build else "reuses the shared action image")
    return f"""  # --- {name} (generated from the brain.env zone) — {bundle_kind} '{bundle_name}' ACTION neuron ---
  # READ side: retrieves from '{collection}' + synthesizes an answer. Carries a chroma:reader +
  # ollama:use bearer, so it PHYSICALLY cannot write (write-funnel invariant, gateway-enforced).
  # service_type={service_type} => {shape}. This neuron {build_note}.
  {name}:
    profiles: ["neurons"]
    image: ${{BRAIN_NAME}}-action_neurons
{build}    container_name: ${{BRAIN_NAME}}-{name}
{restart}
    read_only: true
    tmpfs:
      - /tmp
    depends_on:
      gateway:
        condition: service_started
    environment:
      CHROMA_URL: "http://chroma:8000"
      OLLAMA_URL: "http://ollama:11434"
      # Per-neuron bearer (config-flow Phase 4): VALUE from NEURON_TOKEN__{bundle_name}__{name} in
      # ~/docker/.env; the code still reads ACTION_GATEWAY_TOKEN.
      ACTION_GATEWAY_TOKEN: "{_token_ref(bundle_name, name)}"
      NEURON_BUNDLE: "{bundle_name}"
      NEURON_ROLE: "action"
      NEURON_NAME: "{name}"
      BUNDLE_COLLECTION: "{collection}"
      # Query embedder MUST match the model the docs were embedded with (the input neuron's).
      ACTION_EMBED_MODEL: "{embed_model}"
      # Answer-synthesis LLM (the neuron's `synth_model`). MUST be in the ollama roster
      # (brain_etc/ollama/models) or /ask 404s.
      ACTION_LLM_MODEL: "{synth_model}"
    networks:
      - {net_key}
{command}"""


# --------------------------------------------------------------------------- #
# Region render + splice
# --------------------------------------------------------------------------- #
def render_region(bd=BRAIN_DIR) -> str:
    """The full managed region (markers included) for the default bundle's neuron services,
    rendered from the brain.env zone. A zone with no default bundle yields an empty region
    (markers + a note) rather than a hard failure — the base RAG stack still stands."""
    bd = Path(bd)
    flat = brain_env.load_flat(brain_env.brain_env_path(bd))
    neurons = brain_env.load_neurons(brain_env.brain_env_path(bd))
    bundle = resolve_default_bundle_obj(flat, neurons)
    intro = ("  # The DEFAULT neuron bundle, GENERATED from the brain.env ===NEURONS=== zone by\n"
             "  # neuron_compose.py (config-flow Phase 4). Names/collection/synth-model/token come\n"
             "  # from the neuron object; re-render by editing the zone + reapplying. Additional\n"
             "  # bundles render into the separate `generated bundles` region below.\n")
    if bundle is None:
        return f"{BEGIN}\n{intro}  # (the brain.env zone declares no bundle — nothing to render)\n{END}\n"

    # The default bundle keeps the hand-authored `neuron_net` (index 0); its FIRST input/action
    # neuron carries the shared image `build:` context (built once, reused by every other neuron
    # incl. additional bundles). Everything else per-neuron comes from the block renderers, which
    # additional bundles (add_neuron_bundle) share — the ONE implementation (P0 unification).
    embed_model = _bundle_embed_model(bundle)
    blocks = []
    seen_input = seen_action = False
    for n in (bundle.get("neurons") or []):
        ntype = n.get("type")
        if ntype == "input":
            blocks.append(render_input_block(bundle, n, net_key="neuron_net",
                                             with_build=not seen_input))
            seen_input = True
        elif ntype == "action":
            blocks.append(render_action_block(bundle, n, embed_model=embed_model,
                                              net_key="neuron_net", with_build=not seen_action))
            seen_action = True
    return f"{BEGIN}\n{intro}\n" + "\n".join(blocks) + END + "\n"


def _has_markers(text: str) -> bool:
    return BEGIN in text and END in text


def _splice(compose_text: str, region: str) -> str:
    head, _, rest = compose_text.partition(BEGIN)
    _, _, tail = rest.partition(END + "\n")
    return head + region + tail


def render(bd=BRAIN_DIR, strict: bool = True):
    """Splice the freshly-rendered default-bundle region into brain_etc/docker/compose.yaml
    (idempotent; LF bytes). Returns the compose Path on success, or None when the region markers
    are absent and strict=False (a WARN + skip, so a deploy/keepalive generate never hard-aborts
    on a pre-refactor compose). Fails closed on a bad brain.env marker (via brain_env), and — when
    strict — on missing region markers or a missing compose file (the direct-CLI contract)."""
    bd = Path(bd)
    compose_path = bd / COMPOSE_REL
    if not compose_path.is_file():
        if strict:
            die(f"compose file not found: {compose_path}")
        info(f"WARNING: compose file not found ({compose_path}) - default-bundle region not rendered.")
        return None
    text = compose_path.read_text(encoding="utf-8")
    if not _has_markers(text):
        msg = (f"managed-region markers not found in {compose_path.name}: {BEGIN.strip()!r} .. "
               f"{END.strip()!r} (the base compose.yaml must carry them at the default-bundle anchor).")
        if strict:
            die(msg)
        info(f"WARNING: {msg} Default-bundle region not rendered (skipped).")
        return None
    region = render_region(bd)
    compose_path.write_text(_splice(text, region), encoding="utf-8", newline="\n")
    return compose_path


# --------------------------------------------------------------------------- #
# CLI (render / show / print — test + operator surface)
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render the DEFAULT bundle's compose blocks from the "
                                             "brain.env neuron zone.")
    ap.add_argument("--brain-dir", dest="brain_dir", default=None,
                    help="brain root (default: this tool's brain)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("render", help="splice the default-bundle region into compose.yaml")
    sub.add_parser("print", help="print the rendered region to stdout (no write)")
    args = ap.parse_args(argv)
    bd = Path(args.brain_dir) if args.brain_dir else BRAIN_DIR
    if args.cmd == "print":
        sys.stdout.write(render_region(bd))
        return 0
    out = render(bd)
    info(f"rendered default-bundle region -> {out.relative_to(bd) if out.is_relative_to(bd) else out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
