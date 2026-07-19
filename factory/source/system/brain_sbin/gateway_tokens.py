#!/usr/bin/env python3
"""
gateway_tokens.py - the unified bearer-token REGISTRY + map generator (brain_sbin).
==================================================================================

ADR projects/2ndbraindevelopment/decisions/0010-unified-bearer-token-registry.md.

ONE authoritative file - `brain_etc/gateway/token_registry` - is the single source
of truth for every bearer token across every service the gateway fronts. Each token
is listed ONCE with per-service:role grants. Backend tooling (this module) GENERATES
the per-service nginx `map` files from it:

    token_registry  --generate-->  reader_tokens.map     ($is_reader - chroma:reader)
                                    writer_tokens.map     ($is_writer - chroma:writer)
                                    ollama_use.map        ($is_ollama_use   - ollama:use)
                                    ollama_admin.map      ($is_ollama_admin - ollama:admin)

WHY (the administration problem)
------------------------------------------
A separate `.map` per service+role means the same key is hand-copied into every file
it should be honored in and kept absent from every file it should not - error-prone
and strictly worse per service added. Here the admin edits ONE file; a key is never
hand-synced; a token removed from the registry disappears from every map (maps are
regenerated whole, never edited). Adding a service = one new grant name + one emitted
map, no new hand-maintained convention.

REGISTRY FORMAT (line-based, stdlib-parseable - no YAML dep)
-----------------------------------------------------------
One token per line; blank lines and `#`-comment lines ignored:

    <token>   <service:role>[ <service:role> ...]   [# label=<label> created=<iso8601>]

  * field 1                = the raw bearer secret (hex).
  * remaining fields (up   = grants, whitespace-separated, until an inline `#`.
    to the inline `#`)
  * inline `# ...`         = OPTIONAL metadata (label=, created=()) - legibility only,
                             carried through by the CLI; nginx never sees it.

Recognized grants: chroma:reader  chroma:writer  ollama:use  ollama:admin
An UNKNOWN grant is a hard error (fail-closed - a typo must not silently drop access
nor silently widen it). One grant -> exactly one map; role SUPERSET semantics (a writer
may read; an ollama admin may use) live in nginx's admission map, NOT here, so each map
stays a clean membership list (mirrors how the chroma writer is not copied into the
reader map - nginx grants writers read access).

WHERE IT RUNS / THE REAL GATE
-----------------------------
Host-side, as the brain's OPERATOR (identical posture + probe-write gate to
gateway_token.py - the registry and maps are the OPERATOR-owned seam source in
`brain_etc/gateway/`; the brain account is granted RX only). See gateway_token.py's
module docstring for the full ACL rationale.

CRLF / TOKEN HYGIENE
--------------------
Everything is written as LF bytes (a stray '\\r' has bitten this seam twice - obj 008).
A raw token is printed EXACTLY ONCE, at grant/rotate; `list` shows only label + a
sha256 fingerprint, never the token. The registry file itself holds raw tokens (it IS
the secret surface) and inherits brain_etc admin-only permissions.
"""
import argparse
import hashlib
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent          # brains/<brain>/brain_sbin
BRAIN_DIR = HERE.parent.parent                          # brains/<brain>
RUN_AS_BRAIN = HERE / "run_as_brain.py"
APPLY_SH = "/opt/brain_wsl_in_distro_scripts/apply_brain_truths.sh"

REGISTRY_NAME = "token_registry"
# Generated nginx maps land in this sibling subfolder (backend artifacts, never
# hand-edited), keeping the gateway/ human view clean (token_registry + gateway.conf).
MAPS_SUBDIR = "token_maps_auto_gen"

# grant "service:role"  ->  generated map filename ($is_* the nginx map feeds).
# This table IS the set of legal grants; anything else fails closed (see parse).
GRANT_MAPS = {
    "chroma:reader": "reader_tokens.map",
    "chroma:writer": "writer_tokens.map",
    "ollama:use":    "ollama_use.map",
    "ollama:admin":  "ollama_admin.map",
    # action:call - a bearer permitted to call the action query API (:8443, ADR 0017 sec Next).
    # A single membership grant (no read/write tiers): the app behind it holds its OWN scoped
    # reader token, so this only gates WORLD -> API admission (authorization-in).
    "action:call":   "action_tokens.map",
}
LEGAL_GRANTS = tuple(GRANT_MAPS.keys())

# NEURON NAMED-TOKEN MODEL (config-flow refactor, Phase 3). A neuron references a token
# BY NAME (its `gateway_token:` in the brain.env YAML zone == an Entry `label` here); nothing
# is auto-minted. The operator creates the named token with `grant --label <name> --grant ...`
# BEFORE the neuron runs. This is the DEFAULT grant a neuron of each type is expected to carry
# (the prototype's security posture: an INPUT neuron WRITES the collection, an ACTION neuron
# only READS it). A neuron MAY name a token with different grants - unwise, not blocked; the
# resolver (gateway_config.resolve_neuron_tokens) WARNS on the mismatch but does not fail.
DEFAULT_NEURON_GRANT = {"input": "chroma:writer", "action": "chroma:reader"}


def die(m): print(f"[gateway_tokens] ERROR: {m}", file=sys.stderr); sys.exit(1)
def info(m): print(f"[gateway_tokens] {m}")


def gateway_dir(brain_dir=BRAIN_DIR):
    return Path(brain_dir) / "brain_etc" / "gateway"


def registry_path(brain_dir=BRAIN_DIR):
    return gateway_dir(brain_dir) / REGISTRY_NAME


def fingerprint(token):
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Registry model - pure, testable. An Entry is (token, grants, label, created).
# --------------------------------------------------------------------------- #
class Entry:
    __slots__ = ("token", "grants", "label", "created")

    def __init__(self, token, grants, label="", created=""):
        self.token = token
        self.grants = list(grants)
        self.label = label
        self.created = created


def parse_registry(text, source="<registry>"):
    """Parse registry text -> [Entry]. Fail closed on any unknown grant or dup token."""
    entries, seen = [], set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Split off an inline metadata comment (everything from the first '#').
        body, _, comment = line.partition("#")
        fields = body.split()
        if len(fields) < 2:
            die(f"{source}:{lineno}: need '<token> <service:role>...' - got: {raw!r}")
        # Grants may be space-separated fields AND/OR comma-separated within a field,
        # so one token can carry many roles: `chroma:writer,ollama:admin` or the older
        # `chroma:writer ollama:admin`. Both flatten to the same grant list.
        token = fields[0]
        grants = [g for f in fields[1:] for g in f.split(",") if g]
        if token in seen:
            die(f"{source}:{lineno}: duplicate token (fingerprint {fingerprint(token)})")
        seen.add(token)
        for g in grants:
            if g not in GRANT_MAPS:
                die(f"{source}:{lineno}: unknown grant {g!r} "
                    f"(legal: {', '.join(LEGAL_GRANTS)})")
        meta = dict(re.findall(r"(\w+)=(\S+)", comment))
        entries.append(Entry(token, grants, meta.get("label", ""), meta.get("created", "")))
    return entries


def read_registry(brain_dir=BRAIN_DIR):
    p = registry_path(brain_dir)
    if not p.is_file():
        return []
    return parse_registry(p.read_text(encoding="utf-8"), str(p))


def render_registry(entries):
    """Entries -> registry file text (LF). Header documents the format + generation."""
    out = [
        "# Unified bearer-token registry - SINGLE SOURCE OF TRUTH.",
        "# The per-service nginx maps (reader/writer/ollama_use/ollama_admin) are GENERATED",
        "# from this file by system/brain_sbin/gateway_tokens.py - NEVER edit a .map by hand.",
        "# Format:  <token>  <service:role>[,<service:role>...]   [# label=<l> created=<iso8601>]",
        "# One token, many grants: comma-separated (chroma:writer,ollama:admin) or spaces.",
        f"# Grants:  {'  '.join(LEGAL_GRANTS)}",
        "# Raw bearer secrets live here - this file inherits brain_etc admin-only perms.",
        "",
    ]
    for e in entries:
        meta = []
        if e.label:
            meta.append(f"label={e.label}")
        if e.created:
            meta.append(f"created={e.created}")
        comment = f"   # {' '.join(meta)}" if meta else ""
        # Render multi-grant tokens comma-joined (chroma:writer,ollama:admin) - the
        # preferred single-field form; a single grant renders bare.
        out.append(f"{e.token}  {','.join(e.grants)}{comment}")
    return "\n".join(out) + "\n"


def _write_lf(path, text):
    """Atomic LF-bytes write (never CRLF - see module docstring)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def write_registry(entries, brain_dir=BRAIN_DIR):
    _write_lf(registry_path(brain_dir), render_registry(entries))


# --------------------------------------------------------------------------- #
# The GENERATOR - registry -> the four nginx map files. Pure + deterministic.
# --------------------------------------------------------------------------- #
def render_map(mapname, tokens):
    """One nginx map file's text: header + one anchored Bearer line per token.
    Byte-compatible with the lines gateway_token.py used to hand-author, so the
    migration is transparent to nginx (same include, same match)."""
    header = (
        f"# {mapname} - GENERATED from token_registry by gateway_tokens.py. DO NOT EDIT.\n"
        f"# Edit brain_etc/gateway/token_registry and re-run `gateway_tokens.py generate`.\n"
    )
    body = "".join(f'"~*^Bearer\\s+{t}$"  1;\n' for t in tokens)
    return header + body


def generate(entries, out_dir):
    """Emit all four maps into out_dir. Returns {mapname: token_count}. Every map is
    written EVERY time (fresh from the registry) - a token pulled from the registry
    therefore vanishes from its map; there are no orphaned keys. An always-empty map
    is still written (nginx `include` aborts on a missing file - the map must exist)."""
    out_dir = Path(out_dir) / MAPS_SUBDIR      # backend artifacts land in the subfolder
    out_dir.mkdir(parents=True, exist_ok=True)
    buckets = {mapname: [] for mapname in GRANT_MAPS.values()}
    for e in entries:
        for g in e.grants:
            buckets[GRANT_MAPS[g]].append(e.token)
    counts = {}
    for mapname, tokens in buckets.items():
        _write_lf(out_dir / mapname, render_map(mapname, tokens))
        counts[mapname] = len(tokens)
    return counts


# --------------------------------------------------------------------------- #
# Operator gate (probe write) - identical contract to gateway_token.py.
# --------------------------------------------------------------------------- #
def require_operator():
    d = gateway_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write_probe.tmp"
        probe.write_bytes(b"probe")
        probe.unlink()
    except OSError as e:
        die(f"cannot write the gateway seam ({d}): {e}\n"
            f"    Run as the brain's OPERATOR - the owner of brains/{BRAIN_DIR.name}, a member of the\n"
            f"    host's human-operators group, or an admin. The brain account is granted RX\n"
            f"    only (no write) by design.")


def apply_and_recreate(brain):
    """Sync brain_etc -> ~/docker (apply primitive) then FORCE-RECREATE the gateway so it
    re-reads the regenerated maps. Force-recreate (not reload) because the maps are file
    bind mounts bound by inode - see gateway_token.py.apply_and_recreate for the full why.
    The seam source is already written, so the change is durable even if this can't run now."""
    if not RUN_AS_BRAIN.is_file():
        info(f"run_as_brain not found ({RUN_AS_BRAIN}); maps written to the seam source and "
             f"will apply on the next keepalive. Skipping live recreate.")
        return
    # NOTE 001-58: recreate with the SAME env-derived compose overlays every other recreate site
    # uses (gateway_config.compose_files), NOT compose.yaml alone. A base-only recreate drops all
    # the gateway LAN host-port publishes (chroma 8000 / ollama 11434 / action 8443), sealing the
    # brain off-box on every token grant/rotate/revoke. Lazy import: gateway_config imports this
    # module at load, so importing it at module top would be circular.
    import gateway_config
    env = gateway_config.read_env(gateway_config.brain_env_path(BRAIN_DIR))
    dockerdir = f"/home/{brain}/docker"
    fflags = " ".join(f"-f {dockerdir}/{f}" for f in gateway_config.compose_files(env))
    apply = (f"bash {APPLY_SH} -- "
             f"docker compose {fflags} up -d --force-recreate gateway")
    rc = subprocess.run([sys.executable, str(RUN_AS_BRAIN),
                         "--brain", brain, "--wsl", "--", apply]).returncode
    if rc != 0:
        info("live recreate did NOT complete (rc=%d). Maps are SAVED in the seam source" % rc)
        info("  (durable) and will apply on the next keepalive.")
    else:
        info("gateway synced + recreated - maps are live.")


def _regen_and_apply(args, entries):
    """Common tail for the write verbs: persist registry, regenerate maps, go live."""
    write_registry(entries, BRAIN_DIR)
    counts = generate(entries, gateway_dir())
    info("regenerated maps: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    if not getattr(args, "no_apply", False):
        apply_and_recreate(args.brain)


# --------------------------------------------------------------------------- #
# CLI verbs
# --------------------------------------------------------------------------- #
def cmd_generate(args):
    """Registry -> maps, no token change (used by the seam apply / deploy)."""
    entries = read_registry(BRAIN_DIR)
    counts = generate(entries, gateway_dir())
    info(f"{len(entries)} token(s) -> " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    if getattr(args, "apply", False):
        apply_and_recreate(args.brain)
    return 0


def cmd_mint(args):
    """Print a fresh random bearer secret and do NOTHING else - no registry write, no
    operator gate, no live apply. The 'just give me a key' tool: use it to seed
    CHROMA_MASTER_TOKEN_FOR_GW, hand a token to `grant`/`create`, or generate any opaque secret.
    Default 32 bytes = a 256-bit token (64 hex chars), the same strength grant/rotate mint."""
    print(secrets.token_hex(args.bytes))
    return 0


def cmd_list(args):
    entries = read_registry(BRAIN_DIR)
    if not entries:
        info(f"registry empty or absent ({registry_path(BRAIN_DIR)})")
        return 0
    print(f"\nToken registry ({registry_path(BRAIN_DIR)}):")
    for e in entries:
        label = e.label or "(unlabeled)"
        print(f"  {label:<24} {fingerprint(e.token):<20} "
              f"grants=[{', '.join(e.grants)}] created={e.created or '?'}")
    print()
    return 0


def _validate_grants(grants):
    if not grants:
        die("at least one --grant service:role is required")
    for g in grants:
        if g not in GRANT_MAPS:
            die(f"unknown grant {g!r} (legal: {', '.join(LEGAL_GRANTS)})")


def cmd_grant(args):
    """Mint a NEW token with the given grants (printed once)."""
    _validate_grants(args.grant)
    entries = read_registry(BRAIN_DIR)
    if args.label and any(e.label == args.label for e in entries):
        die(f"label '{args.label}' already exists - use `rotate` or pick another label")
    token = secrets.token_hex(32)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries.append(Entry(token, args.grant, args.label, now))
    _regen_and_apply(args, entries)
    print("\n" + "=" * 64)
    print(f"  NEW token  (label={args.label or '(unlabeled)'})  grants=[{', '.join(args.grant)}]")
    print(f"  Authorization: Bearer {token}")
    print("  ^ shown ONCE - copy it now; it cannot be recovered, only rotated.")
    print("=" * 64 + "\n")
    return 0


def find_by_label(entries, label):
    """Lenient public lookup: the Entry with this label, or None (no dying). The write verbs
    keep labels unique, so a first-match is the only match; used by the neuron named-token
    resolver, which produces its own fail-closed message when a named token is absent."""
    return next((e for e in entries if e.label == label), None)


def _find_by_label(entries, label):
    hits = [e for e in entries if e.label == label]
    if not hits:
        die(f"no token with label '{label}' in the registry")
    if len(hits) > 1:
        die(f"multiple tokens share label '{label}' - registry is inconsistent")
    return hits[0]


def cmd_revoke(args):
    entries = read_registry(BRAIN_DIR)
    victim = _find_by_label(entries, args.label)
    entries = [e for e in entries if e is not victim]
    _regen_and_apply(args, entries)
    info(f"revoked token label={args.label} (fingerprint {fingerprint(victim.token)})")
    return 0


def cmd_rotate(args):
    """Re-mint the token under the same label + grants; old secret dies."""
    entries = read_registry(BRAIN_DIR)
    old = _find_by_label(entries, args.label)
    grants = list(args.grant) if args.grant else old.grants
    _validate_grants(grants)
    token = secrets.token_hex(32)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = [e for e in entries if e is not old]
    entries.append(Entry(token, grants, args.label, now))
    _regen_and_apply(args, entries)
    print("\n" + "=" * 64)
    print(f"  ROTATED token  (label={args.label})  grants=[{', '.join(grants)}]")
    print(f"  Authorization: Bearer {token}")
    print("  ^ shown ONCE - the previous secret for this label is now dead.")
    print("=" * 64 + "\n")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Unified gateway bearer-token registry + nginx map generator "
                    "(run host-side as the brain's operator).")
    ap.add_argument("--brain", default=BRAIN_DIR.name,
                    help=f"brain name -> brains/<brain>/brain_etc/gateway/ + /home/<brain> "
                         f"(default from this tool's folder: {BRAIN_DIR.name})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="registry -> the four nginx maps (no token change)")
    g.add_argument("--apply", action="store_true", help="also sync + recreate the gateway live")

    mn = sub.add_parser("mint", help="print a fresh random bearer token; touch nothing else")
    mn.add_argument("--bytes", type=int, default=32,
                    help="entropy in bytes (default 32 = 256-bit / 64 hex chars)")

    sub.add_parser("list", help="show labels + fingerprints + grants (never the tokens)")

    gr = sub.add_parser("grant", help="mint a new token with grants (printed once)")
    gr.add_argument("--label", help="unique human label, e.g. obsidian-readback")
    gr.add_argument("--grant", action="append", default=[], metavar="service:role",
                    help=f"repeatable; one of: {', '.join(LEGAL_GRANTS)}")
    gr.add_argument("--no-apply", action="store_true", help="write seam only; skip live recreate")

    rv = sub.add_parser("revoke", help="remove a token by label + regenerate")
    rv.add_argument("--label", required=True)
    rv.add_argument("--no-apply", action="store_true")

    ro = sub.add_parser("rotate", help="re-mint under the same label (optionally change grants)")
    ro.add_argument("--label", required=True)
    ro.add_argument("--grant", action="append", default=[], metavar="service:role",
                    help="optional new grant set (default: keep the existing grants)")
    ro.add_argument("--no-apply", action="store_true")

    args = ap.parse_args()
    # mint/list/generate touch no live gateway state; only the write verbs need the seam gate.
    if args.cmd not in ("list", "generate", "mint"):
        require_operator()
    return {"generate": cmd_generate, "list": cmd_list, "mint": cmd_mint, "grant": cmd_grant,
            "revoke": cmd_revoke, "rotate": cmd_rotate}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main() or 0)
