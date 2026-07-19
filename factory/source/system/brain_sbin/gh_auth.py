#!/usr/bin/env python3
"""
gh_auth.py — in-brain credential vault: HOST-SIDE ORCHESTRATOR.
==============================================================

The gpg crypto lives in a POSIX script that runs IN the brain's distro (native gpg-agent):
    system/brain_sbin/wsl_in_distro_scripts/gh_auth_gpg.sh
This host tool does NOT run gpg (a Windows gpg cannot socket an agent for a native-Windows
GNUPGHOME). It owns the two things that ARE host-side:

  1. the store PASSPHRASE, in the OS keystore (same vault + convention as the brain login
     password — see run_as_brain.KEYRING). Never printed; the brain never holds it.
  2. ORCHESTRATION: invoke gh_auth_gpg.sh in-distro via run_as_brain, forwarding the
     passphrase and any cleartext key material.

SECRET FORWARDING: the brain distro is SEALED from the host filesystem (interop off, no
/mnt automount), so we reuse an EXISTING seam surface instead. The host has RW on brain_etc,
which the brain already sees read-only at `/opt/brain_truths` (a live drvfs mount). The host
DROPS the passphrase / cleartext material into `brain_etc/github/gh_auth/.transient/` (mode
0600, gitignored), the in-distro script READS it at
`/opt/brain_truths/github/gh_auth/.transient/…`, and the host SHREDS it on exit NO MATTER
WHAT — success or failure. Nothing on argv, no base64, no giant command strings; just a file
placed on a seam the brain can already read.

    gh_auth.py [--brain N] [--brain-dir D] init | status
        import-ssh --file CLEARTEXT [--shred]
        import-gpg (--file CLEARTEXT | --key-id ID) [--import-passphrase-file F] [--shred]
        reset-password --force | recreate --force

The store lives on ext4 in-distro (brain_rw), NOT the config seam — the agent needs a POSIX
homedir, the brain must WRITE it, and key material stays out of the git repo entirely.
"""
from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

# The in-distro script, at its read-only seam mount (wsl_scripts.MOUNT_POINT).
REMOTE_SCRIPT = "/opt/brain_wsl_in_distro_scripts/gh_auth_gpg.sh"
# The brain_etc config seam, as the brain sees it read-only in-distro (brain_truths.MOUNT_POINT).
SEAM_MOUNT = "/opt/brain_truths"
# Host-side transient drop dir (under brain_etc/github/gh_auth), and its in-distro view.
TRANSIENT_REL = ("github", "gh_auth", ".transient")
TRANSIENT_INDISTRO = f"{SEAM_MOUNT}/" + "/".join(TRANSIENT_REL)

# OS keystore: mirror run_as_brain's brain-owned namespace (same vault).
KEYRING_SERVICE = "brain:{brain}"
KEYRING_USER = "gh_auth_gpg_passphrase"


def info(m): print(f"  {m}")
def die(m): print(f"  [ERROR] {m}", file=sys.stderr); sys.exit(1)


# --------------------------------------------------------------------------- #
# Identity / paths
# --------------------------------------------------------------------------- #
def brain_dir(args) -> Path:
    return Path(args.brain_dir) if args.brain_dir else Path(__file__).resolve().parents[2]


def brain_name(args) -> str:
    return args.brain or brain_dir(args).name


def _rab(bdir: Path) -> Path:
    return bdir / "system" / "brain_sbin" / "run_as_brain.py"


# --------------------------------------------------------------------------- #
# OS keystore (passphrase never leaves the host / keystore)
# --------------------------------------------------------------------------- #
def _keyring():
    try:
        import keyring
        return keyring
    except ImportError:
        die("`keyring` not installed — the passphrase needs the OS keystore (`pip install keyring`).")


def get_pass(brain: str, required: bool = True) -> str | None:
    pw = _keyring().get_password(KEYRING_SERVICE.format(brain=brain), KEYRING_USER)
    if not pw and required:
        die(f"no gh_auth passphrase in the OS keystore for '{brain}' — run `gh_auth.py init` first.")
    return pw


def set_pass(brain: str, pw: str) -> None:
    _keyring().set_password(KEYRING_SERVICE.format(brain=brain), KEYRING_USER, pw)


def gen_pass() -> str:
    return secrets.token_urlsafe(32)


# --------------------------------------------------------------------------- #
# In-distro invocation (the secret crosses only as a /mnt path to a temp file)
# --------------------------------------------------------------------------- #
def _shred(path: Path) -> None:
    try:
        if path.is_file():
            with open(path, "r+b", buffering=0) as fh:
                fh.write(secrets.token_bytes(max(path.stat().st_size, 1)))
                fh.flush(); os.fsync(fh.fileno())
        path.unlink(missing_ok=True)
    except OSError:
        path.unlink(missing_ok=True)


def run_in_distro(args, cmd: str, pw: str, *, new_pw: str | None = None,
                  import_pw: str | None = None, material_bytes: bytes | None = None) -> int:
    """Run `gh_auth_gpg.sh <cmd>` in the brain distro. The passphrase and any cleartext
    material are DROPPED into the brain_etc/github/gh_auth/.transient seam dir (mode 0600),
    read in-distro via /opt/brain_truths, and SHREDDED on exit no matter what."""
    brain, bdir = brain_name(args), brain_dir(args)
    tdir = brain_dir(args).joinpath("brain_etc", *TRANSIENT_REL)
    tdir.mkdir(parents=True, exist_ok=True)
    dropped: list[Path] = []

    def drop(data: bytes, suffix: str) -> str:
        """Write `data` to a fresh 0600 file in the seam .transient dir; return its in-distro path."""
        fd, name = tempfile.mkstemp(dir=str(tdir), suffix=suffix)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        p = Path(name); dropped.append(p)
        try: os.chmod(p, 0o600)
        except (OSError, NotImplementedError): pass
        return f"{TRANSIENT_INDISTRO}/{p.name}"

    try:
        prefix = f'GH_AUTH_PASSPHRASE="$(cat {_q(drop(pw.encode(), ".pw"))})" '
        if new_pw is not None:
            prefix += f'GH_AUTH_NEW_PASSPHRASE="$(cat {_q(drop(new_pw.encode(), ".pw"))})" '
        if import_pw is not None:
            prefix += f'GH_AUTH_IMPORT_PASSPHRASE="$(cat {_q(drop(import_pw.encode(), ".pw"))})" '
        remote = f"{prefix}bash {_q(REMOTE_SCRIPT)} {cmd}"
        if material_bytes is not None:
            remote += f" < {_q(drop(material_bytes, '.mat'))}"

        if args.dry_run:
            info("[dry-run] in-distro command (reads secrets from the seam .transient drop):")
            info(f"          {remote}")
            return 0
        rab = [sys.executable, str(_rab(bdir)), "--brain", brain, "--wsl", "--", remote]
        return subprocess.run(rab, text=True).returncode
    finally:
        for p in dropped:                 # shred NO MATTER WHAT (success, failure, or exception)
            _shred(p)


def _q(s: str) -> str:
    """Single-quote for the POSIX remote shell."""
    return "'" + s.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_init(args) -> int:
    brain = brain_name(args)
    if get_pass(brain, required=False) and not args.force:
        die(f"a gh_auth passphrase already exists for '{brain}' (use --force to reinitialize).")
    pw = gen_pass()
    rc = run_in_distro(args, "init", pw)
    if rc == 0 and not args.dry_run:
        set_pass(brain, pw)         # only record the passphrase once the store is built
        info(f"passphrase stored in OS keystore (namespace '{KEYRING_SERVICE.format(brain=brain)}').")
    return rc


def cmd_status(args) -> int:
    return run_in_distro(args, "status", get_pass(brain_name(args)))


def cmd_import_ssh(args) -> int:
    if not args.file:
        die("import-ssh needs --file CLEARTEXT (a file of one or more SSH private keys).")
    material = Path(args.file).read_bytes()
    rc = run_in_distro(args, "import-ssh", get_pass(brain_name(args)), material_bytes=material)
    if rc == 0 and args.shred and not args.dry_run:
        _shred(Path(args.file)); info(f"shredded cleartext input {args.file}")
    return rc


def cmd_import_gpg(args) -> int:
    brain = brain_name(args)
    pw = get_pass(brain)
    if args.key_id:
        exp = subprocess.run(["gpg", "--export-secret-keys", "--armor", args.key_id],
                             capture_output=True)
        if exp.returncode != 0 or not exp.stdout:
            die(f"could not export key '{args.key_id}' from your gpg: "
                f"{exp.stderr.decode(errors='replace')[:200]}")
        material = exp.stdout
    elif args.file:
        material = Path(args.file).read_bytes()
    else:
        die("import-gpg needs --file CLEARTEXT or --key-id ID.")
    import_pw = None
    if args.import_passphrase_file:
        import_pw = Path(args.import_passphrase_file).read_text(encoding="utf-8").rstrip("\n")
    rc = run_in_distro(args, "import-gpg", pw, import_pw=import_pw, material_bytes=material)
    if rc == 0 and args.shred and args.file and not args.dry_run:
        _shred(Path(args.file)); info(f"shredded cleartext input {args.file}")
    return rc


def cmd_reset_password(args) -> int:
    brain = brain_name(args)
    if not args.force:
        die("reset-password re-seals the whole vault under a new passphrase. Re-run with --force.")
    old = get_pass(brain)
    new = gen_pass()
    rc = run_in_distro(args, "reset", old, new_pw=new)
    if rc == 0 and not args.dry_run:
        set_pass(brain, new)
        info("passphrase rotated in the OS keystore; vault re-sealed.")
    return rc


def cmd_recreate(args) -> int:
    brain = brain_name(args)
    if not args.force:
        die("recreate DESTROYS the vault (all GPG + SSH keys). Re-run with --force.")
    new = gen_pass()
    # recreate makes a fresh store under a new passphrase (the old keys are gone anyway).
    rc = run_in_distro(args, "recreate", new)
    if rc == 0 and not args.dry_run:
        set_pass(brain, new)
        info("vault recreated empty; new passphrase stored in the OS keystore.")
    return rc


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gh_auth", description="In-brain credential vault (orchestrator).")
    ap.add_argument("--brain")
    ap.add_argument("--brain-dir")
    ap.add_argument("--dry-run", action="store_true", help="print the resolved in-distro command; run nothing")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the store; onboarding-time passphrase").set_defaults(fn=cmd_init)
    sub.choices["init"].add_argument("--force", action="store_true")
    sub.add_parser("status", help="fingerprints / counts (no secrets)").set_defaults(fn=cmd_status)

    p = sub.add_parser("import-ssh", help="seal SSH private key(s) into the vault")
    p.add_argument("--file", help="cleartext file of one or more SSH private keys (BEGIN/END blocks)")
    p.add_argument("--shred", action="store_true", help="secure-delete the cleartext --file after import")
    p.set_defaults(fn=cmd_import_ssh)

    p = sub.add_parser("import-gpg", help="import GPG key(s) into the store")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--file", help="cleartext armored GPG key material")
    g.add_argument("--key-id", help="export this key from YOUR gpg and import it")
    p.add_argument("--import-passphrase-file",
                   help="file holding the imported key's own passphrase (recorded in the sealed sidecar)")
    p.add_argument("--shred", action="store_true")
    p.set_defaults(fn=cmd_import_gpg)

    p = sub.add_parser("reset-password", help="force a new store passphrase; re-seal the vault")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_reset_password)

    p = sub.add_parser("recreate", help="force-destroy the vault and make a fresh empty one")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_recreate)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
