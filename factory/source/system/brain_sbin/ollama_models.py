#!/usr/bin/env python3
"""
ollama_models.py - declarative, strict model-roster administration for the brain's
=================================================================================
sealed Ollama server.

Ollama has NO config file and NO concept of an "active model": it serves every
model in its store at once, and the caller picks one per request. So "administer
which models the brain has" means managing the *store* - and we do that the
same way we manage every other config surface: from ONE authoritative host file.

THE ROSTER (single source of truth)
    brain_etc/ollama/models   - one model name per line; '#' comments; blanks ok.
    A bare name means the ':latest' tag (e.g. `nomic-embed-text` == `:latest`).

This file is AUTHORITATIVE. `sync` makes the box match it exactly: it PULLS any
model listed-but-absent and (by default, strict) REMOVES any model present-but-
unlisted. An admin edits one file; the box converges. Adding a model = add a line.

It rides on run_as_brain (like brain_service.py): reads the live store via
`docker exec <brain>-ollama ollama list` and applies via `ollama pull` / `rm`,
all AS the brain uid inside the distro (rootless - never root).

USAGE
    ollama_models.py [--brain NAME] [--brain-dir DIR] <verb> [args]

  Verbs:
    list                 what is ACTUALLY on the box now
    roster               what the authoritative file SAYS should be there
    verify               diff roster vs box; exit 1 on drift (missing/extra)
    sync [--dry-run]     converge the box to the roster (strict: pulls + removes)
         [--no-remove]   additive only - pull missing, DON'T remove extras
    pull MODEL           pull one model directly
    rm   MODEL           remove one model directly

  Examples:
    ollama_models.py list
    ollama_models.py verify
    ollama_models.py sync --dry-run
    ollama_models.py sync
"""
import argparse
import subprocess
import sys
from pathlib import Path

import run_as_brain  # sibling in system/brain_sbin/

# Rootless Docker's per-user runtime dir (see provision/stage3_brain.sh) - prefixed
# so `docker` finds the rootless socket under a non-login WSL shell.
XDG = "export XDG_RUNTIME_DIR=/run/user/$(id -u)"


def brain_dir(args):
    """The brain workspace folder (holds brain_etc/, system/brain_sbin/). Defaults to this
    script's parent-of-parent so the tool travels with the brain."""
    return Path(args.brain_dir) if args.brain_dir else Path(__file__).resolve().parent.parent.parent


def roster_path(args):
    return brain_dir(args) / "brain_etc" / "ollama" / "models"


def container(brain):
    return f"{brain}-ollama"


def _norm(name):
    """Normalize a model reference for comparison: bare name -> name:latest."""
    name = name.strip()
    return name if ":" in name else f"{name}:latest"


# --------------------------------------------------------------------------- #
# reads (capture) vs applies (stream) - mirrors brain_truths / brain_service.
# --------------------------------------------------------------------------- #
def _rab(brain_dir_):
    return Path(brain_dir_) / "system" / "brain_sbin" / "run_as_brain.py"


def _capture(brain, bdir, shell_cmd):
    """Run a shell command in the distro AS the brain and capture stdout."""
    p = subprocess.run(
        [sys.executable, str(_rab(bdir)), "--brain", brain, "--wsl", "--",
         "bash", "-lc", f"{XDG}; {shell_cmd}"],
        capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        raise SystemExit(f"  [ERROR] in-distro command failed (rc={p.returncode})")
    return p.stdout


def _apply(brain, argv_cmd, dry_run=False):
    """Run a shell command in the distro AS the brain, STREAMING output (progress)."""
    return run_as_brain.run(brain, [f"{XDG}; {argv_cmd}"], target="runtime", dry_run=dry_run)


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
def read_roster(args):
    """Desired-state model set from the authoritative file (normalized)."""
    path = roster_path(args)
    if not path.exists():
        raise SystemExit(f"  [ERROR] no roster file: {path}\n"
                         f"          create it (one model per line) - it is authoritative.")
    models = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            models.append(_norm(line))
    return set(models)


def read_actual(brain, args):
    """What is actually in the store now (normalized), via `ollama list`."""
    out = _capture(brain, brain_dir(args), f"docker exec {container(brain)} ollama list")
    actual = set()
    for line in out.splitlines():
        line = line.strip()
        if not line or line.upper().startswith("NAME"):
            continue
        actual.add(_norm(line.split()[0]))
    return actual


# --------------------------------------------------------------------------- #
# verbs
# --------------------------------------------------------------------------- #
def cmd_list(brain, args):
    sys.stdout.write(_capture(brain, brain_dir(args),
                              f"docker exec {container(brain)} ollama list"))
    return 0


def cmd_roster(brain, args):
    for m in sorted(read_roster(args)):
        print(m)
    return 0


def _diff(brain, args):
    desired, actual = read_roster(args), read_actual(brain, args)
    return desired, actual, sorted(desired - actual), sorted(actual - desired)


def cmd_verify(brain, args):
    desired, actual, missing, extra = _diff(brain, args)
    print(f"roster : {len(desired)} model(s) - {', '.join(sorted(desired)) or '(none)'}")
    print(f"on box : {len(actual)} model(s) - {', '.join(sorted(actual)) or '(none)'}")
    if missing:
        print(f"MISSING (in roster, not on box): {', '.join(missing)}")
    if extra:
        print(f"EXTRA   (on box, not in roster): {', '.join(extra)}")
    if not missing and not extra:
        print("IN SYNC - box matches the roster.")
        return 0
    return 1


def cmd_sync(brain, args):
    desired, actual, missing, extra = _diff(brain, args)
    if not missing and (not extra or args.no_remove):
        print("IN SYNC - nothing to do." if not missing else "additive: nothing to pull.")
        if extra and args.no_remove:
            print(f"(leaving {len(extra)} unlisted model(s): {', '.join(extra)})")
        return 0
    rc = 0
    for m in missing:
        print(f"[pull] {m}")
        rc |= _apply(brain, f"docker exec {container(brain)} ollama pull {m}", args.dry_run)
    if not args.no_remove:
        for m in extra:
            print(f"[rm]   {m}  (unlisted - strict roster)")
            rc |= _apply(brain, f"docker exec {container(brain)} ollama rm {m}", args.dry_run)
    return rc


def cmd_pull(brain, args):
    return _apply(brain, f"docker exec {container(brain)} ollama pull {args.model}", args.dry_run)


def cmd_rm(brain, args):
    return _apply(brain, f"docker exec {container(brain)} ollama rm {args.model}", args.dry_run)


def main():
    ap = argparse.ArgumentParser(
        prog="ollama_models",
        description="Declarative strict model-roster administration for the brain's Ollama store.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brain", help="brain name (default: from .brain_provision.json or folder)")
    ap.add_argument("--brain-dir", help="brain workspace dir (default: this tool's brain)")
    ap.add_argument("--dry-run", action="store_true", help="print actions without executing")
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("list", help="models actually on the box")
    sub.add_parser("roster", help="models the authoritative file lists")
    sub.add_parser("verify", help="diff roster vs box (exit 1 on drift)")
    p_sync = sub.add_parser("sync", help="converge box to roster (strict: pull + remove)")
    p_sync.add_argument("--no-remove", action="store_true",
                        help="additive only - pull missing, keep unlisted extras")
    p_pull = sub.add_parser("pull", help="pull one model"); p_pull.add_argument("model")
    p_rm = sub.add_parser("rm", help="remove one model"); p_rm.add_argument("model")
    args = ap.parse_args()

    brain = run_as_brain.brain_name(args)
    verbs = {"list": cmd_list, "roster": cmd_roster, "verify": cmd_verify,
             "sync": cmd_sync, "pull": cmd_pull, "rm": cmd_rm}
    sys.exit(verbs[args.verb](brain, args))


if __name__ == "__main__":
    main()
