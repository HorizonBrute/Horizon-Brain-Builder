#!/usr/bin/env python3
"""
neuron_deliver.py — HOST-SIDE ORCHESTRATOR for the git-delivery WRITE phase.
===========================================================================

Delivery clones the manifest's git sources into brain_ro. For `auth: transient-cred`
sources the credential must reach the in-distro run WITHOUT ever being persisted under
the brain — and run_as_brain forwards neither env nor stdin across the Windows credential
boundary. So we reuse the SAME seam-drop convention as gh_auth.py: drop the secret as a
0600 file on the config seam's `.transient` dir (which the brain already sees read-only at
`/opt/brain_truths/…`), pass its IN-DISTRO path to neuron_deliver.sh, and SHRED it on exit
no matter what.

Two transient shapes, auto-detected from sources.yaml (+ github.env defaults):

  * transient-cred + https  -> an HTTPS token. The operator supplies it on THIS run only
                               (--https-token-file / --https-token-env); we drop it and the
                               in-distro adapter injects it as an Authorization header.
  * transient-cred + ssh    -> a key from the in-brain vault (gh_auth). We read the vault
                               STORE PASSPHRASE from the OS keystore, drop it, and the
                               in-distro side unseals into an ephemeral ssh-agent.

public / operator-delivered need no secret; we just run the delivery.

    neuron_deliver.py [--brain N] [--brain-dir D] [--dry-run]
        [--https-token-file FILE | --https-token-env VAR]

The vault passphrase is read the same way gh_auth.py writes it (shared keystore namespace).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

# Reuse gh_auth's keystore namespace + shred so the vault passphrase is read from EXACTLY
# where gh_auth.py stored it, and the seam-drop shred discipline is identical.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gh_auth  # noqa: E402  (same brain_sbin dir)
import brain_env  # noqa: E402  (the two-zone brain.env loader; sources now come from the zone)

SEAM_MOUNT = gh_auth.SEAM_MOUNT                         # /opt/brain_truths
TRANSIENT_REL = gh_auth.TRANSIENT_REL                  # ("github","gh_auth",".transient")
TRANSIENT_INDISTRO = gh_auth.TRANSIENT_INDISTRO


def info(m): print(f"  {m}")
def die(m): print(f"  [ERROR] {m}", file=sys.stderr); sys.exit(1)


# --------------------------------------------------------------------------- #
# Identity / paths
# --------------------------------------------------------------------------- #
def brain_dir(args) -> Path:
    return Path(args.brain_dir) if args.brain_dir else Path(__file__).resolve().parents[2]


def brain_name(args) -> str:
    return args.brain or brain_dir(args).name


# --------------------------------------------------------------------------- #
# Resolve what the manifest needs (github.env defaults, per-source overrides)
# --------------------------------------------------------------------------- #
def _env_defaults(bdir: Path) -> tuple[str, str]:
    """(default_auth, default_protocol) from brain_etc/github/github.env."""
    auth, proto = "public", "https"
    envf = bdir / "brain_etc" / "github" / "github.env"
    if envf.is_file():
        for line in envf.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("GITHUB_DEFAULT_AUTH="):
                auth = line.split("=", 1)[1].strip().lower() or auth
            elif line.startswith("GITHUB_DEFAULT_PROTOCOL="):
                proto = line.split("=", 1)[1].strip().lower() or proto
    return auth, proto


def _resolve_needs(bdir: Path) -> tuple[bool, bool]:
    """(need_https_token, need_ssh_vault) across all git sources in the manifest."""
    d_auth, d_proto = _env_defaults(bdir)
    # Config-flow Phase 5: sources come from the brain.env ===NEURONS=== zone (rendered to the same
    # runtime sources.yaml shape), not the retired hand-authored brain_etc/neuron/sources.yaml.
    try:
        neurons = brain_env.load_neurons(brain_env.brain_env_path(bdir))
    except brain_env.BrainEnvError as e:
        die(f"cannot read the neuron zone from brain.env: {e}")
    need_token = need_vault = False
    for src in brain_env.zone_sources(neurons):
        deliv = (src or {}).get("delivery", {}) or {}
        if deliv.get("adapter") != "git":
            continue
        auth = str(deliv.get("auth", d_auth)).strip().lower()
        if auth != "transient-cred":
            continue
        proto = str(deliv.get("protocol", d_proto)).strip().lower()
        url = str(deliv.get("url", ""))
        # ssh when protocol says so OR the URL is already an ssh remote.
        if proto == "ssh" or url.startswith("git@") or url.startswith("ssh://"):
            need_vault = True
        else:
            need_token = True
    return need_token, need_vault


# --------------------------------------------------------------------------- #
# Seam-drop + run (mirrors gh_auth.run_in_distro; shreds on exit no matter what)
# --------------------------------------------------------------------------- #
def _run(args, need_token: bool, need_vault: bool) -> int:
    bdir, brain = brain_dir(args), brain_name(args)
    tdir = bdir.joinpath("brain_etc", *TRANSIENT_REL)
    tdir.mkdir(parents=True, exist_ok=True)
    dropped: list[Path] = []

    def drop(data: bytes, suffix: str) -> str:
        fd, name = tempfile.mkstemp(dir=str(tdir), suffix=suffix)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        p = Path(name); dropped.append(p)
        try: os.chmod(p, 0o600)
        except (OSError, NotImplementedError): pass
        return f"{TRANSIENT_INDISTRO}/{p.name}"

    wsl = bdir / "system" / "brain_sbin" / "wsl_scripts.py"
    if args.dry_run:
        # Render only — never fetch or drop the real secret for a dry-run.
        rendered = []
        if need_token:
            rendered += ["--token-file", f"{TRANSIENT_INDISTRO}/<https-token>"]
        if need_vault:
            rendered += ["--ssh-pass-file", f"{TRANSIENT_INDISTRO}/<store-passphrase>"]
        cmd = [sys.executable, str(wsl), "--brain", brain, "--brain-dir", str(bdir),
               "run", "neuron_deliver.sh", "--", *rendered]
        info("[dry-run] would run (secrets read from the seam .transient drop, shredded on exit):")
        info("          " + " ".join(cmd))
        return 0

    script_args: list[str] = []
    try:
        if need_token:
            token = _get_https_token(args)
            script_args += ["--token-file", drop(token.encode(), ".tok")]
        if need_vault:
            pw = gh_auth.get_pass(brain)      # from the OS keystore (gh_auth namespace)
            script_args += ["--ssh-pass-file", drop(pw.encode(), ".pw")]

        cmd = [sys.executable, str(wsl), "--brain", brain, "--brain-dir", str(bdir),
               "run", "neuron_deliver.sh", "--", *script_args]
        return subprocess.run(cmd, text=True).returncode
    finally:
        for p in dropped:                     # shred NO MATTER WHAT
            gh_auth._shred(p)


def _get_https_token(args) -> str:
    if args.https_token_file:
        t = Path(args.https_token_file).read_text(encoding="utf-8").strip()
        if not t:
            die(f"--https-token-file is empty: {args.https_token_file}")
        return t
    if args.https_token_env:
        t = os.environ.get(args.https_token_env, "").strip()
        if not t:
            die(f"--https-token-env ${args.https_token_env} is unset/empty in this run's environment.")
        return t
    die("a transient-cred https source needs a token this run: pass --https-token-file FILE "
        "or --https-token-env VAR (the token is dropped 0600 on the seam and shredded on exit).")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="neuron_deliver",
                                 description="Host orchestrator for git delivery (transient-cred aware).")
    ap.add_argument("--brain")
    ap.add_argument("--brain-dir")
    ap.add_argument("--dry-run", action="store_true", help="resolve + print the run; drop/run nothing live")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--https-token-file", help="file holding the HTTPS transient token (dropped, then shredded)")
    g.add_argument("--https-token-env", help="env var holding the HTTPS transient token for THIS run")
    args = ap.parse_args(argv)

    bdir = brain_dir(args)
    need_token, need_vault = _resolve_needs(bdir)
    if not (need_token or need_vault):
        info("no transient-cred source in the manifest (public / operator-delivered) — plain delivery.")
    else:
        info(f"transient-cred detected: https-token={need_token}  ssh-vault={need_vault}")
    return _run(args, need_token, need_vault)


if __name__ == "__main__":
    raise SystemExit(main())
