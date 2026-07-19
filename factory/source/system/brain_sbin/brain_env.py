#!/usr/bin/env python3
r"""
brain_env.py - the two-zone brain.env loader + the .env render/split (brain_sbin).
==================================================================================

brain.env is the ONE control panel for a brain (spec: project_plans/001_prototype-brain.env).
It has TWO zones split by a single marker line whose content is `===NEURONS===`:

    <flat KEY=VALUE service panel>      ABOVE the marker  -> rendered to ~/docker/.env
    ===NEURONS===
    <nested YAML neuron definitions>    BELOW the marker  -> parsed by the neuron tooling

Both zones are authored by hand in ONE file; this module is the tooling that splits at
the marker. Nothing else understands the two-zone shape - every other tool goes through
here (or through gateway_config.read_env, which is marker-aware for the flat zone).

WHAT THIS MODULE DOES
---------------------
  * split_zones(text)      -> (flat_text, yaml_text), failing CLOSED on a malformed marker
                              or format bleed across it (a clear message, never a silent half-load).
  * load_flat(path)        -> dict of the flat KEY=VALUE zone (the compose environment).
  * load_neurons(path)     -> the validated neuron-config object (schedules + neuron_bundles).
  * render_dotenv(bd)      -> writes brain_etc/docker/.env.rendered (the GENERATED flat env the
                              seam manifest copies to ~/docker/.env; brain.env stays the source).
  * an accessor API over the neuron object (iter_bundles / iter_neurons / schedule_cron / ...)
    that the neuron tooling (tokens, backends, compose, schedule) reads instead of the retired
    brain_etc/neuron/{bundles,sources}.yaml.

Everything is written as LF bytes (a stray CR has bitten the seam - see gateway_config).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
BRAIN_DIR = HERE.parent.parent

# The zone divider. A line is THE marker when its content - after stripping a leading `#`
# and surrounding whitespace - begins with this token. So the prototype's commented banner
#   # ===NEURONS===   (everything below is YAML ...)
# is recognized, while the plain `# ====...====` rule lines around it are NOT (they carry no
# NEURONS token). Exactly one marker must exist.
MARKER = "===NEURONS==="

# The generated flat env: brain.env's flat zone, materialized beside the compose files it
# pairs with. The seam manifest ships THIS to ~/docker/.env - never brain.env itself (which
# now carries the YAML zone compose must not see). Regenerated on every apply; do not edit.
RENDERED_ENV_REL = "brain_etc/docker/.env.rendered"

# The GENERATED runtime source manifest: the input container reads /etc/neuron/sources.yaml
# (NEURON_MANIFEST). Config-flow Phase 5 RETIRES the hand-authored brain_etc/neuron/sources.yaml
# as an INPUT — it is now RENDERED from the brain.env ===NEURONS=== zone into this generated path
# and the seam manifest ships THIS to ~/docker/neuron/sources.yaml. Regenerated on every apply.
RENDERED_SOURCES_REL = "brain_etc/docker/neuron/sources.yaml"


def die(m):  # fail closed, loud, single-line (cp1252-safe console)
    print(f"[brain_env] ERROR: {m}", file=sys.stderr)
    sys.exit(1)


def info(m):
    print(f"[brain_env] {m}")


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def brain_env_path(bd=BRAIN_DIR):
    return Path(bd) / "brain_etc" / "brain.env"


def rendered_env_path(bd=BRAIN_DIR):
    return Path(bd) / RENDERED_ENV_REL


def rendered_sources_path(bd=BRAIN_DIR):
    return Path(bd) / RENDERED_SOURCES_REL


# --------------------------------------------------------------------------- #
# The split - the one place that understands the marker
# --------------------------------------------------------------------------- #
def _is_marker(line: str) -> bool:
    return line.strip().lstrip("#").strip().startswith(MARKER)


class BrainEnvError(Exception):
    """A malformed brain.env - raised so callers can fail closed with a clear message."""


def split_zones(text: str):
    """Split brain.env text into (flat_zone_text, yaml_zone_text) at the ===NEURONS=== marker.

    Fails CLOSED (raises BrainEnvError) when the file cannot be split unambiguously:
      * no marker at all              - a brain.env MUST declare its two zones.
      * more than one marker          - ambiguous; which one splits?
      * flat-zone format bleed        - a non-comment, non-blank line above the marker that is
                                        not KEY=VALUE (a stray YAML line leaked up).
      * yaml-zone format bleed        - the zone below the marker does not parse as YAML.
    The marker line itself belongs to NEITHER zone (it is the divider)."""
    lines = text.splitlines()
    marker_idx = [i for i, ln in enumerate(lines) if _is_marker(ln)]
    if not marker_idx:
        raise BrainEnvError(
            f"no '{MARKER}' marker found - a brain.env must split its flat service panel "
            f"from its YAML neuron zone with a single '{MARKER}' line.")
    if len(marker_idx) > 1:
        raise BrainEnvError(
            f"multiple '{MARKER}' markers (lines {', '.join(str(i + 1) for i in marker_idx)}) "
            f"- exactly one may split the file.")
    idx = marker_idx[0]
    flat_text = "\n".join(lines[:idx])
    yaml_text = "\n".join(lines[idx + 1:])

    # Flat-zone bleed: every non-comment, non-blank line above the marker must be KEY=VALUE.
    for i, ln in enumerate(lines[:idx]):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            raise BrainEnvError(
                f"flat zone line {i + 1} is not KEY=VALUE and not a comment: {ln!r} "
                f"- YAML belongs BELOW the '{MARKER}' marker.")

    # YAML-zone bleed: the zone below the marker must parse as a YAML mapping (or be empty).
    try:
        obj = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise BrainEnvError(f"YAML zone (below the marker) does not parse: {e}")
    if obj is not None and not isinstance(obj, dict):
        raise BrainEnvError(
            f"YAML zone must be a mapping (schedules:/neuron_bundles:), got {type(obj).__name__}.")

    return flat_text, yaml_text


def _read_text(path) -> str:
    p = Path(path)
    if not p.is_file():
        raise BrainEnvError(f"brain.env not found: {p}")
    return p.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Flat zone -> the compose environment
# --------------------------------------------------------------------------- #
def parse_flat(flat_text: str) -> dict:
    """Parse the flat KEY=VALUE zone -> dict (blanks / # comments ignored). Same semantics as
    gateway_config.read_env, kept here so callers can parse an already-split flat zone."""
    out = {}
    for line in flat_text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        # strip a trailing inline comment (`KEY=val   # note`) the way an operator writes them,
        # but only when the '#' is clearly a comment (preceded by whitespace) - a '#' inside a
        # value with no leading space is kept.
        if "#" in v:
            head, _, tail = v.partition("#")
            if head.rstrip() != head:  # there was whitespace before the '#'
                v = head
        out[k.strip()] = v.strip()
    return out


def load_flat(path=None) -> dict:
    """Load ONLY the flat KEY=VALUE zone of brain.env -> dict."""
    path = path or brain_env_path()
    flat_text, _ = split_zones(_read_text(path))
    return parse_flat(flat_text)


# --------------------------------------------------------------------------- #
# YAML zone -> the neuron-config object (+ validation)
# --------------------------------------------------------------------------- #
_NEURON_TYPES = {"input", "action"}
_CONSUMPTION = {"on-disk", "script", "git"}
_GIT_PROTOCOLS = {"ssh", "none"}
_SERVICE_TYPES = {"on-demand", "daemon"}
_BACKEND_KINDS = {"chroma", "inference"}
_INFERENCE_PROVIDERS = {"ollama", "openai"}


def load_neurons(path=None) -> dict:
    """Load + VALIDATE the YAML neuron zone -> a normalized config object:

        { "schedules": {name: cron, ...},
          "neuron_bundles": [ {name, default_collection, description, backends, neurons:[...]} ] }

    Raises BrainEnvError (fail closed) on any structural violation."""
    path = path or brain_env_path()
    _, yaml_text = split_zones(_read_text(path))
    obj = yaml.safe_load(yaml_text) or {}
    errors = validate_neurons(obj)
    if errors:
        raise BrainEnvError(
            "neuron zone (below ===NEURONS===) is invalid:\n  - " + "\n  - ".join(errors))
    obj.setdefault("schedules", {})
    obj.setdefault("neuron_bundles", [])
    return obj


def validate_neurons(obj: dict) -> list:
    """Structural validation of the neuron zone. Returns a list of human-readable errors
    (empty = valid). Phase 1 checks SHAPE + cross-references that live entirely in this file
    (schedule names resolve, types are known, roles carry their required blocks). It does NOT
    check that a gateway_token name exists in the token map - that is Phase 3's live check."""
    errors = []
    if not isinstance(obj, dict):
        return [f"top level must be a mapping, got {type(obj).__name__}"]

    schedules = obj.get("schedules") or {}
    if schedules and not isinstance(schedules, dict):
        errors.append("`schedules` must be a mapping of name -> cron string")
        schedules = {}

    bundles = obj.get("neuron_bundles")
    if bundles is None:
        errors.append("`neuron_bundles` is required (a list of bundle mappings)")
        return errors
    if not isinstance(bundles, list):
        errors.append("`neuron_bundles` must be a list")
        return errors

    seen_bundles = set()
    for bi, b in enumerate(bundles):
        where = f"neuron_bundles[{bi}]"
        if not isinstance(b, dict):
            errors.append(f"{where} must be a mapping")
            continue
        name = b.get("name")
        if not name or not isinstance(name, str):
            errors.append(f"{where} needs a string `name`")
            name = where
        if name in seen_bundles:
            errors.append(f"duplicate bundle name {name!r}")
        seen_bundles.add(name)

        errors += _validate_backends(b.get("backends"), f"bundle {name!r}")

        neurons = b.get("neurons")
        if not isinstance(neurons, list) or not neurons:
            errors.append(f"bundle {name!r} needs a non-empty `neurons` list")
            neurons = []

        seen_neurons = set()
        for ni, n in enumerate(neurons):
            nwhere = f"bundle {name!r} neurons[{ni}]"
            if not isinstance(n, dict):
                errors.append(f"{nwhere} must be a mapping")
                continue
            nname = n.get("name")
            if not nname or not isinstance(nname, str):
                errors.append(f"{nwhere} needs a string `name`")
                nname = nwhere
            if nname in seen_neurons:
                errors.append(f"bundle {name!r} has duplicate neuron name {nname!r}")
            seen_neurons.add(nname)

            errors += _validate_backends(n.get("backends"), f"neuron {nname!r}")

            ntype = n.get("type")
            if ntype not in _NEURON_TYPES:
                errors.append(f"neuron {nname!r}: `type` must be one of {sorted(_NEURON_TYPES)}, "
                              f"got {ntype!r}")
                continue

            if ntype == "input":
                errors += _validate_input_neuron(n, nname, schedules)
            else:
                errors += _validate_action_neuron(n, nname)

    return errors


def _validate_backends(backends, where: str) -> list:
    """Structural check of a `backends:` block (bundle default OR per-neuron override).
    Absent is fine (defaults to internal). Catches gross shape errors so an off-box endpoint can't
    silently misconfigure the gateway's external egress (ADR-0020 Q2). Endpoint STRINGS are not
    URL-parsed here - gateway_config does that when it renders the egress upstream (fail closed)."""
    errors = []
    if backends is None:
        return errors
    if not isinstance(backends, dict):
        return [f"{where}: `backends` must be a mapping (chroma:/inference:)"]
    for kind, sec in backends.items():
        if kind not in _BACKEND_KINDS:
            errors.append(f"{where}: unknown backend {kind!r} (expected {sorted(_BACKEND_KINDS)})")
            continue
        if not isinstance(sec, dict):
            errors.append(f"{where}: backend {kind!r} must be a mapping")
            continue
        ep = sec.get("endpoint")
        if ep is not None and not isinstance(ep, str):
            errors.append(f"{where}: backend {kind!r} `endpoint` must be a string "
                          f"('internal' or an https:// URL)")
        if kind == "inference":
            prov = sec.get("provider")
            if prov is not None and prov not in _INFERENCE_PROVIDERS:
                errors.append(f"{where}: inference `provider` must be one of "
                              f"{sorted(_INFERENCE_PROVIDERS)}, got {prov!r}")
    return errors


def _validate_input_neuron(n: dict, nname: str, schedules: dict) -> list:
    errors = []
    sched = n.get("schedule")
    if sched is not None and sched not in schedules:
        errors.append(f"input neuron {nname!r}: schedule {sched!r} is not defined in the "
                      f"top-level `schedules:` map ({sorted(schedules) or 'empty'})")
    sources = n.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append(f"input neuron {nname!r} needs a non-empty `sources` list")
        return errors
    for si, s in enumerate(sources):
        swhere = f"input neuron {nname!r} sources[{si}]"
        if not isinstance(s, dict):
            errors.append(f"{swhere} must be a mapping")
            continue
        if not s.get("name"):
            errors.append(f"{swhere} needs a `name`")
        consumption = s.get("consumption")
        if consumption not in _CONSUMPTION:
            errors.append(f"{swhere}: `consumption` must be one of {sorted(_CONSUMPTION)}, "
                          f"got {consumption!r}")
            continue
        if consumption == "script" and not s.get("script"):
            errors.append(f"{swhere}: consumption:script needs a `script` path")
        if consumption == "git":
            git = s.get("git") or {}
            proto = (git.get("protocol") if isinstance(git, dict) else None)
            if proto is not None and proto not in _GIT_PROTOCOLS:
                errors.append(f"{swhere}: git protocol must be one of {sorted(_GIT_PROTOCOLS)} "
                              f"(https is unsupported), got {proto!r}")
    return errors


def _validate_action_neuron(n: dict, nname: str) -> list:
    errors = []
    app = n.get("app")
    if not isinstance(app, dict):
        errors.append(f"action neuron {nname!r} needs an `app` mapping")
        return errors
    if not app.get("executable"):
        errors.append(f"action neuron {nname!r}: app.executable is required")
    st = app.get("service_type")
    if st is not None and st not in _SERVICE_TYPES:
        errors.append(f"action neuron {nname!r}: app.service_type must be one of "
                      f"{sorted(_SERVICE_TYPES)}, got {st!r}")
    ports = n.get("publish_to_lan_ports")
    if ports is not None and not (isinstance(ports, list) and all(isinstance(p, int) for p in ports)):
        errors.append(f"action neuron {nname!r}: publish_to_lan_ports must be a list of ints")
    return errors


# --------------------------------------------------------------------------- #
# Accessor API over the neuron object - the neuron tooling reads THESE instead
# of the retired brain_etc/neuron/{bundles,sources}.yaml.
# --------------------------------------------------------------------------- #
def default_bundle_name(flat: dict) -> str:
    """The default bundle name: DEFAULT_BUNDLE if set, else the brain name (BRAIN_NAME)."""
    return (flat.get("DEFAULT_BUNDLE") or flat.get("BRAIN_NAME") or "").strip()


def iter_bundles(neurons: dict):
    yield from (neurons.get("neuron_bundles") or [])


def iter_neurons(neurons: dict, ntype: str = None):
    """Yield (bundle, neuron) pairs, optionally filtered to a type ('input'|'action')."""
    for b in iter_bundles(neurons):
        for n in (b.get("neurons") or []):
            if ntype is None or n.get("type") == ntype:
                yield b, n


def schedule_cron(neurons: dict, name: str):
    """Resolve a schedule NAME to its cron string (None if undefined)."""
    return (neurons.get("schedules") or {}).get(name)


def active_schedules(neurons: dict) -> dict:
    """The {name: cron} map of schedules that at least one input neuron actually uses -
    exactly the timers the schedule generator should install."""
    used = {n.get("schedule") for _, n in iter_neurons(neurons, "input") if n.get("schedule")}
    sched = neurons.get("schedules") or {}
    return {name: cron for name, cron in sched.items() if name in used}


def bundle_collection(bundle: dict) -> str:
    """A bundle's chroma collection (default_collection, or <name>_docs as a fallback)."""
    return bundle.get("default_collection") or f"{bundle.get('name')}_docs"


# --------------------------------------------------------------------------- #
# Per-neuron / per-bundle backends  (config-flow Phase 4 / ADR-0020 Q2)
# --------------------------------------------------------------------------- #
# A bundle declares DEFAULT backends; a neuron may OVERRIDE per-key. Two backend kinds:
#   chroma      { endpoint: internal | https://host:port }
#   inference   { provider: ollama|openai, endpoint: internal | URL, api_key_gpg_id: <future> }
# `internal` (the DEFAULT) is the brain's own service reached through the gateway; an off-box URL
# ALSO routes through the gateway (the Q2 invariant). The gateway consumer of these is
# gateway_config.render_internal_conf (the external-egress upstreams).
def bundle_backends(bundle: dict) -> dict:
    """A bundle's `backends:` block (chroma/inference sections), or {} when absent."""
    b = bundle.get("backends")
    return b if isinstance(b, dict) else {}


def backend_section(backends: dict, kind: str) -> dict:
    """One section ('chroma'|'inference') of a backends block, or {} when absent/misshapen."""
    sec = (backends or {}).get(kind)
    return sec if isinstance(sec, dict) else {}


def resolve_backend(bundle: dict, neuron: dict, kind: str) -> dict:
    """Effective backend config for a NEURON = the bundle-default section overlaid by the neuron's
    OWN `backends.<kind>` override (per key). `endpoint` defaults to 'internal' (the secure built-in
    path); for kind='inference', `provider` defaults to 'ollama'. neuron may be {} (bundle default)."""
    merged = dict(backend_section(bundle_backends(bundle), kind))
    for k, v in backend_section((neuron or {}).get("backends") or {}, kind).items():
        if v is not None:
            merged[k] = v
    merged.setdefault("endpoint", "internal")
    if kind == "inference":
        merged.setdefault("provider", "ollama")
    return merged


# --------------------------------------------------------------------------- #
# Render the flat zone -> brain_etc/docker/.env.rendered (what the seam ships)
# --------------------------------------------------------------------------- #
_RENDER_HEADER = (
    "# ===========================================================================\n"
    "# GENERATED - do NOT edit. Source of truth: brain_etc/brain.env (flat zone,\n"
    "# ABOVE the ===NEURONS=== marker). Regenerated on every apply/reapply by\n"
    "# brain_env.render_dotenv(); the seam manifest copies THIS file to ~/docker/.env\n"
    "# (the file docker compose auto-loads). Edit brain.env, then apply.\n"
    "# ===========================================================================\n"
)


def neuron_token_env_key(bundle_name: str, neuron_name: str) -> str:
    """The .env variable a neuron's container reads its gateway bearer from (config-flow Phase 4).
    Per-neuron (NOT one shared flat key) so every neuron carries its OWN named token: compose
    references ${NEURON_TOKEN__<bundle>__<neuron>}. Bundle/neuron names are validated to
    [a-z][a-z0-9_]*, so this is always a legal shell identifier; the generated (or hand-authored)
    compose block and this renderer MUST agree on this exact spelling."""
    return f"NEURON_TOKEN__{bundle_name}__{neuron_name}"


def _neuron_token_lines(bd) -> list:
    """Resolve each neuron's named `gateway_token:` to its secret and emit the per-neuron .env
    lines the compose neuron blocks consume (NEURON_TOKEN__<bundle>__<neuron>=<secret>).

    LENIENT by design: a neuron that names no token, or names one absent from the registry, is
    simply omitted here - the compose block's ${...:?} guard (or gateway_config.resolve_neuron_tokens,
    which is the FAIL-CLOSED gate at generate time) is what surfaces the error. This renderer must
    never die: it runs on every keepalive apply, where a hard failure would break a live reflow.
    Returns [] (no appendix) if the token registry tooling isn't importable."""
    try:
        import gateway_tokens  # sibling; lazy so brain_env stays importable without it
    except Exception:
        return []
    try:
        neurons = load_neurons(brain_env_path(bd))
    except BrainEnvError:
        return []
    by_label = {e.label: e for e in gateway_tokens.read_registry(bd) if e.label}
    lines = []
    for bundle, n in iter_neurons(neurons):
        label = n.get("gateway_token")
        entry = by_label.get(label) if label else None
        if entry is None:
            continue
        key = neuron_token_env_key(bundle.get("name"), n.get("name"))
        lines.append(f"{key}={entry.token}")
    return lines


def default_input_neuron_name(flat: dict, neurons: dict) -> str:
    """The NAME of the DEFAULT bundle's FIRST input neuron - the neuron the in-distro ingest
    timers drive (config-flow Phase 5, retiring neuron_schedule's stale `<bundle>_input_1`
    guess). The compose SERVICE name == the neuron name (neuron_compose.render_input_block).
    Returns '' when the zone declares no input neuron in the default bundle."""
    want = default_bundle_name(flat)
    bundles = list(iter_bundles(neurons))
    if not bundles:
        return ""
    bundle = next((b for b in bundles if b.get("name") == want), bundles[0])
    for n in (bundle.get("neurons") or []):
        if n.get("type") == "input" and n.get("name"):
            return str(n["name"])
    return ""


def _neuron_schedule_lines(bd) -> list:
    """Emit the in-distro ingest-schedule seam as flat .env keys, sourced from the brain.env
    ===NEURONS=== zone (config-flow Phase 5 - retires ~/docker/neuron/sources.yaml as the
    schedule source). neuron_schedule.py (in-distro) reads THESE from ~/docker/.env:
        DEFAULT_INPUT_NEURON=<name>          the default bundle's first input neuron (timer target)
        NEURON_SCHEDULE__<tag>=<cron>        one per ACTIVE schedule (a cadence >=1 input neuron uses)
    Lenient: returns [] if the zone can't be loaded (a keepalive render must never die)."""
    try:
        flat = load_flat(brain_env_path(bd))
        neurons = load_neurons(brain_env_path(bd))
    except BrainEnvError:
        return []
    lines = []
    default_input = default_input_neuron_name(flat, neurons)
    if default_input:
        lines.append(f"DEFAULT_INPUT_NEURON={default_input}")
    for tag, cron in active_schedules(neurons).items():
        lines.append(f"NEURON_SCHEDULE__{tag}={cron}")
    return lines


def render_dotenv(bd=BRAIN_DIR, out=None) -> Path:
    """Split brain.env, write its flat zone (verbatim, under a generated banner) to
    brain_etc/docker/.env.rendered, THEN append the per-neuron gateway-token appendix (each
    neuron's named token resolved to its secret; config-flow Phase 4). Both the gateway_config
    generate path and the brain_truths apply/keepalive path call THIS one producer, so the shipped
    ~/docker/.env always carries the tokens compose needs and a keepalive never clobbers them.
    Returns the path written. Fails closed on a bad marker (the flat split); the token appendix is
    lenient (see _neuron_token_lines)."""
    bd = Path(bd)
    flat_text, _ = split_zones(_read_text(brain_env_path(bd)))
    out = Path(out) if out else rendered_env_path(bd)
    out.parent.mkdir(parents=True, exist_ok=True)
    body = _RENDER_HEADER + "\n" + flat_text.rstrip("\n") + "\n"
    token_lines = _neuron_token_lines(bd)
    if token_lines:
        body += ("\n# --- per-neuron gateway tokens (GENERATED from the neuron zone + token "
                 "registry) ---\n"
                 "# Each neuron's `gateway_token:` name, resolved to its secret. Compose neuron\n"
                 "# blocks read ${NEURON_TOKEN__<bundle>__<neuron>}. Regenerated every apply.\n"
                 + "\n".join(token_lines) + "\n")
    sched_lines = _neuron_schedule_lines(bd)
    if sched_lines:
        body += ("\n# --- neuron ingest schedule (GENERATED from the brain.env neuron zone) ---\n"
                 "# The in-distro neuron_schedule.py reads DEFAULT_INPUT_NEURON + each\n"
                 "# NEURON_SCHEDULE__<tag> from here (retires ~/docker/neuron/sources.yaml as the\n"
                 "# schedule source). Regenerated every apply.\n"
                 + "\n".join(sched_lines) + "\n")
    out.write_text(body, encoding="utf-8", newline="\n")
    return out


# --------------------------------------------------------------------------- #
# Render the neuron zone -> the runtime source manifest (sources.yaml)
# (config-flow Phase 5 - retires the hand-authored brain_etc/neuron/sources.yaml as an INPUT).
# --------------------------------------------------------------------------- #
# The input container reads /etc/neuron/sources.yaml (manifest.load_manifest via NEURON_MANIFEST).
# We render that SAME runtime shape from the zone so the container contract is unchanged; the only
# thing retired is the hand-authored file - the zone is now the single source of truth.
_ZONE_GIT_PROTOCOLS = {"ssh", "none"}


def zone_sources(neurons: dict) -> list:
    """Flatten the ===NEURONS=== zone's input-neuron sources into the runtime sources.yaml
    entry list (the shape manifest.load_manifest / manifest.Source consume). One entry per
    (input neuron x source). The input neuron's `schedule:` becomes the source's `tags:` so the
    scheduled `neuron --ingest-only --tags <schedule>` timer actually selects it (this closes the
    NOTE 001-43 tag wrinkle - the zone carries the cadence on the NEURON, sources.yaml on the SOURCE)."""
    out = []
    for bundle, n in iter_neurons(neurons, "input"):
        bname = bundle.get("name")
        collection = bundle_collection(bundle)
        sched = n.get("schedule")
        embed = n.get("embed_model")
        for s in (n.get("sources") or []):
            if not isinstance(s, dict) or not s.get("name"):
                continue
            entry = {
                "name": str(s["name"]),
                "neuron": n.get("name"),
                "bundle": bname,
                "collection": s.get("collection") or collection,
            }
            if sched:
                entry["tags"] = [sched]
            consumption = s.get("consumption")
            if consumption:
                entry["consumption"] = consumption
            if s.get("script"):
                entry["script"] = s["script"]
            if s.get("ingest_scope"):
                entry["ingest_scope"] = s["ingest_scope"]
            if embed:
                entry["embed_model"] = embed
            # git: translate the zone's nested `git:` block into the `delivery:` shape
            # manifest._deliver_git reads (url/ref/protocol/auth). Zone protocol is ssh|none
            # (https unsupported); none => keyless public HTTPS, ssh => vault key + pinned host.
            if str(consumption).lower() == "git":
                g = s.get("git") or {}
                proto = str(g.get("protocol") or "none").lower()
                deliv = {"adapter": "git"}
                if g.get("url"):
                    deliv["url"] = g["url"]
                if g.get("ref"):
                    deliv["ref"] = g["ref"]
                if proto == "ssh":
                    deliv["protocol"] = "ssh"
                    deliv["auth"] = "transient-cred"
                else:
                    deliv["protocol"] = "https"
                    deliv["auth"] = "public"
                entry["delivery"] = deliv
            out.append(entry)
    return out


_SOURCES_HEADER = (
    "# ===========================================================================\n"
    "# GENERATED - do NOT edit. Source of truth: brain_etc/brain.env (the ===NEURONS===\n"
    "# YAML zone). Config-flow Phase 5 retired the hand-authored brain_etc/neuron/sources.yaml\n"
    "# as an input; this runtime manifest is rendered from the zone on every apply by\n"
    "# brain_env.render_sources_yaml(). The seam manifest ships THIS to ~/docker/neuron/\n"
    "# sources.yaml, which the input container reads read-only at /etc/neuron/sources.yaml.\n"
    "# Edit the brain.env neuron zone, then apply.\n"
    "# ===========================================================================\n"
)


def render_sources_yaml(bd=BRAIN_DIR, out=None) -> Path:
    """Render the runtime source manifest (sources.yaml) from the brain.env neuron zone into
    brain_etc/docker/neuron/sources.yaml. Fails closed on a bad marker / invalid zone (same gate
    as the rest of the zone tooling). Returns the path written."""
    bd = Path(bd)
    neurons = load_neurons(brain_env_path(bd))
    doc = {
        "version": 1,
        "schedules": dict(neurons.get("schedules") or {}),
        "sources": zone_sources(neurons),
    }
    out = Path(out) if out else rendered_sources_path(bd)
    out.parent.mkdir(parents=True, exist_ok=True)
    body = _SOURCES_HEADER + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    out.write_text(body, encoding="utf-8", newline="\n")
    return out


def iter_additional_bundle_objects(bd=BRAIN_DIR) -> list:
    """The RICH non-default bundle objects (the FULL `neurons:` list — type/app/synth_model/
    embed_model/gateway_token/backends intact), what the SHARED compose block renderer
    (neuron_compose.render_input_block/render_action_block) needs to render additional bundles with
    per-neuron tokens/models/service_type (config-flow P0 unification). This is DISTINCT from
    zone_additional_bundles() below, whose names-only adapted view feeds topology/net allocation
    (which needs only name + net_index). Returns [] when the zone can't be loaded (a keepalive
    render must never die)."""
    try:
        flat = load_flat(brain_env_path(bd))
        neurons = load_neurons(brain_env_path(bd))
    except BrainEnvError:
        return []
    default = default_bundle_name(flat)
    return [b for b in iter_bundles(neurons) if b.get("name") and b.get("name") != default]


def zone_additional_bundles(bd=BRAIN_DIR) -> list:
    """The NON-default bundles of the neuron zone, adapted to the legacy registry-bundle shape the
    topology + additional-bundle compose renderers consume:
        {name, collection, description?, input_neurons:[{name}], action_neurons:[{name}]}
    Config-flow Phase 5 repoints neuron_topology + add_neuron_bundle off the retired
    brain_etc/neuron/bundles.yaml onto this zone-sourced view. The DEFAULT bundle (index 0) is
    handled separately by topology(); only the additional bundles come from here. Returns [] when
    the zone can't be loaded (a keepalive render must never die)."""
    try:
        flat = load_flat(brain_env_path(bd))
        neurons = load_neurons(brain_env_path(bd))
    except BrainEnvError:
        return []
    default = default_bundle_name(flat)
    out = []
    for b in iter_bundles(neurons):
        name = b.get("name")
        if not name or name == default:
            continue
        adapted = {
            "name": name,
            "collection": bundle_collection(b),
            "input_neurons": [{"name": n.get("name")}
                              for n in (b.get("neurons") or []) if n.get("type") == "input"],
            "action_neurons": [{"name": n.get("name")}
                               for n in (b.get("neurons") or []) if n.get("type") == "action"],
        }
        if b.get("description"):
            adapted["description"] = b["description"]
        out.append(adapted)
    return out


# --------------------------------------------------------------------------- #
# CLI - render / validate / show (test + operator surface)
# --------------------------------------------------------------------------- #
def _resolve_bd(args):
    return Path(args.brain_dir) if getattr(args, "brain_dir", None) else BRAIN_DIR


def cmd_render(args):
    bd = _resolve_bd(args)
    try:
        out = render_dotenv(bd)
        src = render_sources_yaml(bd)
    except BrainEnvError as e:
        die(str(e))
    info(f"rendered flat zone -> {out}")
    info(f"rendered source manifest -> {src}")
    return 0


def cmd_validate(args):
    bd = _resolve_bd(args)
    try:
        flat = load_flat(brain_env_path(bd))
        neurons = load_neurons(brain_env_path(bd))
    except BrainEnvError as e:
        die(str(e))
    nb = len(neurons.get("neuron_bundles") or [])
    nn = sum(1 for _ in iter_neurons(neurons))
    info(f"OK - {len(flat)} flat keys; {nb} bundle(s), {nn} neuron(s); "
         f"schedules: {sorted((neurons.get('schedules') or {}))}")
    return 0


def cmd_show(args):
    bd = _resolve_bd(args)
    try:
        flat = load_flat(brain_env_path(bd))
        neurons = load_neurons(brain_env_path(bd))
    except BrainEnvError as e:
        die(str(e))
    print(f"\n[brain_env] {brain_env_path(bd)}\n")
    print(f"  default bundle : {default_bundle_name(flat) or '(unset)'}")
    print(f"  flat keys      : {len(flat)}")
    print(f"  schedules      : {', '.join((neurons.get('schedules') or {})) or '(none)'}")
    print(f"  active timers  : {', '.join(active_schedules(neurons)) or '(none)'}\n")
    for b in iter_bundles(neurons):
        print(f"  bundle {b.get('name')}  (collection: {bundle_collection(b)})")
        for n in (b.get("neurons") or []):
            extra = (f"token={n.get('gateway_token')}" if n.get("gateway_token") else "")
            print(f"      - {n.get('type'):6} {n.get('name')}   {extra}")
    print()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Two-zone brain.env loader: split, render ~/docker/.env, "
                                             "and read the neuron zone.")
    ap.add_argument("--brain-dir", dest="brain_dir", default=None,
                    help="brain root (default: this tool's brain)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("render", help="write brain_etc/docker/.env.rendered from the flat zone")
    sub.add_parser("validate", help="parse + validate both zones; fail closed on error")
    sub.add_parser("show", help="human summary of both zones")
    args = ap.parse_args(argv)
    return {"render": cmd_render, "validate": cmd_validate, "show": cmd_show}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main() or 0)
