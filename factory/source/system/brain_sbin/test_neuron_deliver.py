#!/usr/bin/env python3
"""
test_neuron_deliver.py — unit-prove the delivery orchestrator's transient-cred resolver.

neuron_deliver.py decides, from the brain.env ===NEURONS=== zone's git sources (+ github.env),
WHICH transient channel a delivery run needs (the ssh vault, or neither). Config-flow Phase 5
retired the hand-authored sources.yaml as an input; sources now come from the zone, whose git
model is narrower: `consumption: git` + `git: {protocol: ssh|none}` (none = keyless public HTTPS;
https token delivery is UNSUPPORTED in the zone). So the only transient channel the zone can
express is the ssh vault (protocol: ssh). This drives _resolve_needs against synthetic brain.env
files — no distro, no docker, no secrets.

    test_neuron_deliver.py            run all cases
Exit code: 0 = all PASS, 1 = a case FAILED.  Stdlib + pyyaml (a neuron_deliver.py dep).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import neuron_deliver as nd

_FLAT = "BRAIN_NAME=testbrain\nDEFAULT_BUNDLE=b\n"


def _brain(tmp: Path, source_block: str, default_auth="public", default_proto="https") -> Path:
    """Build a minimal brain dir: brain_etc/github/github.env + a two-zone brain_etc/brain.env
    carrying ONE input neuron whose single source is `source_block` (a YAML `sources:` entry)."""
    (tmp / "brain_etc" / "github").mkdir(parents=True, exist_ok=True)
    (tmp / "brain_etc" / "github" / "github.env").write_text(
        f"GITHUB_DEFAULT_AUTH={default_auth}\nGITHUB_DEFAULT_PROTOCOL={default_proto}\n",
        encoding="utf-8")
    zone = (
        "schedules: {}\n"
        "neuron_bundles:\n"
        "  - name: b\n"
        "    default_collection: c\n"
        "    neurons:\n"
        "      - name: b_in\n"
        "        type: input\n"
        "        sources:\n"
        f"{source_block}"
    )
    (tmp / "brain_etc" / "brain.env").write_text(
        _FLAT + "===NEURONS===\n" + zone, encoding="utf-8")
    return tmp


# name -> (sources: entry, default_auth, default_proto, expected (need_token, need_vault))
CASES = {
    "git protocol none (keyless public)": (
        "          - name: A\n            consumption: git\n            git: {protocol: none, url: https://github.com/x/a.git}\n",
        "public", "https", (False, False)),
    "git protocol ssh (needs the ssh vault)": (
        "          - name: A\n            consumption: git\n            git: {protocol: ssh, url: 'git@github.com:x/a.git'}\n",
        "public", "https", (False, True)),
    "mixed none + ssh needs the vault": (
        "          - name: A\n            consumption: git\n            git: {protocol: none, url: https://github.com/x/a.git}\n"
        "          - name: B\n            consumption: git\n            git: {protocol: ssh, url: 'git@github.com:x/b.git'}\n",
        "public", "https", (False, True)),
    "non-git source ignored": (
        "          - name: A\n            consumption: script\n            script: obtain.py\n",
        "public", "https", (False, False)),
}


def main() -> int:
    failed = 0
    with tempfile.TemporaryDirectory() as td:
        for i, (name, (block, da, dp, expected)) in enumerate(CASES.items()):
            bdir = _brain(Path(td) / f"c{i}", block, da, dp)
            got = nd._resolve_needs(bdir)
            ok = got == expected
            failed += not ok
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got} expected {expected}")
    print(f"\n{'ALL PASS' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
