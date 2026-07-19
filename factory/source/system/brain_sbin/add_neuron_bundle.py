#!/usr/bin/env python3
r"""
add_neuron_bundle.py — scaffold an additional neuron bundle.
============================================================

A neuron BUNDLE groups an input neuron + an action neuron over ONE data source (a chroma
COLLECTION): both act on the same collection — input neurons WRITE it, action neurons READ it.
A bundle may hold MULTIPLE input and/or action neurons. Every neuron is a container SHARING one
of the two role images (input: `<brain>-neurons`; action: `<brain>-action_neurons`) and reaching
the Ollama/Chroma backends ONLY through the gateway (ADR 0015), differing only by env:
NEURON_BUNDLE / NEURON_ROLE / NEURON_NAME / BUNDLE_COLLECTION.

The DEFAULT bundle is rendered by neuron_compose.py from the brain.env ===NEURONS=== zone. Every
*additional* bundle is ALSO authored in that zone (config-flow Phase 5 retired the separate
`brain_etc/neuron/bundles.yaml` registry); this tool renders those additional bundles into a MANAGED
REGION of `brain_etc/docker/compose.yaml` (between `>>> BEGIN generated bundles >>>` markers) and
scaffolds their code/runtime dirs. Never hand-edit the region — edit the zone and re-run this tool.

What one run does, idempotently:

  1. READ the additional bundles from the brain.env neuron zone (brain_env.zone_additional_bundles).
     The named bundle MUST already exist there (this tool no longer registers/writes a file).
  2. RENDER the managed region of compose.yaml from all additional bundles — one service block per
     neuron (input blocks share the input image; action blocks share the action image).
  3. SCAFFOLD the per-neuron impulses/<bundle>/<neuron>/ (your code) + neurons/<bundle>/<neuron>/
     {input,action}/ (brain-managed runtime) dirs.
  4. PRINT the token step — tokens are NOT auto-minted (config-flow Phase 3). Each neuron names its
     gateway bearer BY NAME (`gateway_token:`); the operator creates that named token with
     system/brain_sbin/gateway_tokens.py FIRST (INPUT = chroma:writer, ACTION = chroma:reader).
     gateway_config.resolve_neuron_tokens fails closed if a named token is missing.

    add_neuron_bundle.py <name> [--brain-dir D]

`<name>` is the BUNDLE name (a safe identifier, e.g. `temp`) — declare it in the zone first.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import brain_env              # sibling: the two-zone brain.env loader (the zone bundle registry).
import neuron_compose         # sibling: the SHARED neuron service-block renderer (P0 unification).
import neuron_topology as nt   # sibling: the per-bundle neuron-net addressing scheme (ADR 0015).

HERE = Path(__file__).resolve().parent
BRAIN_DIR = HERE.parent.parent
DEFAULT_BRAIN = BRAIN_DIR.name

COMPOSE_REL = "brain_etc/docker/compose.yaml"
ENV_REL = "brain_etc/brain.env"

# NAMED-TOKEN MODEL (config-flow refactor, Phase 3): a neuron references its gateway bearer BY
# NAME (`gateway_token:` in the brain.env YAML zone). This tool NO LONGER auto-mints or writes
# tokens - the operator creates the named token with system/brain_sbin/gateway_tokens.py BEFORE
# the neuron runs, and gateway_config.resolve_neuron_tokens fails closed if a named token is
# absent. See the NEXT steps this tool prints.

BEGIN = "  # >>> BEGIN generated bundles (add_neuron_bundle.py — do NOT hand-edit) >>>"
END = "  # >>> END generated bundles <<<"
# Two more managed regions carry the per-bundle network topology (ADR 0015). The DEFAULT bundle's
# net (key `neuron_net`) is hand-authored; these regions hold ONLY the additional bundles.
NETS_BEGIN = "  # >>> BEGIN generated neuron-nets (add_neuron_bundle.py — do NOT hand-edit) >>>"
NETS_END = "  # >>> END generated neuron-nets <<<"
GWNET_BEGIN = "      # >>> BEGIN generated gateway neuron-nets (add_neuron_bundle.py — do NOT hand-edit) >>>"
GWNET_END = "      # >>> END generated gateway neuron-nets <<<"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def info(m): print(f"[add-bundle] {m}")
def die(m): print(f"[add-bundle] ERROR: {m}", file=sys.stderr); sys.exit(1)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def load_registry(bd: Path) -> list:
    """The ADDITIONAL neuron bundles, sourced from the brain.env ===NEURONS=== zone (config-flow
    Phase 5 retired the hand-authored brain_etc/neuron/bundles.yaml). Bundles are now authored in
    the ONE control panel; this tool RENDERS + SCAFFOLDS what the zone declares, it no longer
    registers/writes a separate file."""
    return brain_env.zone_additional_bundles(Path(bd))


def _neuron_names(bundle: dict, role_key: str) -> list:
    """The list of neuron names for a role ('input_neurons'|'action_neurons'), tolerating either
    [{'name': x}] or bare [x] entries."""
    out = []
    for n in (bundle.get(role_key) or []):
        name = n.get("name") if isinstance(n, dict) else n
        if name:
            out.append(str(name))
    return out


# --------------------------------------------------------------------------- #
# Compose managed region
# --------------------------------------------------------------------------- #
# NOTE (config-flow P0 unification): the per-neuron service blocks are NO LONGER rendered here.
# add_neuron_bundle once carried its OWN render_input_block/render_action_block that emitted the
# STALE flat shape (one shared NEURON_GATEWAY_TOKEN/ACTION_GATEWAY_TOKEN, hardcoded embed model,
# ${ACTION_LLM_MODEL:-...}, no service_type shape) — a divergent twin of neuron_compose's rich
# default-bundle renderer. They are now DELETED: additional bundles render through the SAME
# neuron_compose.render_input_block/render_action_block the default bundle uses, so per-neuron
# token/synth_model/embed_model/service_type can never drift between the two regions. The ONLY
# things that differ for an additional bundle are topology (its own `<bundle>_neuron_net`) and
# with_build=False (it reuses the image the default bundle's neurons built). add_neuron_bundle
# still OWNS the additional-bundle NETS + the gateway per-net attachment (below) + scaffolding.


def _additional(bundles: list):
    """Yield (bundle_dict, index, net_key) for each ADDITIONAL bundle, index from neuron_topology
    (0 is the default bundle; additional start at 1). The net key is `<bundle>_neuron_net`. Accepts
    the names-only adapted view (zone_additional_bundles) — index/net allocation needs only
    name + net_index, and keeping THIS on the same view topology uses guarantees the two agree."""
    idx = nt.allocate_indices(bundles)
    for b in bundles:
        name = str(b["name"])
        i = idx[name]
        yield b, i, nt.net_key(name, i)


def render_region(bundles: list, rich_by_name: dict) -> str:
    """The additional-bundle SERVICE-BLOCK region, rendered through the SHARED neuron_compose block
    renderers from the RICH zone objects (per-neuron token/synth_model/derived-embed_model/
    service_type — no more flat placeholders). `bundles` is the names-only adapted view (for net_key
    via _additional); `rich_by_name` maps bundle name -> its full zone object (the per-neuron data).
    with_build=False: additional bundles reuse the image the default bundle's neurons built."""
    intro = (
        "  # Additional neuron bundles rendered from the brain.env ===NEURONS=== zone through the SAME\n"
        "  # shared renderer as the DEFAULT bundle (config-flow P0 unification), so per-neuron token /\n"
        "  # synth_model / embed_model / service_type never drift between the two regions. Each neuron\n"
        "  # SHARES a role image (input: <brain>-input_neurons; action: <brain>-action_neurons — only\n"
        "  # the default bundle's neurons carry the `build:` context; these reuse the image) and sits on\n"
        "  # its bundle's OWN neuron net. Re-render via `gateway_config.py generate` or add_neuron_bundle.py.\n"
    )
    blocks = []
    for b, _i, net_key in _additional(bundles):
        rich = rich_by_name.get(str(b["name"])) or {}
        embed_model = neuron_compose._bundle_embed_model(rich)
        for n in (rich.get("neurons") or []):
            t = n.get("type")
            if t == "input":
                blocks.append(neuron_compose.render_input_block(
                    rich, n, net_key=net_key, with_build=False, bundle_kind="bundle"))
            elif t == "action":
                blocks.append(neuron_compose.render_action_block(
                    rich, n, embed_model=embed_model, net_key=net_key, with_build=False,
                    bundle_kind="bundle"))
    return f"{BEGIN}\n{intro}\n" + "\n".join(blocks) + END + "\n"


def render_nets_region(bundles: list, brain_default: str) -> str:
    """The top-level `networks:` entries for the ADDITIONAL bundles — one isolated /24 each
    (192.168.<idx>.0/24), gateway static .2, neuron dynamic pool .128/25 (collision-proof)."""
    bn = "${BRAIN_NAME:-" + brain_default + "}"
    intro = (
        "  # One isolated docker network per ADDITIONAL bundle (ADR 0015). The DEFAULT bundle's net is\n"
        "  # `neuron_net` (hand-authored above). Each is a /24 from 192.168.0.0/16 by ascending index;\n"
        "  # dynamic neuron IPs come ONLY from .128/25 so a neuron can never squat the gateway's static\n"
        "  # .2 (collision-proof, per-net). Re-render via add_neuron_bundle.py.\n"
    )
    blocks = []
    for b, i, net_key in _additional(bundles):
        blocks.append(
            f"  {net_key}:\n"
            f"    name: {bn}_{net_key}\n"
            f"    ipam:\n"
            f"      config:\n"
            f'        - subnet: "{nt.subnet(i)}"\n'
            f'          ip_range: "{nt.dyn_ip_range(i)}"\n'
        )
    return f"{NETS_BEGIN}\n{intro}" + "".join(blocks) + f"{NETS_END}\n"


def render_gw_attach_region(bundles: list) -> str:
    """The gateway service's `networks:` entries for the ADDITIONAL bundles — the gateway joins
    EVERY bundle net with its static .2 + the chroma/ollama aliases (so nginx can `listen` there
    and neurons resolve chroma/ollama to the gateway). The default net is attached hand-authored."""
    intro = (
        "      # The gateway is multi-homed onto EVERY additional bundle net (static .2 + chroma/ollama\n"
        "      # aliases), so it is the one chokepoint every bundle reaches. Re-render via add_neuron_bundle.py.\n"
    )
    blocks = []
    for _b, i, net_key in _additional(bundles):
        blocks.append(
            f"      {net_key}:\n"
            f'        ipv4_address: "{nt.gw_ip(i)}"\n'
            f"        aliases:\n"
            f"          - chroma\n"
            f"          - ollama\n"
        )
    return f"{GWNET_BEGIN}\n{intro}" + "".join(blocks) + f"{GWNET_END}\n"


def _splice(compose_text: str, begin: str, end: str, region: str) -> str:
    """Replace the managed region delimited by `begin`..`end` (inclusive) with `region` (which
    itself begins/ends with those markers). The markers MUST already exist in the file (they are
    hand-placed in the base compose.yaml at the right anchor + indentation)."""
    if begin not in compose_text or end not in compose_text:
        die(f"managed-region markers not found in compose.yaml: {begin.strip()!r}")
    head, _, rest = compose_text.partition(begin)
    _, _, tail = rest.partition(end + "\n")
    return head + region + tail


def splice_all(compose_text: str, bundles: list, rich_by_name: dict, brain_default: str) -> str:
    """Re-render all three managed regions from the zone: the neuron service blocks (through the
    SHARED renderer, from `rich_by_name`), the per-bundle network definitions, and the gateway's
    per-net attachments. `bundles` is the names-only adapted view (nets/gw/index)."""
    txt = _splice(compose_text, BEGIN, END, render_region(bundles, rich_by_name))
    txt = _splice(txt, NETS_BEGIN, NETS_END, render_nets_region(bundles, brain_default))
    txt = _splice(txt, GWNET_BEGIN, GWNET_END, render_gw_attach_region(bundles))
    return txt


def _has_all_markers(text: str) -> bool:
    """True iff all three additional-region marker pairs are present (a base compose.yaml carries
    them at the right anchors + indentation). render_all skips lenient when they're absent."""
    return all(m in text for m in (BEGIN, END, NETS_BEGIN, NETS_END, GWNET_BEGIN, GWNET_END))


def render_all(bd, strict: bool = False):
    """Render ALL additional bundles from the brain.env zone into compose.yaml's three managed
    regions (service blocks + nets + gateway attachments) — the PURE render, idempotent, no
    scaffolding/token prints. This is the SINGLE additional-bundle materialize step: called by
    gateway_config.cmd_generate on every apply (so additional regions never go stale) AND by this
    tool's CLI. Lenient by default (strict=False): a missing compose file or absent markers → WARN
    + skip (returns None) so a keepalive generate never hard-aborts on a pre-refactor compose;
    strict=True (the direct-CLI contract) dies instead. Returns the compose Path on a write."""
    bd = Path(bd)
    compose_path = bd / COMPOSE_REL
    if not compose_path.is_file():
        if strict:
            die(f"compose file not found: {compose_path}")
        info(f"WARNING: compose file not found ({compose_path}) — additional bundles not rendered.")
        return None
    text = compose_path.read_text(encoding="utf-8")
    if not _has_all_markers(text):
        msg = (f"additional-bundle managed-region markers not all present in {compose_path.name} "
               f"(need the generated bundles / neuron-nets / gateway neuron-nets marker pairs).")
        if strict:
            die(msg)
        info(f"WARNING: {msg} Additional bundles not rendered (skipped).")
        return None
    bundles = load_registry(bd)                                   # adapted view: nets/gw/index
    rich = {str(b.get("name")): b for b in brain_env.iter_additional_bundle_objects(bd)}
    compose_path.write_text(splice_all(text, bundles, rich, bd.name),
                            encoding="utf-8", newline="\n")
    return compose_path


# --------------------------------------------------------------------------- #
def scaffold_neuron_dirs(bd: Path, bundle_name: str, entry: dict) -> list:
    """Config-flow Phase 5: neuron creation auto-creates, per neuron of the bundle,
        impulses/<bundle>/<neuron>/                YOUR code (providers + action apps)
        neurons/<bundle>/<neuron>/{input,action}/  the brain-managed runtime
    Idempotent (mkdir -p). Runtime dirs get a .gitkeep so they survive an empty checkout.
    Returns the list of created top-level per-neuron dirs (relative posix) for reporting."""
    made = []
    names = _neuron_names(entry, "input_neurons") + _neuron_names(entry, "action_neurons")
    for nn in names:
        imp = bd / "impulses" / bundle_name / nn
        imp.mkdir(parents=True, exist_ok=True)
        made.append(imp.relative_to(bd).as_posix())
        for role in ("input", "action"):
            run = bd / "neurons" / bundle_name / nn / role
            run.mkdir(parents=True, exist_ok=True)
            keep = run / ".gitkeep"
            if not keep.exists():
                keep.write_text("", encoding="utf-8", newline="\n")
        made.append((bd / "neurons" / bundle_name / nn).relative_to(bd).as_posix())
    return made


# --------------------------------------------------------------------------- #
# brain.env reader (the default-bundle name lives in the flat zone).
# --------------------------------------------------------------------------- #
def _env_value(env_path: Path, key: str) -> str:
    """Current value of KEY in an env file, or '' if unset/blank/absent. Tolerates an
    optional `export `, surrounding whitespace, wrapping quotes, and a ` # comment` tail."""
    if not env_path.is_file():
        return ""
    pat = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}=(.*)$")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        m = pat.match(line)
        if not m:
            continue
        val = re.sub(r"\s+#.*$", "", m.group(1)).strip().strip('"').strip("'")
        return val
    return ""


def resolve_default_bundle(bd: Path) -> str:
    """The DEFAULT bundle — the hand-authored bundle (in brain_etc/docker/compose.yaml) that
    every unlabeled source belongs to; never generated here. Its name is a per-deployment
    decision read from the DEFAULT_BUNDLE seam in brain.env; unset/empty falls back to the
    brain name (the sane default the factory template ships)."""
    return _env_value(bd / ENV_REL, "DEFAULT_BUNDLE") or bd.name


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render + scaffold an additional neuron bundle "
                                             "declared in the brain.env ===NEURONS=== zone.")
    ap.add_argument("name", help="bundle name — must already exist in the brain.env neuron zone")
    ap.add_argument("--brain-dir", default=None, help="brain root (default: this tool's brain)")
    args = ap.parse_args(argv)

    if not _NAME_RE.match(args.name):
        die(f"bundle name '{args.name}' must be a lowercase identifier ([a-z][a-z0-9_]*)")

    bd = Path(args.brain_dir) if args.brain_dir else BRAIN_DIR

    default_bundle = resolve_default_bundle(bd)
    if args.name == default_bundle:
        die(f"'{default_bundle}' is the DEFAULT bundle (rendered by neuron_compose.py from the zone); "
            f"this tool only handles ADDITIONAL bundles")

    compose_path = bd / COMPOSE_REL
    if not compose_path.is_file():
        die(f"compose file not found: {compose_path}")

    # 1) The bundle is AUTHORED in the brain.env ===NEURONS=== zone now (config-flow Phase 5 retired
    #    the separate brain_etc/neuron/bundles.yaml registry). This tool no longer registers a bundle
    #    — it renders + scaffolds what the zone already declares. Read the zone's additional bundles.
    bundles = load_registry(bd)
    entry = next((b for b in bundles if b.get("name") == args.name), None)
    if entry is None:
        names = ", ".join(b.get("name") for b in bundles) or "(none)"
        die(f"bundle '{args.name}' is not declared as an ADDITIONAL bundle in the brain.env neuron "
            f"zone. Add a `neuron_bundles:` entry (name: {args.name}) below the ===NEURONS=== marker "
            f"in brain_etc/brain.env, then re-run. Known additional bundles: {names}")
    collection = entry.get("collection") or args.name
    idx = nt.allocate_indices(bundles)
    info(f"bundle '{args.name}' -> net {nt.net_key(args.name, idx[args.name])} "
         f"({nt.subnet(idx[args.name])}, gateway {nt.gw_ip(idx[args.name])})")

    # 2) compose managed regions: neuron service blocks + per-bundle net defs + gateway attachments,
    #    rendered from ALL the zone's additional bundles through the SAME single materialize step
    #    gateway_config.cmd_generate calls (render_all — idempotent; the service blocks go through the
    #    SHARED neuron_compose renderer, so they carry the SAME per-neuron token/model/service_type
    #    shape as the default bundle). Re-run any time the zone changes.
    render_all(bd, strict=True)
    n_neurons = sum(len(_neuron_names(b, "input_neurons") or [1]) + len(_neuron_names(b, "action_neurons"))
                    for b in bundles)
    info(f"rendered {len(bundles)} additional bundle(s) / {n_neurons} neuron(s) into {COMPOSE_REL}")

    # 3) the per-neuron impulses/ (your code) + neurons/ (runtime) dirs for this bundle
    for made in scaffold_neuron_dirs(bd, args.name, entry):
        info(f"scaffolded neuron dir: {made}/")

    # 4) tokens are NOT auto-minted (config-flow Phase 3). A neuron names its bearer BY NAME; the
    #    operator creates that named token with gateway_tokens.py FIRST (INPUT = write, ACTION = read).
    has_action = bool(_neuron_names(entry, "action_neurons"))
    brain = bd.name
    print()
    info("NEXT (manual — a human decision):")
    info(f"  1. Create the gateway token(s) this bundle's neurons name (referenced in the zone):")
    info(f"       gateway_tokens.py --brain {brain} grant --label {args.name}_writer "
         f"--grant chroma:writer --grant ollama:use")
    if has_action:
        info(f"       gateway_tokens.py --brain {brain} grant --label {args.name}_reader "
             f"--grant chroma:reader --grant ollama:use")
    info(f"     each neuron's `gateway_token:` in the zone must name one of these (input -> writer,")
    info(f"     action -> reader).")
    info(f"  2. Author the bundle's sources in the zone (each input neuron's `sources:` list); the")
    info(f"     runtime sources.yaml is RENDERED from the zone by brain_env.render_sources_yaml.")
    info(f"  3. Drop provider code under impulses/{args.name}/<neuron>/ (scaffolded above).")
    info(f"  4. Apply: python system/brain_sbin/reapply_brain_configs.py   (syncs + recreates the stack).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
