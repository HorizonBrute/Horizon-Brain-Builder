#!/usr/bin/env python3
"""
brain_installer_1_admin.py - ADMIN-side deploy of a brain engine (rootless Docker in WSL2).

Run this in an ELEVATED console. It does the host-level "needful", then hands off to
phase 2 (which runs AS the brain user). Presumes:
  - the brain OS user account already exists with correct permissions,
  - the deploy package is unzipped into the brain folder, containing:
      <brain>/system/wsl_engine/<brain>_engine.tar   (the preconfigured WSL distro image)
      <brain>/system/brain_bin/deploy/brain_installer_2_brain.py

What it does:
  1. Resolve brain identity (.brain_provision.json, or --brain NAME).
  2. Read the brain password from the OS keystore (brain:<brain> / account_password, what
     create_brain.py wrote); fall back to $BRAIN_CRED_HELPER, then prompt. Never blocks headless.
  3. Ensure WSL2 is present and updated.
  4. ACL the brain folder so the brain account controls system/wsl_engine/,
     system/deploy_logs/ and knowledge/.
  5. Lock the brain's edit-source (its code + policy) read-only to the brain ACCOUNT
     (Windows-side defense-in-depth for the security model; applied in every posture).
  6. Launch phase 2 AS the brain user (or, with --no-launch, print the runas command).
  7. Register the per-brain BOOT residency (keepalive) task — the only thing that holds
     the WSL distro resident so the stack survives idle-shutdown/reboot (skip: --skip-residency).

Usage:
  python brain_installer_1_admin.py [--brain NAME] [--posture personal|server] [--no-launch]
                                    [--skip-residency]
"""
import argparse
import ctypes
import getpass
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent              # <brain>/system/brain_bin/deploy
BRAIN_DIR = HERE.parent.parent.parent                       # <brain>
PROVISION = BRAIN_DIR / ".brain_provision.json"
# Credential namespaces in the ONE OS-native keystore — MUST stay in sync with
# windows_deploy_brain.py's and run_as_brain.py's _keyring_namespaces(). Order = lookup
# precedence (first hit wins). A Horizon.AIOS PLATFORM brain's create-brain
# (horizon_aios_create_brain.py) writes the password to 'horizon_aios'/'brain_account:<brain>';
# a STANDALONE factory create_brain.py writes 'brain:<brain>'/'account_password'. Prefer the
# platform namespace when this is a platform-provisioned brain (the .aios_provision.json marker
# is present) or when a host advertises its own namespace via $BRAIN_KEYRING_SERVICE, and always
# try the brain-owned namespace LAST so a stale entry there can't shadow the real credential.
AIOS_PROVISION = BRAIN_DIR / ".aios_provision.json"


def _keyring_namespaces():
    ns = []
    host_service = os.environ.get("BRAIN_KEYRING_SERVICE")
    if host_service:
        ns.append((host_service, os.environ.get("BRAIN_KEYRING_USER", "brain_account:{brain}")))
    elif AIOS_PROVISION.is_file():
        ns.append(("horizon_aios", "brain_account:{brain}"))   # Horizon.AIOS platform namespace
    ns.append(("brain:{brain}", "account_password"))           # standalone / brain-owned namespace
    return tuple(ns)

# OPTIONAL host credential-helper seam (a fallback, NOT the primary path). A host platform
# that provisions brain accounts usually already holds the password in the keystore above;
# point $BRAIN_CRED_HELPER at a script supporting
#     <helper> get <brain> --show   ->   prints the password on stdout, rc 0
# and this deploy uses it if the keystore missed. Unset (the default) => keystore-or-prompt.
# Named by env, never guessed: the factory carries no knowledge of any host's layout.
_CRED_HELPER = os.environ.get("BRAIN_CRED_HELPER")
CRED = Path(_CRED_HELPER) if _CRED_HELPER else None
PHASE2 = HERE / "brain_installer_2_brain.py"

# Brain-relative RUNTIME homes. MUST match windows_deploy_brain.py's constants of the same
# names — the 2026-07-13 `wsl` → `wsl_engine` rename changed one copy of this path and not the
# others, which is exactly the drift these named constants exist to prevent.
WSL_RUNTIME_REL = ("system", "wsl_engine")     # live distro: disk/, _build/, residency_task.xml
DEPLOY_LOGS_REL = ("system", "deploy_logs")    # host-side phase logs, ALL phases, one dir

# One stamp per deploy RUN, so every phase's logs sort and group together. Date+time, not
# date alone: a brain gets redeployed many times a day and a bare date would self-overwrite.
_DEPLOY_STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")


def deploy_log_path(phase, suffix=".log"):
    """<brain>/system/deploy_logs/<YYYYmmdd-HHMMSS>_deploy_phase<N>.log|.err

    ONE home and ONE shape for every phase. Phase 2's log used to land in the live WSL
    runtime dir, which mixed host-side deploy output into the 12 GB distro workspace and
    left phase 1 with no log at all (it ran in the deployer's foreground and its output
    survived only in whatever console the operator happened to be watching).
    """
    d = BRAIN_DIR.joinpath(*DEPLOY_LOGS_REL)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_DEPLOY_STAMP}_deploy_phase{phase}{suffix}"

# The brain's own Python resolver (system/brain_sbin/). Used to pick an interpreter the
# BRAIN account can actually execute for the phase-2 handoff — never sys.executable,
# which may be the admin's per-user Store Python (un-runnable by the brain).
sys.path.insert(0, str(BRAIN_DIR / "system" / "brain_sbin"))
try:
    from brain_python_resolver import resolve_brain_python
except ImportError:
    resolve_brain_python = None

# Edit-source the brain runs but must never rewrite (brain_security_model.md #2:
# the writer is never the runner). Policy/identity files + the leash
# (.claude/settings*.json — Claude Code permissions; if the brain could rewrite it
# it could unclip its own leash, threat #2). The neuron platform code dirs
# (system/common_neuron_platform/{input,action}/) are handled separately as directories.
CONTEXT_FILES = ("CLAUDE.md", "agents.md", "brain_core.md", "brain_invariants.md")

# The SHARED per-role platform image source dirs (ADR-0015: neurons are container bundles built
# from these; config-flow Phase 5 relocated them from the retired root input_neurons/ +
# action_neurons/ into system/common_neuron_platform/{input,action}/, one image per role). Locked
# read-only to brain-runners so the brain cannot rewrite the code it runs; the runtime copies are
# the RO code-in seams /opt/input_neurons + /opt/action_neurons in-distro (mount targets unchanged).
NEURON_SRC_DIRS = ("system/common_neuron_platform/input", "system/common_neuron_platform/action")

# Deny mask: write/create/append/delete class, leaving read+execute+read-attrs
# intact so the brain can still READ its policy across /mnt/c. Mirrors
# the host harden model's brains-no-write mask. Omits WRITE_DAC deliberately: the
# brain does not OWN these paths (owner is the human/admin), so it has no implicit
# right to re-permission — no need to deny what it never had.
NOWRITE_MASK = "(WD,AD,WEA,WA,DE,DC)"


class _Tee:
    """Duplicate a console stream into one or more files (append-only, line-flushed).

    Phase 1 runs in the DEPLOYER's foreground, so unlike phase 2 it was never redirected
    anywhere: its output lived only in whatever console the operator was watching, and a
    detached/backgrounded deploy lost it entirely. Teeing gives phase 1 a durable log with
    the same home and shape as phase 2's.
    """

    def __init__(self, stream, *files):
        self._stream, self._files = stream, files

    def write(self, s):
        self._stream.write(s)
        for f in self._files:
            f.write(s)
            f.flush()          # a killed/reaped deploy must not lose buffered lines
        return len(s)

    def flush(self):
        self._stream.flush()
        for f in self._files:
            f.flush()

    def isatty(self):
        return getattr(self._stream, "isatty", lambda: False)()


def info(m): print(f"  {m}")
def step(n, m): print(f"\n[{n}] {m}")
def warn(m): print(f"  [WARN] {m}", file=sys.stderr)
def die(m): print(f"  [ERROR] {m}", file=sys.stderr); sys.exit(1)


def require_admin():
    try:
        if not ctypes.windll.shell32.IsUserAnAdmin():
            die("Run this from an Administrator console.")
    except Exception:
        die("Could not verify elevation; run from an Administrator console.")


def brain_name(args):
    if args.brain:
        return args.brain
    if PROVISION.is_file():
        try:
            return json.loads(PROVISION.read_text(encoding="utf-8"))["brain_name"]
        except Exception:
            pass
    return BRAIN_DIR.name


def get_password(brain):
    """Brain Windows password. Read the OS keystore DIRECTLY (the source create_brain.py
    writes) — this is the one path a headless elevated deploy can complete: it never blocks.
    Only if the keystore misses do we try the optional host cred-helper, then (last resort,
    for a standalone by-hand run) prompt."""
    try:
        import keyring
        for service_tmpl, user_tmpl in _keyring_namespaces():
            try:
                # BOTH halves are templates. Formatting only the user half asks the vault for
                # a service literally named "brain:{brain}" (braces included) — a guaranteed
                # miss for every brain. This is the same one-char seam that hid in the deployer
                # and run_as_brain; keep both .format() calls.
                pw = keyring.get_password(service_tmpl.format(brain=brain),
                                          user_tmpl.format(brain=brain))
            except Exception as e:
                info(f"password: keystore read failed for '{service_tmpl}' ({e})")
                pw = None
            if pw:
                info(f"password: retrieved from OS keystore (namespace '{service_tmpl.format(brain=brain)}')")
                return pw
        info("password: not found in OS keystore")
    except ImportError:
        info("password: `keyring` not installed — trying cred-helper / prompt")

    if CRED is not None and CRED.is_file():
        try:
            p = subprocess.run([sys.executable, str(CRED), "get", brain, "--show"],
                               capture_output=True, text=True)
            if p.returncode == 0 and p.stdout.strip():
                info("password: retrieved from the host credential helper")
                return p.stdout.strip()
            info("password: not retrievable from the host credential helper")
        except Exception as e:
            info(f"password: cred-helper lookup failed ({e})")
    return getpass.getpass(f"  Enter Windows password for '{brain}': ")


def ensure_wsl():
    p = subprocess.run(["wsl", "--status"], capture_output=True, text=True)
    if p.returncode != 0:
        info("WSL not detected; installing the WSL platform (may require a reboot)...")
        subprocess.run(["wsl", "--install", "--no-distribution"])
    else:
        info("WSL present; pulling platform/kernel updates...")
        subprocess.run(["wsl", "--update"])


# Dirs that hold a DATA DOOR — a `mklink /D` reparse point into the RUNNING distro
# (knowledge/brain_rw/chroma). A recursive icacls pass FOLLOWS a reparse point, so no
# `/T` may ever be rooted AT or ABOVE one of these: it would walk off the host and
# re-permission the live 9p share inside the distro. Mirrors _DOOR_PARENTS in the
# deployer — KEEP THE TWO LISTS IN SYNC.
_DOOR_PARENTS_REL = (("knowledge", "brain_rw"),)


def _icacls_grant(path, brain, recurse):
    """One grant. rc is CHECKED: /C suppresses per-file errors but not a principal that
    fails to resolve, and a silently-skipped grant here leaves the brain unable to write
    its own runtime state — surfacing much later as an unrelated-looking failure."""
    cmd = ["icacls", str(path), "/grant", f"{brain}:(OI)(CI)F", "/C"]
    if recurse:
        cmd.append("/T")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        die(f"icacls grant failed on {path}: {r.stderr.strip()}")


def _grant_around_doors(root, brain, doors):
    """Grant below `root` while treating each door parent as a FLOOR: grant on the door
    dir itself non-recursively (repair the dir, never enter it) and descend everywhere
    else. Files directly under a granted dir pick the ACE up by inheritance."""
    for child in sorted(root.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        _icacls_grant(child, brain, recurse=child not in doors)


def set_acls(brain):
    # The brain must own its RUNTIME state. These sit under system/ (ADR-0019 amended) but are
    # NOT source: lock_edit_source is surgical (NEURON_SRC_DIRS + context files + settings*.json),
    # so nothing here collides with the read-only edit-source lock applied in step 5.
    for sub in (WSL_RUNTIME_REL, DEPLOY_LOGS_REL, ("knowledge",)):
        d = BRAIN_DIR.joinpath(*sub)
        d.mkdir(parents=True, exist_ok=True)
        doors = [BRAIN_DIR.joinpath(*p) for p in _DOOR_PARENTS_REL if p[:len(sub)] == sub]
        if doors:
            # A door lives at or below this root — never /T from here.
            _icacls_grant(d, brain, recurse=False)
            _grant_around_doors(d, brain, doors)
        else:
            _icacls_grant(d, brain, recurse=True)
        info(f"ACL: {brain} controls {'/'.join(sub)}/")


def _deny_nowrite(path, principal, container):
    """Apply the read-only (NOWRITE) Deny for `principal` to one path.
    Deny beats the Full inherited from the brain-folder root; owner/Administrators
    keep Full. container=True adds (OI)(CI) + /T so a directory's children inherit."""
    mask = f"(OI)(CI){NOWRITE_MASK}" if container else NOWRITE_MASK
    cmd = ["icacls", str(path), "/deny", f"{principal}:{mask}", "/C"]
    if container:
        cmd.append("/T")
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        info(f"  [WARN] could not lock {path}: {p.stderr.strip() or p.stdout.strip()}")
        return False
    return True


def lock_edit_source(brain, posture):
    """Windows-side defense-in-depth: make the brain's edit-source (its code + its
    policy/identity) read-only to the per-brain RUNTIME GROUP <brain>_group, so that
    in postures where the /mnt/c automount is still on, no account that runs as the
    brain can edit the source it runs (brain_security_model.md #7).

    We deny the GROUP, not the single brain user: <brain>_group is the set of
    identities that may run as the brain (one brain can be run by several accounts),
    so the whole group is treated as brain-runners. Read+execute stay intact.

    CAVEAT — writer≠runner membership: `create-brain` also adds the human invoker to
    <brain>_group for folder access, and a group Deny beats that human's Allow (even
    when elevated — the group SID rides in their token). For this lock to leave humans
    able to edit policy, the human operator must NOT be in the runtime group; humans
    edit as the owner, via the host's human-operators group, or as Administrators (all
    retain Full). See §7 of brain_security_model.md.

    Posture dial (mirrors provision/stage7_harden.sh) — secure by default, no lax tier:
      personal  -> lock code + policy read-only to the runtime group.
      server    -> same lock here; host-fs/egress screws live in-distro (stage7).
    """
    group = f"{brain}_group"  # per-brain runtime group (Windows naming: <brain>_group)

    # system/common_neuron_platform/{input,action}/ — the shared per-role platform image source
    # (ADR-0015: neurons are container bundles built from these; config-flow Phase 5 relocated them
    # here from the retired root input_neurons/ + action_neurons/). Create each so the deny is
    # inheritable and future code is born read-only to brain-runners (the edit-copies; the runtime
    # copies are the RO code-in seams /opt/input_neurons + /opt/action_neurons in-distro).
    for sub in NEURON_SRC_DIRS:
        nd = BRAIN_DIR / sub
        nd.mkdir(parents=True, exist_ok=True)
        if _deny_nowrite(nd, group, container=True):
            info(f"ACL: {group} read-only on {sub}/ (neuron code edit-source)")

    # Policy/identity files + the leash. Files take no inheritance flags. The
    # .claude/ dir itself stays writable (the brain's Claude Code session writes
    # session state there); only settings*.json (its permissions) are locked.
    # Context files are SEEDED by the deployer, which dies if seeding fails — so by the
    # time we run, every one MUST exist. An absent one is a broken contract, not routine:
    # skipping it would leave the brain able to rewrite its own policy while we print green.
    for f in (BRAIN_DIR / f for f in CONTEXT_FILES):
        rel = f.relative_to(BRAIN_DIR)
        if not f.is_file():
            die(f"context file missing, cannot lock it: {rel}. The deployer must seed all "
                f"of {', '.join(CONTEXT_FILES)} before this runs — refusing to leave the "
                f"brain's own policy writable.")
        if _deny_nowrite(f, group, container=False):
            info(f"ACL: {group} read-only on {rel}")

    # The leash. NOT the same contract as the context files: nothing stages .claude/ or
    # settings*.json, so this glob legitimately matches nothing on a fresh deploy. That is
    # exactly why it must be LOUD — an empty glob runs no loop body and would otherwise
    # print nothing at all, reading as "leash applied" when no leash exists. If the brain's
    # first session creates settings.json itself, it is born UNLOCKED and the brain owns its
    # own Claude Code permissions.
    leash = sorted((BRAIN_DIR / ".claude").glob("settings*.json"))
    if not leash:
        warn("NO .claude/settings*.json present — the permissions leash was NOT applied. "
             "If a brain session creates one later it will be writable by the brain.")
    for f in leash:
        rel = f.relative_to(BRAIN_DIR)
        if _deny_nowrite(f, group, container=False):
            info(f"ACL: {group} read-only on {rel}")


def resolve_python(brain):
    """Resolve a brain-runnable (machine-wide) Python for the phase-2 handoff.

    NEVER sys.executable: this admin process may be running the admin's per-user
    Microsoft Store Python (under \\Users\\...\\WindowsApps), which the brain
    account cannot execute. Fail loudly if no machine-wide interpreter exists —
    an un-runnable handoff command is worse than a clear preflight error.
    """
    if resolve_brain_python is None:
        die("system/brain_sbin/brain_python_resolver.py not importable — cannot resolve a "
            "brain-runnable Python. Restore it and re-run.")
    rp = resolve_brain_python()
    if rp is None:
        die("No brain-runnable (machine-wide) Python found on this host. A brain "
            "account cannot use the admin's per-user Store Python. Install an "
            "all-users Python and re-run:\n"
            "    winget install Python.Python.3.12 --scope machine")
    info(f"brain-runnable Python: {rp.path} "
         f"(v{rp.version[0]}.{rp.version[1]}.{rp.version[2]}, via {rp.source})")
    return rp.path


def launch_phase2(brain, password, no_launch, brain_py):
    if not PHASE2.is_file():
        die(f"phase 2 script missing: {PHASE2}")
    if no_launch:
        print("\n  Run phase 2 yourself, as the brain:")
        print(f'    runas /user:{brain} "{brain_py} \\"{PHASE2}\\" --brain {brain}"')
        return
    # Launch phase 2 as the brain via a PSCredential; password via env (not argv).
    log = deploy_log_path(2, ".log")
    err = deploy_log_path(2, ".err")
    # -WorkingDirectory is REQUIRED: Start-Process -Credential defaults the child's working
    # directory to the CALLER's current location, and if the brain user cannot access it the
    # launch dies "The directory name is invalid" before phase 2 ever runs. The factory now
    # lives INSIDE brain_workshop/ (post ADR-0019), whose ACL grants no access to OTHER brains,
    # so a deploy launched from the factory CWD hit exactly that. Pin the child to %SystemRoot%
    # (always brain-accessible), mirroring run_as_brain's _WIN_STREAM_PS. brain_py + PHASE2 are
    # absolute, so the working directory is irrelevant to resolution — only to the launch gate.
    ps = (
        f"$sec = ConvertTo-SecureString $env:BRAIN_PW -AsPlainText -Force; "
        f"$cred = New-Object System.Management.Automation.PSCredential('{brain}',$sec); "
        f"Start-Process -FilePath '{brain_py}' "
        f"-ArgumentList '\"{PHASE2}\" --brain {brain}' -Credential $cred "
        f"-WorkingDirectory $env:SystemRoot "
        f"-RedirectStandardOutput '{log}' -RedirectStandardError '{err}' -Wait"
    )
    info("launching phase 2 as the brain user...")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   env=dict(os.environ, BRAIN_PW=password))
    if log.is_file():
        print("\n----- phase 2 output -----")
        print(log.read_text(encoding="utf-8", errors="replace"))


def register_residency(brain, password, skip):
    """Register the per-brain BOOT residency (keepalive) task — the ONLY thing that holds
    the WSL distro resident, so the stack survives WSL idle-shutdown and reboot. This is an
    ELEVATED, password-holding concern (see residency.py), so it lives here, after phase 2
    has imported the distro AND shipped ~/keepalive.sh (launch_phase2 blocks with -Wait).
    Without this step the distro idle-shuts and the gateway vanishes between calls."""
    if skip:
        info("residency registration skipped (--skip-residency) — persistence NOT wired.")
        return
    sys.path.insert(0, str(HERE))  # residency.py ships alongside this file (deploy/)
    try:
        import residency
    except ImportError as e:
        info(f"[WARN] residency.py not importable ({e}) — boot keepalive task NOT registered. "
             "The distro will idle-shut and the stack will not survive reboot. Register it "
             "manually once residency.py is restored.")
        return
    residency.register(brain, f"brain-{brain}", password, BRAIN_DIR, info)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain")
    ap.add_argument("--posture", choices=("personal", "server"),
                    default="personal",
                    help="hardening posture for the Windows edit-source ACL "
                         "(default: personal). personal/server both lock code/policy "
                         "read-only to the brain account (secure by default). "
                         "Mirrors provision/stage7_harden.sh.")
    ap.add_argument("--no-launch", action="store_true",
                    help="do host prep only; print the runas command for phase 2")
    ap.add_argument("--skip-residency", action="store_true",
                    help="do host prep + phase 2 but do NOT register the boot residency "
                         "(keepalive) task; persistence across idle/reboot is NOT wired.")
    args = ap.parse_args()

    # cp1252 consoles (detached WMI/runas launches) raise UnicodeEncodeError when we print
    # the phase-2 log (which can carry non-cp1252 bytes). Force UTF-8 so the deploy does not
    # die at the finish line — replaces the PYTHONIOENCODING launcher workaround.
    for _s in (sys.stdout, sys.stderr):
        try: _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception: pass

    # Phase 1's own log — same dir + shape as phase 2's. .log carries the full interleaved
    # transcript (what the console shows); .err carries stderr alone, mirroring phase 2's split.
    try:
        _p1_log = deploy_log_path(1, ".log")
        _p1_err = deploy_log_path(1, ".err")
        _lf = open(_p1_log, "w", encoding="utf-8", errors="replace")
        _ef = open(_p1_err, "w", encoding="utf-8", errors="replace")
        sys.stdout = _Tee(sys.stdout, _lf)
        sys.stderr = _Tee(sys.stderr, _lf, _ef)
    except OSError as _e:
        print(f"  [WARN] could not open the phase 1 log ({_e}) — continuing unlogged")

    require_admin()
    brain = brain_name(args)
    print("=" * 60)
    print(f"Brain engine deploy (admin) - {brain}")
    print(f"  brain dir: {BRAIN_DIR}")
    print(f"  posture:   {args.posture}")
    print("=" * 60)

    step(1, "brain identity"); info(brain)
    step(2, "credential"); pw = get_password(brain)
    step(3, "WSL platform"); ensure_wsl()
    step(4, "brain-folder ACLs"); set_acls(brain)
    step(5, f"edit-source read-only ACLs (posture={args.posture})")
    lock_edit_source(brain, args.posture)
    step(6, "resolve brain-runnable Python (preflight)"); brain_py = resolve_python(brain)
    step(7, "hand off to phase 2 (brain user)")
    launch_phase2(brain, pw, args.no_launch, brain_py)
    if args.no_launch:
        info("residency not registered: phase 2 was not launched (--no-launch). After you run "
             "phase 2, re-run this installer without --no-launch to register the boot task.")
    else:
        step(8, "register boot residency (keepalive task)")
        register_residency(brain, pw, args.skip_residency)
    print("\n[done] admin phase complete.")


if __name__ == "__main__":
    main()
