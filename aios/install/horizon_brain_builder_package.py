#!/usr/bin/env python3
"""horizon_brain_builder_package.py — install / uninstall / update / status for the
Horizon Brain Builder package.

This is a SEPARATE package from the Horizon AIOS core. Unlike LAPP (which file-drops a skill), the
brain builder is a heavy CLI deployment toolchain, so `install` does NOT copy the toolchain anywhere:
it REGISTERS the package and injects a discovery-context pointer, and the toolchain runs IN PLACE from
the clone (deploy a brain by running the clone's windows_/linux_deploy_brain.py). This keeps a single
copy of the toolchain — the clone — as the one thing to update.

Cross-platform, standard-library only (Python 3.8+). Mirrors the horizon_aios_*.py tooling style but
uses the package-scoped horizon_brain_builder_* name (it is not part of the OS core).

Subcommands:
  install     Register the package + inject the discovery-context pointer (+ pull-only for a deployment).
  update      git-pull the deployment clone from upstream, then re-register (install --force).
  uninstall   Deregister + strip the context pointer. Leaves the clone AND any deployed brains intact.
  status      Print the registry and what is currently registered/deployed.

Source model: the DEVELOPMENT CANON is the push-enabled checkout (e.g. projects/horizon_brain_builder),
which publishes to the upstream. A DEPLOYMENT is a clone of that upstream under
$HORIZON_SYSTEM/deployed_packages/ that tracks it, pull-only. `update` is the deployment side of the
loop: canon -> upstream (push) -> deployment (pull).

Locations (resolved from env, overridable with --horizon-root):
  HORIZON_ROOT     AIOS root
  HORIZON_SYSTEM   <root>/horizon_system   (expected clone home: <system>/deployed_packages/)
  HORIZON_ETC      <system>/ai_os_etc      (registry lives here)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PACKAGE_NAME = "horizon_brain_builder"
# Canonical upstream for this package — where deployments pull updates from and clone by default.
DEFAULT_UPSTREAM = "https://github.com/HorizonBrute/Horizon-Brain-Builder"
REGISTRY_NAME = "horizon_deployed_packages.local.json"
REGISTRY_SCHEMA = "horizon_deployed_packages/v1"
CONTEXT_MARKER = "horizon-brain-builder"
BEGIN_MARKER = f"<!-- BEGIN {CONTEXT_MARKER}"
END_MARKER = f"<!-- END {CONTEXT_MARKER} -->"


# --------------------------------------------------------------------------- helpers
def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def die(msg: str) -> "None":
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def package_root() -> Path:
    # this file is <pkg>/aios/install/horizon_brain_builder_package.py
    return Path(__file__).resolve().parents[2]


def resolve_paths(horizon_root: "str | None") -> dict:
    root = horizon_root or os.environ.get("HORIZON_ROOT")
    if not root:
        die("HORIZON_ROOT is not set and --horizon-root was not supplied.")
    root_p = Path(root).expanduser().resolve()
    if not root_p.is_dir():
        die(f"HORIZON_ROOT does not exist: {root_p}")
    system = Path(os.environ.get("HORIZON_SYSTEM") or root_p / "horizon_system").resolve()
    etc = Path(os.environ.get("HORIZON_ETC") or system / "ai_os_etc").resolve()
    return {
        "root": root_p,
        "system": system,
        "etc": etc,
        "registry": etc / REGISTRY_NAME,
        "agents_file": root_p / "projects" / "agents.md",
    }


def rel_to_root(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def git_remotes(repo: Path) -> list:
    """Return [{name,url}] for the package clone, or [] if not a git repo / git absent."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "remote", "-v"],
            capture_output=True, text=True, check=False,
        )
    except (OSError, FileNotFoundError):
        return []
    seen, remotes = set(), []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in seen:
            seen.add(parts[0])
            remotes.append({"name": parts[0], "url": parts[1]})
    return remotes


def is_deployment_clone(pkg: Path, system: Path) -> bool:
    """True if this clone lives under $HORIZON_SYSTEM/deployed_packages/ (a DEPLOYMENT), as opposed
    to the development checkout (the factory canon, e.g. the projects/ repo)."""
    dp = (system / "deployed_packages").resolve()
    try:
        pkg.resolve().relative_to(dp)
        return True
    except ValueError:
        return False


def configure_pull_only(pkg: Path) -> str:
    """Make a deployment clone PULL-ONLY: it may fetch/pull from upstream but must never push. The
    developer publishes from the canon; a deployment is a read-only mirror. Implemented by pointing
    the push URL at a sentinel that fails fast with a clear message."""
    sentinel = "DISABLED-pull-only-deployment"
    remotes = git_remotes(pkg)
    if not remotes:
        return "no-remote"
    name = remotes[0]["name"]
    res = subprocess.run(
        ["git", "-C", str(pkg), "remote", "set-url", "--push", name, sentinel],
        capture_output=True, text=True, check=False,
    )
    return "pull-only" if res.returncode == 0 else f"failed:{res.stderr.strip()}"


def read_registry(registry: Path) -> dict:
    if registry.exists():
        try:
            data = json.loads(registry.read_text(encoding="utf-8"))
            data.setdefault("schema", REGISTRY_SCHEMA)
            data.setdefault("packages", [])
            return data
        except (json.JSONDecodeError, OSError) as exc:
            die(f"registry is present but unreadable ({exc}); fix or remove {registry}")
    return {"schema": REGISTRY_SCHEMA, "packages": []}


def write_registry(registry: Path, data: dict) -> None:
    data["updated_utc"] = now_utc()
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _package_version(pkg: Path) -> str:
    vf = pkg / "VERSION"
    return vf.read_text(encoding="utf-8").strip() if vf.exists() else "unknown"


# --------------------------------------------------------------------------- install
def cmd_install(args) -> None:
    p = resolve_paths(args.horizon_root)
    pkg = package_root()
    print(f"Installing '{PACKAGE_NAME}' (register + context; toolchain runs in place from {pkg})")

    # 1. Discovery-context pointer into projects/agents.md (idempotent, marker-delimited). The factory
    #    is a CLI toolchain — nothing is copied; the pointer tells agents where it lives + how to run it.
    agents = p["agents_file"]
    pointer_src = pkg / "aios" / "install" / "context_pointer.md"
    if not pointer_src.is_file():
        die(f"context pointer template missing: {pointer_src}")
    pointer = pointer_src.read_text(encoding="utf-8").replace(
        "[[CLONE_PATH]]", rel_to_root(pkg, p["root"]))
    if agents.exists():
        text = agents.read_text(encoding="utf-8")
        if BEGIN_MARKER not in text:
            with agents.open("a", encoding="utf-8", newline="\n") as fh:
                if not text.endswith("\n"):
                    fh.write("\n")
                fh.write("\n" + pointer.rstrip() + "\n")
            print("  - injected context pointer into projects/agents.md")
        else:
            # refresh the managed block in place (clone path may have changed)
            out, skip = [], False
            for ln in text.splitlines():
                if BEGIN_MARKER in ln:
                    skip = True
                    continue
                if skip:
                    if END_MARKER in ln:
                        skip = False
                    continue
                out.append(ln)
            while out and out[-1].strip() == "":
                out.pop()
            agents.write_text("\n".join(out) + "\n\n" + pointer.rstrip() + "\n", encoding="utf-8")
            print("  - refreshed context pointer in projects/agents.md")
    else:
        print("  ! projects/agents.md not found; skipped context pointer")

    # 2. A DEPLOYMENT clone is a pull-only mirror of canon — allow fetch/pull, forbid push. The
    #    development checkout (factory canon) is left push-enabled.
    deployment = is_deployment_clone(pkg, p["system"])
    pull_only = False
    if deployment:
        st = configure_pull_only(pkg)
        pull_only = st == "pull-only"
        print(f"  - deployment clone: push {'DISABLED (pull-only)' if pull_only else st}")
    else:
        print("  - development checkout (factory canon): push left enabled")

    # 3. Register in the machine-local deployed-packages registry. No skill_dir/payload copy — the
    #    only reversible artifact is the context block, so that is the whole payload manifest.
    data = read_registry(p["registry"])
    entry = {
        "name": PACKAGE_NAME,
        "version": _package_version(pkg),
        "clone_path": rel_to_root(pkg, p["root"]),
        "upstream": DEFAULT_UPSTREAM,
        "remotes": git_remotes(pkg) or [{"name": "origin", "url": DEFAULT_UPSTREAM + ".git"}],
        "role": "deployment" if deployment else "development-canon",
        "pull_only": pull_only,
        "sync": True,
        # Entrypoint the AIOS sync invokes to update this package: `python <clone>/<entrypoint> update`.
        "install_entrypoint": Path(__file__).resolve().relative_to(pkg).as_posix(),
        "installed_utc": now_utc(),
        "updated_utc": now_utc(),
        "payload": {
            "context_block_file": rel_to_root(agents, p["root"]),
            "context_block_marker": CONTEXT_MARKER,
        },
    }
    others = [pk for pk in data["packages"] if pk.get("name") != PACKAGE_NAME]
    prior = next((pk for pk in data["packages"] if pk.get("name") == PACKAGE_NAME), None)
    if prior and prior.get("installed_utc"):
        entry["installed_utc"] = prior["installed_utc"]
    data["packages"] = others + [entry]
    write_registry(p["registry"], data)
    print(f"  - registered in {p['registry'].name}"
          f" (clone_path={entry['clone_path']}, role={entry['role']})")

    print(f"Done. The builder is registered and discoverable. Deploy a brain by running the clone's\n"
          f"      windows_deploy_brain.py / linux_deploy_brain.py in place (see the clone's README).")
    if rel_to_root(pkg, p["root"]) == pkg.resolve().as_posix():
        print("  note: this package clone is OUTSIDE $HORIZON_ROOT; for sync coverage clone it to "
              f"$HORIZON_SYSTEM/deployed_packages/{PACKAGE_NAME}/ and re-run install.")


# --------------------------------------------------------------------------- uninstall
def cmd_uninstall(args) -> None:
    p = resolve_paths(args.horizon_root)
    print(f"Uninstalling '{PACKAGE_NAME}' (deregister + strip context; clone + brains untouched)")

    agents = p["agents_file"]
    if agents.exists():
        text = agents.read_text(encoding="utf-8")
        if BEGIN_MARKER in text:
            out, skip = [], False
            for ln in text.splitlines():
                if BEGIN_MARKER in ln:
                    skip = True
                    continue
                if skip:
                    if END_MARKER in ln:
                        skip = False
                    continue
                out.append(ln)
            while out and out[-1].strip() == "":
                out.pop()
            agents.write_text("\n".join(out) + "\n", encoding="utf-8")
            print("  - stripped context pointer from projects/agents.md")
        else:
            print("  - no context pointer block found (skipped)")

    if p["registry"].exists():
        data = read_registry(p["registry"])
        before = len(data["packages"])
        data["packages"] = [pk for pk in data["packages"] if pk.get("name") != PACKAGE_NAME]
        if len(data["packages"]) != before:
            write_registry(p["registry"], data)
            print(f"  - deregistered from {p['registry'].name}")
        else:
            print("  - not in registry (skipped)")

    print("Done. The package clone and every deployed brain are left untouched (self-contained).")


# --------------------------------------------------------------------------- update
def cmd_update(args) -> None:
    """Pull the deployment clone from its upstream, then re-register. The deployment side of the
    canon -> upstream -> deployment loop. Runs from a deployed clone (has a git remote)."""
    pkg = package_root()
    remotes = git_remotes(pkg)
    if not remotes:
        die(f"{pkg} has no git remote to pull from. This command runs on a DEPLOYMENT clone "
            f"(cloned from the upstream), not a detached copy.")
    print(f"Updating deployment at {pkg} (upstream authoritative - local changes overwritten)")
    up = subprocess.run(
        ["git", "-C", str(pkg), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        capture_output=True, text=True, check=False,
    )
    upstream_ref = up.stdout.strip() if up.returncode == 0 else f"{remotes[0]['name']}/HEAD"
    fetch = subprocess.run(["git", "-C", str(pkg), "fetch", "--prune"],
                           capture_output=True, text=True, check=False)
    if fetch.returncode != 0:
        die(f"git fetch failed: {fetch.stderr.strip()}")
    reset = subprocess.run(["git", "-C", str(pkg), "reset", "--hard", upstream_ref],
                           capture_output=True, text=True, check=False)
    out = (reset.stdout + reset.stderr).strip()
    if out:
        print("  " + out.replace("\n", "\n  "))
    if reset.returncode != 0:
        die(f"git reset to {upstream_ref} failed: {reset.stderr.strip()}")
    print("  - overwritten from upstream; re-registering ...")
    cmd_install(args)


# --------------------------------------------------------------------------- status
def cmd_status(args) -> None:
    p = resolve_paths(args.horizon_root)
    pkg = package_root()
    print(f"HORIZON_ROOT : {p['root']}")
    print(f"clone        : {pkg}  (version {_package_version(pkg)})")
    print(f"registry     : {p['registry']}"
          + ("" if p["registry"].exists() else "  (absent)"))
    if p["registry"].exists():
        data = read_registry(p["registry"])
        print(f"registry schema: {data.get('schema')}  updated: {data.get('updated_utc','?')}")
        mine = [pk for pk in data["packages"] if pk.get("name") == PACKAGE_NAME]
        if not mine:
            print(f"  ({PACKAGE_NAME} not registered)")
        for pk in mine:
            remotes = ", ".join(r.get("url", "?") for r in pk.get("remotes", [])) or "none"
            print(f"  - {pk['name']} v{pk.get('version','?')}  sync={pk.get('sync')}  "
                  f"role={pk.get('role','?')}  pull_only={pk.get('pull_only', '?')}")
            print(f"      clone   : {pk.get('clone_path')}")
            print(f"      upstream: {pk.get('upstream','(none)')}")
            print(f"      remotes : {remotes}")


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="Install/uninstall/update/status the Horizon Brain Builder package.")
    ap.add_argument("--horizon-root", help="AIOS root (default: $HORIZON_ROOT).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (
        ("install", cmd_install),
        ("update", cmd_update),
        ("uninstall", cmd_uninstall),
        ("status", cmd_status),
    ):
        sp = sub.add_parser(name)
        sp.add_argument("--horizon-root", help="AIOS root (default: $HORIZON_ROOT).")
        sp.add_argument("--force", action="store_true",
                        help="re-register / refresh an existing install in place.")
        sp.set_defaults(func=fn)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
