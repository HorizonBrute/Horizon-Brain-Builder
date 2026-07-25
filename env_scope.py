#!/usr/bin/env python3
"""
env_scope.py — system-scope-first, user-scope-fallback environment variable
persistence (Horizon Brain Builder).

POLICY (owner requirement, verbatim): "creating a brain ... should create all
environment variables as System environment variables, if possible, and
fall-back to User ENV Vars if not. But I'm expecting this to be installed by
Owner / Admin." So: attempt system/machine scope first; fall back to user
scope only when elevation is genuinely unavailable; and make the fallback
LOUD, not silent — a user-scope write is a DEGRADED result and the operator
must be told so explicitly, every time, not just on first occurrence.

REFERENCE PATTERN: the Windows branch below generalizes what
deploy_brain.py's _persist_machine_token_env() already did correctly —
`[Environment]::SetEnvironmentVariable(name, value, 'Machine')` for the
brain's gateway bootstrap tokens (<BRAIN>_CHROMA_R / <BRAIN>_CHROMA_RW). This
module adds the User-scope fallback that function did not have, and a native
Linux equivalent (deploy_brain.py's Windows-only PowerShell call silently
no-ops on Linux hosts today).

NOTE ON REACHABILITY: deploy_brain.py's `deploy` verb calls require_admin()
before any stage runs (main(), ~line 4230), which DIES if the process is not
already elevated/root. So when THIS module is invoked from deploy_brain.py's
gateway stage, the "not elevated" branch below is unreachable in practice —
elevation is a precondition of the caller, not of us. The fallback path is
kept here anyway because (a) it is the honest, general-purpose implementation
of the stated policy, not a deploy_brain.py-specific hack, and (b) other,
future callers (this module is shared, not deploy_brain-private) may not
require_admin() first. Do not read the fallback branch's presence as a claim
that deploy_brain.py's current call site can hit it today.

This module intentionally has NO Horizon.AIOS-branded imports — Brain Builder
must remain buildable/runnable standalone, with no platform tree present.
"""

import os
import platform
import subprocess


def is_windows():
    return platform.system() == "Windows"


def is_elevated_windows():
    """Best-effort Windows admin check. Returns False (never raises) on any
    failure, including on non-Windows, so callers can call it unconditionally."""
    if not is_windows():
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_root_linux():
    return hasattr(os, "geteuid") and os.geteuid() == 0


# ---------------------------------------------------------------------------
# Windows: Machine scope first, User scope fallback (loud)
# ---------------------------------------------------------------------------

def set_windows_env_var(name, value, warn=print):
    """Persist a Windows environment variable at Machine (system) scope; if
    that fails (most commonly: not elevated), fall back to User scope and
    WARN LOUDLY that this is a degraded, per-user-only result.

    Returns (scope, ok): scope is 'machine', 'user', or None; ok is bool.
    """
    ps_machine = (
        "[Environment]::SetEnvironmentVariable("
        f"'{name}', $env:__ENV_SCOPE_VALUE, 'Machine')"
    )
    p = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_machine],
        capture_output=True, text=True,
        env=dict(os.environ, __ENV_SCOPE_VALUE=value),
    )
    if p.returncode == 0:
        return "machine", True

    machine_err = (p.stderr or p.stdout).strip()
    ps_user = (
        "[Environment]::SetEnvironmentVariable("
        f"'{name}', $env:__ENV_SCOPE_VALUE, 'User')"
    )
    p2 = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_user],
        capture_output=True, text=True,
        env=dict(os.environ, __ENV_SCOPE_VALUE=value),
    )
    if p2.returncode == 0:
        warn(f"  [WARN] Could not write {name} at Machine (system) scope — "
             f"probably not elevated: {machine_err}")
        warn(f"  [WARN] DEGRADED: {name} was written at USER scope only. It will "
             f"NOT be visible to other accounts, services, or scheduled tasks "
             f"running as a different identity. Re-run elevated (Administrator) "
             f"to fix this.")
        return "user", True

    warn(f"  [WARN] Could not persist {name} at Machine OR User scope: "
         f"{(p2.stderr or p2.stdout).strip()}")
    return None, False


# ---------------------------------------------------------------------------
# Linux: root -> /etc/environment (system scope); else invoking user's
# shell profile (loud fallback). Values are written fully expanded — the
# Linux system-scope file (/etc/environment, PAM-read) does NOT do $VAR
# interpolation, so unresolved references would be empty for every
# consumer that isn't also the process that defined them.
# ---------------------------------------------------------------------------

def set_linux_env_var(name, value, etc_environment="/etc/environment",
                       fallback_profile=None, warn=print):
    """Root -> upsert a fully-expanded KEY="value" line into /etc/environment
    (system scope; PAM-read, covers non-login + most non-interactive
    contexts). Non-root -> WARN LOUDLY and fall back to the invoking user's
    shell profile (degraded: visible to that user only).

    Returns (scope, ok): scope is 'system' or 'user'; ok is bool.
    """
    if is_root_linux():
        _upsert_etc_environment(name, value, etc_environment)
        return "system", True

    warn(f"  [WARN] Not root — cannot write {name} to {etc_environment} "
         f"(system scope).")
    warn(f"  [WARN] DEGRADED: falling back to the invoking user's shell "
         f"profile — {name} will be visible to this user only, not "
         f"system-wide. Re-run as root to fix this.")
    profile = fallback_profile or os.path.join(os.path.expanduser("~"), ".bashrc")
    _upsert_shell_profile(name, value, profile)
    return "user", True


def _upsert_etc_environment(name, value, path):
    """Idempotent upsert of a single KEY="value" line in an /etc/environment
    -style flat file. Rewrites the whole file so re-runs never duplicate."""
    marker = f"{name}="
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if not ln.startswith(marker)]
    lines.append(f'{name}="{value}"')
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _upsert_shell_profile(name, value, path):
    """Idempotent upsert of a single managed export block in a shell rc
    file. Only the sentinel-guarded block for THIS name is replaced; all
    other content is preserved untouched."""
    begin = f"# >>> horizon-env {name} >>>"
    end = f"# <<< horizon-env {name} <<<"
    content = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
    if begin in content and end in content:
        pre, _, rest = content.partition(begin)
        _, _, post = rest.partition(end)
        content = pre + post
    block = f'{begin}\nexport {name}="{value}"\n{end}\n'
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content + block)


# ---------------------------------------------------------------------------
# Single entry point
# ---------------------------------------------------------------------------

def set_system_env_var(name, value, **kwargs):
    """System-scope first, user-scope fallback, loud warning on degrade.
    Dispatches by platform. Returns (scope, ok)."""
    if is_windows():
        return set_windows_env_var(name, value, **{k: v for k, v in kwargs.items() if k == "warn"})
    return set_linux_env_var(name, value, **kwargs)
