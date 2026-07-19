#!/usr/bin/env python3
"""
run_as_brain.py — run a command AS the brain (across host + brain runtime env).
================================================================================

Brain-owned privileged tooling. Lives in the brain's ``system/brain_sbin/`` and travels
with the brain — no dependency on the host platform beyond an *optional* keystore
lookup (soft-detected; falls back to a prompt when no keystore entry is present).

WHY THIS EXISTS
---------------
A brain runs as its own least-privileged OS account, and its container runtime
(rootless Docker in a per-user WSL2 distro on Windows) is deliberately invisible
to the human owner. To operate the brain — deploy code, drive its services, run
distro-level system ops — an elevated operator needs a reliable way to *become
the brain* and run a command, without hand-crafting ``runas`` / ``Start-Process``
/ ``sudo`` incantations every time and getting the quoting or the identity wrong.

THE THREE IDENTITIES (keep them distinct)
-----------------------------------------
  (a) the elevated host operator            — whoever runs THIS tool (admin / root)
  (b) the brain's host OS account           — run_as_brain's DEFAULT target ("account")
  (c) root INSIDE the brain's Linux runtime — reached with --root ("runtime" + root)

TWO TARGET LOCATIONS
--------------------
  account  (default)  run as the brain's host OS account.
  runtime  (--wsl)    run inside the brain's Linux runtime environment.
                      --root implies runtime (root only exists in the runtime).

  On Windows these are two distinct places: the brain's Windows account vs. its
  per-user WSL distro ``brain-<brain>``. Because that distro is per-user, only the
  brain's Windows logon can launch it — so even the runtime path first becomes the
  brain's Windows account (Start-Process -Credential), then runs ``wsl -d ...``.

  On Linux the two collapse: the brain uid lives on the host and Docker is rootless
  native, so both are ``sudo -u <brain>`` (root = ``sudo``). ``--wsl`` is accepted
  as the platform-neutral "runtime" spelling and is a no-op distinction there.

  On macOS the runtime is a Lima VM (the WSL analogue that hosts Docker); account =
  ``sudo -u <brain>`` on the host, runtime = ``limactl shell <vm>``.

PLATFORM COVERAGE / TEST STATUS
-------------------------------
  Windows : implemented AND exercised on the dev host.
  Linux   : implemented (sudo -u / sudo); NOT exercised on the Windows dev host.
  macOS   : implemented (sudo -u / limactl); NOT exercised on the Windows dev host.
  Use --dry-run on any platform to print the resolved command without executing.

USAGE
-----
    run_as_brain.py [--brain NAME] [--wsl] [--root] [-i] [--dry-run] -- CMD [ARGS...]
    run_as_brain.py [--brain NAME] [--wsl] [--root] [-i] [--dry-run] --script FILE

  Examples (Windows):
    run_as_brain.py --wsl -- docker ps                 # brain-uid docker, in the distro
    run_as_brain.py --root -- apt-get update           # root in the distro (system ops)
    run_as_brain.py --wsl -i                           # interactive brain-uid shell
    run_as_brain.py -i                                 # interactive PowerShell as the brain
    run_as_brain.py --dry-run --root -- systemctl status docker

PAYLOAD CONTRACT (`--` vs `--script`)
-------------------------------------
  `-- CMD [ARGS...]` is ssh-style: argv is space-joined into ONE shell string for
  `bash -lc` (quoting a spaced arg is the caller's job, exactly like `ssh host "…"`).
  If you hand it an already-`bash -lc <string>` argv, it is UNWRAPPED, not re-wrapped.
  Use `--` ONLY for simple command lists — `&&`-chains and `;`-lists are robust.

  For non-trivial shell — nested `$(...)`, `case`/`for`, heredocs — use `--script FILE`.
  Inline complex logic can be mangled crossing the PowerShell -> bridge -> bash parsers;
  `--script` (a `shlex`-quoted single token, host path -> distro `/mnt` view) is the
  supported, quote-safe path. This is the ratified contract, not a temporary workaround.
"""
import argparse
import getpass
import json
import os
import platform
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # <brain>/brain_sbin
BRAIN_DIR = HERE.parent.parent                          # <brain>
PROVISION = BRAIN_DIR / ".brain_provision.json"

# Credential namespaces in the ONE OS-native keystore (Windows Credential Manager /
# macOS Keychain / Linux Secret Service, via the `keyring` lib). There is no separate
# "brain keystore" — just namespaces in the same vault:
#   brain-owned  : service 'brain:<brain>',  user 'account_password'
#                  (a brain that provisioned its own credential) — always tried.
#   host platform: whatever namespace the host's own provisioner wrote, named by
#                  $BRAIN_KEYRING_SERVICE (+ optional $BRAIN_KEYRING_USER, which may
#                  contain '{brain}'). Tried FIRST when set. This is the 'use the host's
#                  vault if it has one' seam: the host names its own namespace, so this
#                  tool carries no knowledge of any particular platform's convention.
# Then prompt. NOTE: on Windows the vault is per-user, so this must run as the SAME
# operator account that stored it (the elevated operator), then become the brain —
# never the other way round.
def _keyring_namespaces():
    host_service = os.environ.get("BRAIN_KEYRING_SERVICE")
    ns = []
    if host_service:
        ns.append((host_service, os.environ.get("BRAIN_KEYRING_USER", "brain_account:{brain}")))
    ns.append(("brain:{brain}", "account_password"))    # brain-owned namespace
    return tuple(ns)

def info(m): print(f"  {m}")
def die(m): print(f"  [ERROR] {m}", file=sys.stderr); sys.exit(1)


# ---------------------------------------------------------------------------
# Identity / target resolution
# ---------------------------------------------------------------------------

def brain_name(args):
    if args.brain:
        return args.brain
    if PROVISION.is_file():
        try:
            return json.loads(PROVISION.read_text(encoding="utf-8"))["brain_name"]
        except Exception:
            pass
    return BRAIN_DIR.name


def distro_name(brain):
    return f"brain-{brain}"         # matches brain_installer_2_brain.py's wsl --import


def lima_vm(brain):
    return f"brain-{brain}"         # macOS Lima VM naming (analogue of the WSL distro)


def to_wsl_path(p):
    """Translate a Windows path to its WSL /mnt/<drive> view (C:\\a\\b -> /mnt/c/a/b).
    Used so `--script <host path>` can run inside the distro. No-op-ish for a path
    that has no drive letter (already POSIX)."""
    p = Path(p)
    if p.drive:                                     # 'C:'
        drive = p.drive.rstrip(":").lower()
        rest = p.as_posix()[len(p.drive):].lstrip("/")
        return f"/mnt/{drive}/{rest}"
    return p.as_posix()


# ---------------------------------------------------------------------------
# Credential (Windows only; soft keystore -> prompt)
# ---------------------------------------------------------------------------

def get_password(brain):
    """Brain Windows password for Start-Process -Credential. Read the OS-native
    keystore directly (the host-named namespace first when $BRAIN_KEYRING_SERVICE is
    set, then the brain-owned namespace), else prompt. This is the 'use the host's
    vault if there, but don't require it' seam — it depends only on the universal
    `keyring` lib plus an env-named namespace, so it travels with the brain (see
    _keyring_namespaces)."""
    try:
        import keyring
    except ImportError:
        info("credential: `keyring` not installed — prompting")
        return getpass.getpass(f"  Windows password for '{brain}': ")

    for service_tmpl, user_tmpl in _keyring_namespaces():
        service = service_tmpl.format(brain=brain)
        try:
            pw = keyring.get_password(service, user_tmpl.format(brain=brain))
        except Exception as e:
            info(f"credential: keystore read failed for '{service}' ({e})")
            pw = None
        if pw:
            info(f"credential: retrieved from OS keystore (namespace '{service}')")
            return pw

    info("credential: not found in OS keystore — prompting")
    return getpass.getpass(f"  Windows password for '{brain}': ")


# ---------------------------------------------------------------------------
# PowerShell rendering helpers (Windows)
# ---------------------------------------------------------------------------

def _ps_squote(s):
    """Single-quote a string for PowerShell (literal; '' escapes a quote)."""
    return "'" + str(s).replace("'", "''") + "'"


def _ps_array(tokens):
    """Render a PowerShell array literal @('a','b',...) from a token list."""
    return "@(" + ",".join(_ps_squote(t) for t in tokens) + ")"


# ---------------------------------------------------------------------------
# Command construction — returns the *inner* command (exe, args) that will run
# UNDER the brain's identity, per platform + target.
# ---------------------------------------------------------------------------

def build_inner(brain, target, as_root, interactive, argv, system):
    """Return (exe, args_list, note) for the command that runs as the brain.

    On Windows this is what Start-Process -Credential launches. On Unix it is the
    argv handed to sudo/limactl. `note` is a human description for --dry-run.
    """
    if system == "Windows":
        if target == "runtime":
            distro = distro_name(brain)
            wsl = ["wsl", "-d", distro] + (["-u", "root"] if as_root else [])
            if interactive:
                return wsl[0], wsl[1:], f"interactive shell in {distro}" + (" as root" if as_root else "")
            # ssh-style contract: the payload is ONE shell command line handed to a
            # single `bash -lc`, and bare argv space-joins into it. So `-- docker ps`
            # and `-- "docker ps; ls"` (one token) both work — quoting of any spaced
            # arg is the caller's job, exactly as with ssh. (Do NOT shlex.join a bare
            # argv here: it would quote a compound token and `bash -lc` would treat it
            # as a literal command name.) `bash -lc` keeps the login-shell env rootless
            # Docker needs (PATH / DOCKER_HOST / XDG_RUNTIME_DIR).
            #
            # BUT: if the caller already handed us `bash -lc <string> [args…]` (the shape
            # people reach for out of ssh habit), take that <string> verbatim and do NOT
            # re-wrap. Blindly wrapping produced `bash -lc "bash -lc <string>"`, and the
            # naive space-join destroyed the inner argument boundaries — decaying to a
            # bare, argument-less `bash` that blocks on stdin under -Wait (the GATE-B0
            # hang). We build exactly ONE well-formed `bash -lc` either way.
            if (len(argv) >= 3 and argv[0] == "bash"
                    and argv[1].startswith("-") and "c" in argv[1]):
                payload, positional = argv[2], list(argv[3:])   # already bash -lc: unwrap
            else:
                payload, positional = " ".join(argv), []        # ssh-style shell string
            return wsl[0], wsl[1:] + ["--", "bash", "-lc", payload] + positional, \
                f"in {distro}" + (" as root" if as_root else "")
        # account
        if interactive:
            return "powershell", ["-NoExit"], "interactive PowerShell as the brain (new console)"
        return argv[0], argv[1:], "as the brain Windows account"

    # ---- Unix (Linux / macOS) ----
    if system == "Darwin" and target == "runtime":
        vm = lima_vm(brain)
        base = ["limactl", "shell", vm]
        if as_root:
            base += ["sudo"]
        if interactive:
            # limactl shell with no command drops into an interactive VM shell.
            return base[0], base[1:], f"interactive shell in Lima VM {vm}" + (" as root" if as_root else "")
        return base[0], base[1:] + list(argv), f"in Lima VM {vm}" + (" as root" if as_root else "")

    # Linux (any target) and macOS account target: sudo does the identity switch.
    if as_root:
        sudo = ["sudo"] + (["-i"] if interactive else [])
    else:
        sudo = ["sudo", "-u", brain] + (["-i"] if interactive else [])
    if interactive:
        return sudo[0], sudo[1:], ("root login shell" if as_root else f"login shell as {brain}")
    return sudo[0], sudo[1:] + ["--"] + list(argv), ("as root" if as_root else f"as {brain}")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _hidden_startupinfo():
    """STARTUPINFO that HIDES the console window the helper powershell.exe would otherwise flash.
    When run_as_brain is invoked from a parent with no attached console (a scheduled task, a
    service, a harness-spawned child), powershell.exe allocates a FRESH console — the half-second
    window flash seen on every `run`. STARTF_USESHOWWINDOW + SW_HIDE creates that console HIDDEN.
    Deliberately NOT CREATE_NO_WINDOW: that detaches the std handles, and the brain child's output
    (redirected + pumped in _WIN_STREAM_PS, because a cross-user CreateProcessWithLogonW child
    cannot share our console) then vanishes. SW_HIDE keeps the console + handles, just unshown.
    None on non-Windows (subprocess.run accepts startupinfo=None)."""
    if platform.system() != "Windows":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


# PowerShell that becomes the brain and STREAMS the child's stdout/stderr live.
# Inputs arrive via env (never argv) so there is zero Python->PS quoting surface:
#   BRAIN_USER / BRAIN_PW / BRAIN_EXE / BRAIN_ARGS.
# Why .NET Process instead of Start-Process:
#   * Start-Process -ArgumentList does not re-quote array elements, so a token with
#     spaces (our `bash -lc '...'`) was re-split by the child -> the GATE-B0 hang.
#     Here BRAIN_ARGS is one already-Windows-quoted command line (subprocess.list2cmdline)
#     handed to .Arguments verbatim; the child parses it back with CommandLineToArgvW.
#   * Start-Process buffered all output to a file until exit (no progress signal).
#     OutputDataReceived/ErrorDataReceived stream each line as it arrives.
# LoadUserProfile=$true loads the brain's HKCU so `wsl -d brain-<brain>` can see the
# per-user distro; WorkingDirectory=SystemRoot avoids an inaccessible-cwd failure.
_WIN_STREAM_PS = r"""
$ErrorActionPreference = 'Stop'
$si = New-Object System.Diagnostics.ProcessStartInfo
$si.FileName               = $env:BRAIN_EXE
$si.Arguments              = $env:BRAIN_ARGS
$si.UseShellExecute        = $false
$si.RedirectStandardOutput = $true
$si.RedirectStandardError  = $true
$si.CreateNoWindow         = $true
$si.UserName               = $env:BRAIN_USER
$si.Password               = (ConvertTo-SecureString $env:BRAIN_PW -AsPlainText -Force)
$si.LoadUserProfile        = $true
$si.WorkingDirectory       = $env:SystemRoot
$p = New-Object System.Diagnostics.Process
$p.StartInfo = $si
[void]$p.Start()
# The brain child runs as a DIFFERENT user (CreateProcessWithLogonW), so it cannot share this
# process's console — its output must be redirected and pumped. Pump to the RAW standard
# handles via OpenStandardOutput/Error (the stdout/stderr handles inherited from Python), NOT
# [Console]::Out — the latter binds to a console SCREEN BUFFER and writes NOTHING once the
# orchestrator is windowless. OpenStandard* works with CREATE_NO_WINDOW, so no console (no
# flash) is needed. CopyToAsync streams bytes live; WaitAll drains both to EOF before exit.
$out = [Console]::OpenStandardOutput()
$err = [Console]::OpenStandardError()
$t1 = $p.StandardOutput.BaseStream.CopyToAsync($out)
$t2 = $p.StandardError.BaseStream.CopyToAsync($err)
$p.WaitForExit()
[System.Threading.Tasks.Task]::WaitAll(@($t1, $t2))
$out.Flush(); $err.Flush()
exit $p.ExitCode
"""


def run_windows(brain, exe, args, interactive, dry_run):
    """Become the brain's Windows account and run (exe, args). Interactive opens a
    new console (the credential boundary forbids attaching a different identity to
    the current terminal). Non-interactive streams stdout/stderr live via a .NET
    Process under the brain credential. Returns the child's exit code."""
    if interactive:
        # New console under the brain's credential. -Wait blocks until it ends.
        filepath = _ps_squote(exe)
        arglist = f" -ArgumentList {_ps_array(args)}" if args else ""
        sp = (f"Start-Process -FilePath {filepath}{arglist} "
              f"-Credential $cred -PassThru -Wait")
        ps = (
            "$ErrorActionPreference='Stop'; "
            "$sec = ConvertTo-SecureString $env:BRAIN_PW -AsPlainText -Force; "
            f"$cred = New-Object System.Management.Automation.PSCredential('{brain}',$sec); "
            f"$p = {sp}; exit $p.ExitCode"
        )
        if dry_run:
            info("[dry-run] would Start-Process -Credential (interactive, new console) as the brain:")
            info(f"          {exe} {' '.join(args)}")
            return 0
        password = get_password(brain)
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            env=dict(os.environ, BRAIN_PW=password),
            startupinfo=_hidden_startupinfo(),  # hide the orchestrator; Start-Process still opens the brain console
        )
        return proc.returncode

    # Non-interactive: one Windows-quoted command line handed to .NET .Arguments,
    # streamed live. This is the fix for the GATE-B0 hang (arg mangling) AND the
    # no-progress blindness (buffer-until-exit) in one change.
    argstr = subprocess.list2cmdline(list(args))
    if dry_run:
        info("[dry-run] would run as the brain (streamed, CreateProcessWithLogonW):")
        info(f"          {exe} {argstr}")
        return 0

    password = get_password(brain)
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _WIN_STREAM_PS],
        env=dict(os.environ, BRAIN_PW=password, BRAIN_USER=brain,
                 BRAIN_EXE=exe, BRAIN_ARGS=argstr),
        startupinfo=_hidden_startupinfo(),  # no flash: hidden console, handles intact for the pump
    )
    return proc.returncode


def run_unix(exe, args, interactive, dry_run):
    """Run (exe, args) directly; sudo/limactl perform the identity switch. For
    interactive we inherit the terminal (Unix genuinely attaches to it)."""
    cmd = [exe] + list(args)
    if dry_run:
        info("[dry-run] would exec:")
        info("          " + " ".join(cmd))
        return 0
    # Inherit stdio in both modes: interactive needs the tty; capture-vs-inherit
    # on Unix is left to the parent's streams (simpler and correct for a console
    # operator tool — output flows straight to the operator).
    return subprocess.run(cmd).returncode


# ---------------------------------------------------------------------------
# Reusable entry point (imported by brain_service.py and other brain_sbin tools)
# ---------------------------------------------------------------------------

def run(brain, argv, target="account", as_root=False, interactive=False, dry_run=False):
    """Run `argv` as the brain and return the child's exit code. `target` is
    'account' (brain host OS account) or 'runtime' (brain's Linux env); as_root
    forces runtime. This is the programmatic form of the CLI below."""
    if as_root:
        target = "runtime"
    system = platform.system()
    exe, inner_args, note = build_inner(brain, target, as_root, interactive, argv, system)
    print(f"run_as_brain: {brain} [{target}] {note}")
    if system == "Windows":
        return run_windows(brain, exe, inner_args, interactive, dry_run)
    return run_unix(exe, inner_args, interactive, dry_run)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        prog="run_as_brain",
        description="Run a command AS the brain (host account or brain runtime env).",
    )
    ap.add_argument("--brain", help="brain name (default: from .brain_provision.json or folder)")
    ap.add_argument("--wsl", action="store_true",
                    help="target the brain's runtime environment (WSL distro on "
                         "Windows / Lima VM on macOS; same as the host account on Linux)")
    ap.add_argument("--root", action="store_true",
                    help="run as root inside the brain's runtime env (implies --wsl)")
    ap.add_argument("-i", "--interactive", action="store_true",
                    help="attach an interactive shell instead of running a command")
    ap.add_argument("--script", metavar="FILE",
                    help="run a script FILE as the brain; trailing '-- ARGS' are passed "
                         "to the script. With --wsl, a Windows FILE path is translated to "
                         "the distro's /mnt/<drive> view.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the resolved command without executing (needs no credential)")
    ap.add_argument("command", nargs=argparse.REMAINDER,
                    help="the command to run (after --), e.g. -- docker ps")
    args = ap.parse_args()

    brain = brain_name(args)
    system = platform.system()

    # --root implies the runtime environment (root only exists there).
    target = "runtime" if (args.wsl or args.root) else "account"

    # Assemble argv from either a script file or the trailing command.
    argv = list(args.command)
    if argv and argv[0] == "--":            # argparse.REMAINDER keeps a leading --
        argv = argv[1:]
    if args.script:
        script = Path(args.script)
        if not script.is_file():
            die(f"script not found: {script}")
        script_args = argv          # trailing tokens after -- become the SCRIPT's args
        # Runtime env is Linux (bash); host account on Windows is PowerShell.
        if target == "runtime":
            if system == "Windows":
                # Windows runtime wraps the payload in `bash -lc <string>`, so hand it
                # ONE properly shell-quoted token (space/quote-safe path + args). The
                # host path is translated to the distro's /mnt/<drive> view.
                argv = [shlex.join(["bash", to_wsl_path(script)] + script_args)]
            else:
                # Unix runtime execs an argv vector via sudo — pass it unquoted.
                argv = ["bash", str(script)] + script_args
        elif system != "Windows":
            argv = ["bash", str(script)] + script_args
        else:
            argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(script)] + script_args

    if not args.interactive and not argv:
        die("nothing to run: give a command after --, use --script, or pass -i.")

    rc = run(brain, argv, target=target, as_root=args.root,
             interactive=args.interactive, dry_run=args.dry_run)
    sys.exit(rc)


if __name__ == "__main__":
    main()
