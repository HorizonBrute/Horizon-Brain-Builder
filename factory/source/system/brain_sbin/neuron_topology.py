#!/usr/bin/env python3
r"""
neuron_topology.py — the per-bundle neuron-network addressing scheme (ADR-0015).
================================================================================
Single source of truth shared by add_neuron_bundle.py (which renders the docker networks +
the gateway's per-net attachment into compose.yaml) and gateway_config.py (which renders one
nginx internal `listen <gw_ip>:8000 / :11434` per net). Keeping the scheme HERE means the two
generators can never disagree on a bundle's subnet or the gateway's IP on it.

MODEL (per-bundle isolation):
  * Each neuron BUNDLE gets its OWN docker network, a /24 carved from 192.168.0.0/16 by an
    ascending index. The DEFAULT bundle is index 0; every ADDITIONAL bundle (registry
    brain_etc/neuron/bundles.yaml) takes the next free index.
  * The DEFAULT bundle keeps the stable compose network KEY `neuron_net` (its name is dynamic
    in the factory — DEFAULT_BUNDLE falls back to the brain name — so a bundle-derived key
    would not be templatable). ADDITIONAL bundles, whose names are static registry identifiers,
    get the key `<bundle>_neuron_net`.
  * The gateway is multi-homed onto brain_net + EVERY bundle net, taking a STATIC .2 on each
    (nginx binds its internal chroma/ollama listeners to it) and answering to chroma/ollama
    there. A neuron sits ONLY on its own bundle net -> it cannot reach another bundle's net,
    and reaches the sealed backends solely through the gateway.
  * Dynamic neuron IPs on each net come ONLY from .128/25, so a neuron can never be handed the
    gateway's static .2 regardless of start order (the collision-proofing, now per-net).
  * Backends (chroma/ollama) + brain_net stay in 172 space (brain_net pinned 172.16.0.0/23),
    so the 192.168 neuron nets never overlap them.
"""
from __future__ import annotations

import re
from pathlib import Path

import brain_env   # sibling: the two-zone brain.env loader (the additional-bundle registry, Phase 5)

# --- the scheme (authoritative; brain.env mirrors these as documented knobs for the default) --
NET_BASE = "192.168"      # the /16 the per-bundle /24s are carved from
GW_HOST = 2               # gateway static host octet on each neuron net (.2)
DYN_RANGE = "128/25"      # dynamic-IP pool (host part) neurons draw from on each net

BRAIN_ENV_REL = "brain_etc/brain.env"


def subnet(idx: int) -> str:       return f"{NET_BASE}.{idx}.0/24"
def gw_ip(idx: int) -> str:        return f"{NET_BASE}.{idx}.{GW_HOST}"
def dyn_ip_range(idx: int) -> str: return f"{NET_BASE}.{idx}.{DYN_RANGE}"


def default_net(bd: Path) -> dict:
    """The DEFAULT bundle's net, taken from the brain.env control-panel knobs
    (NEURON_NET_SUBNET / GATEWAY_NEURON_IP / NEURON_NET_DYNRANGE) so the ONE panel is
    authoritative for BOTH generators (compose templates these knobs directly; the gateway's
    internal nginx `listen` comes through here). A blank knob falls back to the index-0 scheme
    value, so an untouched brain still lands on 192.168.0.0/24. This closes the desync where
    compose honored the knob but the gateway hardcoded .0.2 (see the config-flow refactor)."""
    ep = bd / BRAIN_ENV_REL
    return {
        "subnet":   _env_value(ep, "NEURON_NET_SUBNET")  or subnet(0),
        "gw_ip":    _env_value(ep, "GATEWAY_NEURON_IP")  or gw_ip(0),
        "ip_range": _env_value(ep, "NEURON_NET_DYNRANGE") or dyn_ip_range(0),
    }


def net_key(name: str, index: int) -> str:
    """The compose network KEY services reference. The default bundle (index 0) keeps the
    stable, factory-templatable key `neuron_net`; additional bundles are `<bundle>_neuron_net`."""
    return "neuron_net" if index == 0 else f"{name}_neuron_net"


def _env_value(env_path: Path, key: str) -> str:
    """Current value of KEY in an env file, or '' if unset/blank/absent (tolerant parser)."""
    if not env_path.is_file():
        return ""
    pat = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}=(.*)$")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.strip().lstrip("#").strip().startswith("===NEURONS==="):
            break                      # flat zone only; keys never live in the YAML neuron zone
        m = pat.match(line)
        if not m:
            continue
        return re.sub(r"\s+#.*$", "", m.group(1)).strip().strip('"').strip("'")
    return ""


def resolve_default_bundle(bd: Path) -> str:
    """The DEFAULT bundle name (hand-authored in compose.yaml). DEFAULT_BUNDLE seam in brain.env;
    unset/empty falls back to the brain name (the factory default)."""
    return _env_value(bd / BRAIN_ENV_REL, "DEFAULT_BUNDLE") or bd.name


def load_registry(bd: Path) -> list:
    """The ADDITIONAL neuron bundles (everything but the default). Config-flow Phase 5 repoints this
    off the retired brain_etc/neuron/bundles.yaml onto the brain.env ===NEURONS=== zone
    (brain_env.zone_additional_bundles), so bundles are authored in the ONE control panel."""
    return brain_env.zone_additional_bundles(Path(bd))


def allocate_indices(bundles: list) -> dict:
    """bundle name -> net_index for the ADDITIONAL bundles. Honors an explicit `net_index` in the
    registry; assigns the next free index (>=1; 0 is reserved for the default bundle) to any
    without one. Stable: an assigned index is never reused for a different bundle."""
    used = {0}
    for b in bundles:
        i = b.get("net_index")
        if isinstance(i, int):
            used.add(i)
    nxt = 1
    out = {}
    for b in bundles:
        i = b.get("net_index")
        if not isinstance(i, int):
            while nxt in used:
                nxt += 1
            i = nxt
            used.add(i)
            nxt += 1
        out[str(b["name"])] = i
    return out


def topology(bd: Path) -> list:
    """Ordered list of {name, index, key, subnet, gw_ip, ip_range}: the DEFAULT bundle (index 0)
    first, then the registry bundles ascending by index. `key` is the compose network key."""
    entries = [{"name": resolve_default_bundle(bd), "index": 0}]
    reg = load_registry(bd)
    idx = allocate_indices(reg)
    for b in sorted(reg, key=lambda b: idx[str(b["name"])]):
        entries.append({"name": str(b["name"]), "index": idx[str(b["name"])]})
    dnet = default_net(bd)
    for e in entries:
        i = e["index"]
        if i == 0:
            # default bundle: the brain.env knobs are authoritative (fallback to the scheme)
            e.update(key=net_key(e["name"], i), subnet=dnet["subnet"],
                     gw_ip=dnet["gw_ip"], ip_range=dnet["ip_range"])
        else:
            e.update(key=net_key(e["name"], i), subnet=subnet(i),
                     gw_ip=gw_ip(i), ip_range=dyn_ip_range(i))
    return entries


if __name__ == "__main__":
    import sys
    bd = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent.parent
    for e in topology(bd):
        print(f"  idx {e['index']:>2}  {e['name']:<20} key={e['key']:<24} "
              f"subnet={e['subnet']:<18} gw={e['gw_ip']:<14} dyn={e['ip_range']}")
