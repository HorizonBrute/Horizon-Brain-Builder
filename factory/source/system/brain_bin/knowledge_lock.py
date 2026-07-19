#!/usr/bin/env python3
"""
knowledge_lock.py - fast owner-write lock for a brain's knowledge area
======================================================================

Toggles whether the OWNER account (the human admin who provisioned the brain) can
WRITE into this brain's `knowledge/` tree. The brain account always owns and can
write it; the lock only fences the owner out (write), so the brain's knowledge
can't be tampered with outside deliberate, unlocked ingestion windows.

This is a control surface, not a wall against root/Administrator: an admin can
always take ownership back. It expresses and enforces intent, and prevents
accidental writes from the owner side.

Scope: `<brain>/knowledge/` (the brain's owned data domain). It does NOT touch
Chroma's live data - that lives in a named Docker volume inside WSL2, by design,
precisely so locking the folder can't break the running container.

Usage:
    python knowledge_lock.py             # status (read-only, no elevation)
    python knowledge_lock.py --status    # same
    python knowledge_lock.py --lock      # brain owns it; DENY owner write   (admin)
    python knowledge_lock.py --unlock    # remove the owner-write DENY        (admin)
    python knowledge_lock.py --help      # this help  (aliases: -h, -?, --?, /?)

Identity (brain account + owner account) is read from `../.brain_provision.json`.
"""

import ctypes
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

BRAIN_BIN = Path(__file__).resolve().parent
BRAIN_DIR = BRAIN_BIN.parent.parent
KNOWLEDGE = BRAIN_DIR / "knowledge"
PROVISION_JSON = BRAIN_DIR / ".brain_provision.json"
OS_NAME = platform.system()

HELP_FLAGS = {"-h", "--help", "-?", "--?", "/?", "help"}


def out(msg=""):
    print(msg)


def err(msg):
    print(f"  [ERROR] {msg}", file=sys.stderr)


def is_elevated():
    if OS_NAME == "Windows":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def require_elevation():
    if is_elevated():
        return
    if OS_NAME == "Windows":
        err("--lock/--unlock change ownership + ACLs; run from an Administrator terminal.")
    else:
        err("--lock/--unlock need root: sudo python3 knowledge_lock.py ...")
    sys.exit(1)


def load_identity():
    """Return (brain_name, owner_name) from the provisioning manifest.

    NEVER guess the owner. The lock is an icacls DENY keyed on this name, and the
    admin invoking --lock is a REAL principal: a wrong-but-real guess makes icacls
    exit 0, so the lock reports success while denying nobody who matters. Worse,
    _windows_is_locked() then greps for that same wrong name, finds the DENY it
    just wrote, and confirms the lie. rc-checks catch a TYPO'd principal and are
    structurally blind to this. Refusing to guess is the only defense.

    A missing or incomplete manifest is therefore a hard stop, not a default.
    """
    if not PROVISION_JSON.is_file():
        err(f"no provisioning manifest: {PROVISION_JSON}")
        err("the owner identity has no other source of truth; refusing to guess it.")
        sys.exit(1)
    try:
        data = json.loads(PROVISION_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        err(f"could not parse {PROVISION_JSON.name}: {exc}")
        sys.exit(1)
    owner = data.get("provisioned_by")
    if not owner:
        err(f"{PROVISION_JSON.name} has no 'provisioned_by'; refusing to guess the owner.")
        sys.exit(1)
    return data.get("brain_name") or BRAIN_DIR.name, owner


def ensure_knowledge():
    if not KNOWLEDGE.is_dir():
        err(f"knowledge area does not exist yet: {KNOWLEDGE}")
        err("create it (and its inbox) before locking. See docker_readme / project docs.")
        sys.exit(1)


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

def _windows_owner():
    p = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         f"(Get-Acl '{KNOWLEDGE}').Owner"],
        capture_output=True, text=True,
    )
    return p.stdout.strip()


def _windows_is_locked(owner_name):
    p = subprocess.run(["icacls", str(KNOWLEDGE)], capture_output=True, text=True)
    for line in p.stdout.splitlines():
        if owner_name.lower() in line.lower() and "DENY" in line.upper():
            return True
    return False


def cmd_status(brain_name, owner_name):
    ensure_knowledge()
    out(f"knowledge area : {KNOWLEDGE}")
    out(f"brain account  : {brain_name}")
    out(f"owner account  : {owner_name}")
    if OS_NAME == "Windows":
        out(f"ntfs owner     : {_windows_owner()}")
        locked = _windows_is_locked(owner_name)
    else:
        p = subprocess.run(["getfacl", "-p", str(KNOWLEDGE)],
                           capture_output=True, text=True)
        locked = any(
            line.startswith(f"user:{owner_name}:") and "w" not in line.split(":")[-1]
            for line in p.stdout.splitlines()
        )
    out(f"state          : {'LOCKED (owner write denied)' if locked else 'UNLOCKED (owner may write)'}")
    return 0


# --------------------------------------------------------------------------- #
# Lock / unlock
# --------------------------------------------------------------------------- #

def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def cmd_lock(brain_name, owner_name):
    ensure_knowledge()
    require_elevation()
    if OS_NAME == "Windows":
        # brain owns it + full control; explicit DENY write to the owner (an
        # explicit Deny overrides the owner's inherited Allow). (OI)(CI) so new
        # files/subdirs inherit the same posture.
        _run(["icacls", str(KNOWLEDGE), "/setowner", brain_name, "/T", "/C"])
        _run(["icacls", str(KNOWLEDGE), "/grant", f"{brain_name}:(OI)(CI)F", "/T", "/C"])
        r = _run(["icacls", str(KNOWLEDGE), "/deny", f"{owner_name}:(OI)(CI)(W)", "/T", "/C"])
        if r.returncode != 0:
            err(r.stderr.strip()); return 1
        # rc 0 is NOT proof: /C suppresses per-file errors. Read the ACL back.
        if not _windows_is_locked(owner_name):
            err(f"icacls reported success but no DENY for {owner_name} is present on "
                f"{KNOWLEDGE}. NOT locked — refusing to report otherwise.")
            return 1
    else:
        grp = f"{brain_name}_group"
        for cmd in (["chown", "-R", f"{brain_name}:{grp}", str(KNOWLEDGE)],
                    ["setfacl", "-R", "-m", f"u:{owner_name}:r-x", str(KNOWLEDGE)],
                    ["setfacl", "-R", "-d", "-m", f"u:{owner_name}:r-x", str(KNOWLEDGE)]):
            r = _run(cmd)
            if r.returncode != 0:
                err(f"{cmd[0]} failed: {r.stderr.strip()}"); return 1
    out(f"[OK] LOCKED. {brain_name} owns {KNOWLEDGE.name}/; {owner_name} write denied.")
    return cmd_status(brain_name, owner_name)


def cmd_unlock(brain_name, owner_name):
    ensure_knowledge()
    require_elevation()
    if OS_NAME == "Windows":
        # Drop the explicit DENY for the owner; inherited Allow returns.
        r = _run(["icacls", str(KNOWLEDGE), "/remove:d", owner_name, "/T", "/C"])
        if r.returncode != 0:
            err(r.stderr.strip()); return 1
        # Same reasoning as cmd_lock: verify the DENY is actually gone.
        if _windows_is_locked(owner_name):
            err(f"icacls reported success but a DENY for {owner_name} remains on "
                f"{KNOWLEDGE}. STILL locked — refusing to report otherwise.")
            return 1
    else:
        for cmd in (["setfacl", "-R", "-m", f"u:{owner_name}:rwx", str(KNOWLEDGE)],
                    ["setfacl", "-R", "-d", "-m", f"u:{owner_name}:rwx", str(KNOWLEDGE)]):
            r = _run(cmd)
            if r.returncode != 0:
                err(f"setfacl failed: {r.stderr.strip()}"); return 1
    out(f"[OK] UNLOCKED. {owner_name} may write {KNOWLEDGE.name}/ again "
        f"({brain_name} still owns it).")
    return cmd_status(brain_name, owner_name)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    argv = sys.argv[1:]
    if set(argv) & HELP_FLAGS:
        print(__doc__.strip()); return 0
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    brain_name, owner_name = load_identity()

    if "--lock" in argv:
        return cmd_lock(brain_name, owner_name)
    if "--unlock" in argv:
        return cmd_unlock(brain_name, owner_name)
    if not argv or "--status" in argv:
        return cmd_status(brain_name, owner_name)

    err(f"unknown option(s): {' '.join(argv)}")
    print(__doc__.strip())
    return 1


if __name__ == "__main__":
    sys.exit(main())
