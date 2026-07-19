#!/usr/bin/env python3
"""
onboard.py - brain onboarding (Chroma stack)   [DEPRECATED]
=========================================================

****************************************************************************
* DEPRECATED — NOT part of the current onboarding path (Docker-Desktop era).*
*                                                                          *
* This script predates the rootless-Docker-in-WSL2 + ADR-0015 model. It    *
* assumes Docker Desktop, the `docker-users` group, and a `chroma/.env.example`
* template that no longer ships — none of which the current engine uses.    *
*                                                                          *
* USE THE ORCHESTRATOR INSTEAD:                                            *
*     python windows_deploy_brain.py deploy --brain <name>   (Windows)     *
*     python linux_deploy_brain.py   deploy --brain <name>   (Linux)       *
* which drives: create-brain -> stage package -> engine -> installer_1/2 -> *
* residency -> brain-truths seam -> gateway/token -> reapply (ADR-0015     *
* path-router stack) -> per-route verify.                                  *
*                                                                          *
* Kept only as a host-prep breadcrumb (do NOT run in the live path). See   *
* system/brain_bin/DEPLOYMENT.md.                                                 *
****************************************************************************

Prepares a host so that THIS brain's account can drive its own isolated,
Dockerized ChromaDB stack. Designed to ship inside the brain folder and be run
ONCE by an administrator after the folder is dropped onto a target machine.

What it does (idempotent - safe to re-run):
  1. Verifies Docker is present (presumes it is installed; warns if not).
  2. Generates `chroma/.env` from `chroma/.env.example`, customized for this brain.
  3. Grants the brain account access to the Docker engine
       - Windows : adds the account to the `docker-users` local group
       - Linux   : adds the account to the `docker` group
       - macOS   : no group model (Docker Desktop) - reports and skips
  4. Sets filesystem ownership/ACLs so the brain account controls `brain_bin`.
  5. Prints the commands the brain account uses to bring the stack up + verify.

Cross-platform: Windows / Linux / macOS. Python-only orchestration; shells out
to the native tool for each privileged action so the action is transparent.

Usage:
    python onboard.py            # APPLY: perform the onboarding (needs admin/root)
    python onboard.py --dryrun   # show every command that WOULD run; change nothing
    python onboard.py --help     # this help   (aliases: -h, -?, --?, /?)

Brain identity is read from `../.brain_provision.json` (authoritative), so the
script needs no editing when copied to a different brain folder.
"""

import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths & brain identity
# --------------------------------------------------------------------------- #

BRAIN_BIN = Path(__file__).resolve().parent          # .../<brain>/brain_bin
BRAIN_DIR = BRAIN_BIN.parent.parent                          # .../<brain>
CHROMA_DIR = BRAIN_BIN / "chroma"
ENV_EXAMPLE = CHROMA_DIR / ".env.example"
ENV_FILE = CHROMA_DIR / ".env"
PROVISION_JSON = BRAIN_DIR / ".brain_provision.json"

OS_NAME = platform.system()   # 'Windows' | 'Linux' | 'Darwin'


# --------------------------------------------------------------------------- #
# Small output helpers
# --------------------------------------------------------------------------- #

def info(msg: str) -> None:
    print(f"  {msg}")


def step(n: int, msg: str) -> None:
    print(f"\n[{n}] {msg}")


def ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def err(msg: str) -> None:
    print(f"  [ERROR] {msg}", file=sys.stderr)


def show_cmd(cmd) -> None:
    rendered = cmd if isinstance(cmd, str) else " ".join(cmd)
    print(f"      $ {rendered}")


# --------------------------------------------------------------------------- #
# Help / argument handling
# --------------------------------------------------------------------------- #

HELP_FLAGS = {"-h", "--help", "-?", "--?", "/?", "help"}
DRYRUN_FLAGS = {"--dryrun", "--dry-run", "-n"}


def print_help() -> None:
    print(__doc__.strip())


def parse_args(argv):
    """Return ('help'|'dryrun'|'apply'). Unknown flags -> help with a note."""
    args = set(argv)
    if args & HELP_FLAGS:
        return "help"
    if args & DRYRUN_FLAGS:
        return "dryrun"
    unknown = [a for a in argv if a.startswith("-")]
    if unknown:
        err(f"Unknown option(s): {' '.join(unknown)}")
        return "help"
    return "apply"


# --------------------------------------------------------------------------- #
# Privilege check (apply mode only)
# --------------------------------------------------------------------------- #

def is_elevated() -> bool:
    if OS_NAME == "Windows":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def require_elevation() -> None:
    if is_elevated():
        return
    if OS_NAME == "Windows":
        err("Run this from an Administrator terminal (right-click -> Run as administrator).")
    else:
        err("Run with root: sudo python3 onboard.py")
    err("Or inspect first without changing anything: python onboard.py --dryrun")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Command runner (honors apply vs dryrun)
# --------------------------------------------------------------------------- #

def run(cmd, apply: bool, *, capture=False):
    """In dryrun, print the command and return None. In apply, run it.

    Returns CompletedProcess on apply, or None on dryrun.
    """
    if not apply:
        show_cmd(cmd)
        return None
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
    )


def ps(script: str) -> list:
    """Wrap a PowerShell one-liner as an argv list."""
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]


# --------------------------------------------------------------------------- #
# Brain identity
# --------------------------------------------------------------------------- #

def load_brain():
    """Return (brain_name, brain_group). Falls back to folder name + <name>_group."""
    brain_name = BRAIN_DIR.name
    groups = []
    if PROVISION_JSON.is_file():
        try:
            data = json.loads(PROVISION_JSON.read_text(encoding="utf-8"))
            brain_name = data.get("brain_name", brain_name)
            groups = data.get("groups", [])
        except Exception as exc:
            warn(f"Could not parse {PROVISION_JSON.name}: {exc}. Using folder name.")
    brain_group = next(
        (g for g in groups if g != "brains"),
        f"{brain_name}_group",
    )
    return brain_name, brain_group


# --------------------------------------------------------------------------- #
# Step 1 - Docker presence
# --------------------------------------------------------------------------- #

def check_docker() -> None:
    step(1, "Verify Docker is available")
    docker = shutil.which("docker")
    if not docker and OS_NAME == "Windows":
        fallback = Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
        if fallback.is_file():
            docker = str(fallback)
    if not docker:
        warn("`docker` not found on PATH. This script presumes Docker is installed.")
        warn("Install Docker, ensure the engine is running, then re-run.")
        return
    ok(f"docker found: {docker}")
    proc = subprocess.run([docker, "version", "--format", "{{.Server.Version}}"],
                          capture_output=True, text=True)
    if proc.returncode == 0 and proc.stdout.strip():
        ok(f"engine reachable (server {proc.stdout.strip()})")
    else:
        warn("docker is installed but the engine is not responding "
             "(is Docker Desktop running?). The brain can still be onboarded; "
             "the engine just needs to be up before `compose up`.")


# --------------------------------------------------------------------------- #
# Step 2 - .env generation
# --------------------------------------------------------------------------- #

def generate_env(brain_name: str, apply: bool) -> None:
    step(2, "Generate chroma/.env from template")
    if not ENV_EXAMPLE.is_file():
        err(f"Template missing: {ENV_EXAMPLE}")
        return
    if ENV_FILE.is_file():
        ok(f"{ENV_FILE.name} already exists - leaving as-is (delete it to regenerate).")
        return
    content = ENV_EXAMPLE.read_text(encoding="utf-8").replace("__BRAIN_NAME__", brain_name)
    if not apply:
        info(f"Would write {ENV_FILE} with BRAIN_NAME/COMPOSE_PROJECT_NAME = {brain_name}")
        return
    ENV_FILE.write_text(content, encoding="utf-8")
    ok(f"wrote {ENV_FILE}")


# --------------------------------------------------------------------------- #
# Step 3 - Docker engine access for the brain account
# --------------------------------------------------------------------------- #

def grant_docker_access(brain_name: str, apply: bool) -> None:
    step(3, "Grant the brain account access to the Docker engine")

    if OS_NAME == "Windows":
        # Emit a real boolean: `-match` on an array returns the matching element,
        # not $true, so coerce membership existence to [bool] explicitly.
        check = subprocess.run(
            ps(f"[bool](Get-LocalGroupMember -Group 'docker-users' -ErrorAction "
               f"SilentlyContinue | Where-Object {{ $_.Name -like '*\\{brain_name}' "
               f"-or $_.Name -eq '{brain_name}' }})"),
            capture_output=True, text=True,
        )
        already = "True" in check.stdout
        if already:
            ok(f"{brain_name} already in 'docker-users' - skipping.")
            return
        result = run(ps(f"Add-LocalGroupMember -Group 'docker-users' -Member '{brain_name}'"),
                     apply, capture=True)
        if apply:
            if result.returncode == 0:
                ok(f"added {brain_name} to 'docker-users'.")
                warn("The brain account must log off/on (or restart Docker Desktop) "
                     "for the new group membership to take effect.")
            else:
                err(f"could not add {brain_name} to 'docker-users': "
                    f"{(result.stderr or '').strip()}")

    elif OS_NAME == "Linux":
        check = subprocess.run(["id", "-nG", brain_name], capture_output=True, text=True)
        if "docker" in check.stdout.split():
            ok(f"{brain_name} already in 'docker' group - skipping.")
            return
        run(["usermod", "-aG", "docker", brain_name], apply)
        if apply:
            ok(f"added {brain_name} to 'docker' group (re-login required to take effect).")
        info("Linux note: the stronger isolation target is ROOTLESS docker owned by "
             "the brain account. Group membership here matches the shared-engine model.")

    else:  # Darwin / macOS
        info("macOS uses Docker Desktop, which has no 'docker' group - engine access "
             "is per logged-in user. No membership change needed.")
        info(f"Ensure the '{brain_name}' account can launch Docker Desktop.")


# --------------------------------------------------------------------------- #
# Step 4 - Filesystem ownership / ACLs on brain_bin
# --------------------------------------------------------------------------- #

def set_ownership(brain_name: str, brain_group: str, apply: bool) -> None:
    step(4, "Give the brain account control of its brain_bin")

    if OS_NAME == "Windows":
        # (OI)(CI) = inherit to files+subdirs; M = Modify. Idempotent to re-grant.
        run(["icacls", str(BRAIN_BIN), "/grant", f"{brain_name}:(OI)(CI)M", "/T", "/C"], apply)
        if apply:
            ok(f"granted {brain_name} Modify on {BRAIN_BIN} (inherited).")
    else:
        run(["chown", "-R", f"{brain_name}:{brain_group}", str(BRAIN_BIN)], apply)
        run(["chmod", "-R", "u+rwX,g+rwX", str(BRAIN_BIN)], apply)
        if apply:
            ok(f"set {brain_name}:{brain_group} ownership on {BRAIN_BIN}.")


# --------------------------------------------------------------------------- #
# Next steps
# --------------------------------------------------------------------------- #

def print_next_steps(brain_name: str) -> None:
    print("\n" + "=" * 64)
    print("Onboarding complete. Hand off to the brain account:")
    print("=" * 64)
    print(f"""
  As the '{brain_name}' account, with Docker Desktop / engine running:

      cd "{CHROMA_DIR}"
      docker compose up -d

  Verify the vector store answers:

      curl http://127.0.0.1:8000/api/v2/heartbeat
      -> {{"nanosecond heartbeat": <number>}}

  Stop it (data is kept in knowledge/chroma_store): docker compose down
  Wipe it (DESTROYS the vector data):           docker compose down -v
""")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    mode = parse_args(sys.argv[1:])
    if mode == "help":
        print_help()
        return 0

    # DEPRECATED: this is not the current onboarding path. Refuse to APPLY unless the
    # caller explicitly opts in with --force-deprecated, so nobody runs the Docker-Desktop
    # -era host prep by accident. Point them at the orchestrator. (--dryrun still works so
    # the historical behavior remains inspectable.)
    if mode == "apply" and "--force-deprecated" not in set(sys.argv[1:]):
        print("=" * 64)
        print("onboard.py is DEPRECATED and NOT part of the current onboarding path.")
        print("Use the orchestrator instead:")
        print("    python windows_deploy_brain.py deploy --brain <name>   (Windows)")
        print("    python linux_deploy_brain.py   deploy --brain <name>   (Linux)")
        print("Re-run with --dryrun to inspect the old host-prep, or --force-deprecated")
        print("to run it anyway (Docker-Desktop era; expects a chroma/.env.example that")
        print("no longer ships). See system/brain_bin/DEPLOYMENT.md.")
        print("=" * 64)
        return 2

    apply = mode == "apply"
    # Line-buffer our stdout so our prints interleave in the right order with the
    # immediate output of the native tools we shell out to (icacls, etc.) when
    # this run is piped/logged rather than attached to a tty.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    brain_name, brain_group = load_brain()

    print("=" * 64)
    print(f"brain onboarding - {brain_name}")
    print(f"  brain dir : {BRAIN_DIR}")
    print(f"  platform  : {OS_NAME}")
    print(f"  mode      : {'APPLY' if apply else 'DRYRUN (no changes)'}")
    print("=" * 64)

    if apply:
        require_elevation()

    check_docker()
    generate_env(brain_name, apply)
    grant_docker_access(brain_name, apply)
    set_ownership(brain_name, brain_group, apply)

    if apply:
        print_next_steps(brain_name)
    else:
        print("\n[dryrun] No changes were made. Re-run without --dryrun to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
