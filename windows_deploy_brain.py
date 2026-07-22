#!/usr/bin/env python3
"""
windows_deploy_brain.py — The Brain Deploy Orchestrator (Windows / WSL2)
===============================================================

One elevated entry point that sequences every brain building block into a single
converging deploy: bare host → account → staged code → engine → residency →
gateway → verified TLS heartbeat. And the inverse: tear it down to rebuild any
time.

PLATFORM
    This is the **Windows / WSL2** orchestrator — the brain engine is a per-user
    WSL2 distro (`wsl --import` of a provisioned Debian), residency is a Windows
    Task Scheduler boot task, and the config seam mounts via drvfs (9p). It is
    Windows-only by construction (`wsl`, `schtasks`, `icacls`, Credential Manager).
    The native-Linux sibling is `linux_deploy_brain.py` (systemd + rootless Docker
    + bind-mount seam, no VM). macOS is a later objective.

WHY THIS EXISTS
    The capability to stand up a brain is real but scattered across lanes:
    account provisioning (create_brain), engine deploy (brain_installer_1/2),
    gateway port/token/firewall (gateway_port / gateway_token), and teardown.
    Nothing *combined* them. This is that layer — the sequencing + preflight +
    verification + teardown driver.

DESIGN
    - **Calls the block CLIs; never imports their internals.** The installers and
      gateway tools keep changing; their stable command-line is the contract. If a
      block changes a flag, adapt the call here — not the block.
    - **Stages are idempotent.** Each checks "already done?" and converges, so a
      re-run after a partial failure is safe and a full re-run is a no-op.
    - **Provider seam for create-brain.** When this deploy runs inside a Horizon.AIOS
      install (detected via $HORIZON_ROOT) it calls the platform's
      $HORIZON_SBIN/horizon_aios_create_brain.py; on any other host it calls the factory's
      standalone create_brain.py. The product must deploy on hosts with no platform at all.
    - **The orchestrator calls the *staged* copies** of the block tools (the ones
      inside brains/<brain>/ after staging), because those resolve their own
      brain-relative paths.

THE ENGINE ARTIFACT
    By default the multi-GB `<brain>_engine.tar` (the provisioned WSL distro image)
    is an INPUT to deploy, not part of the code package: deploy validates it is
    present (or a distro is already imported) and fails loud with the remedy if not;
    supply a prebuilt one with --engine-tar. This keeps infra REUSE the default.

    From-scratch, this orchestrator can ALSO build it. `build-engine` (and
    `deploy --from-scratch`) obtain a base Debian into a throwaway scratch distro,
    run the provision/ recipe (stages 1–9) collapsed into one idempotent flow,
    `wsl --export` the result, and drop the scratch distro. That path REFUSES to
    reuse an existing distro or a stale tar — the point of a cold-start test is a
    freshly built engine.

STAGING — factory/source/ IS the brain root
    `factory/source/` is a one-to-one image of a deployed brain: what you see there
    is what lands in <install-root>/brains/<brain>/. Staging is therefore a single
    `copytree(source, brain_dir)` + a perm pass — no member list, no build step, no
    tarball. The delivered artifact IS this repo. Two rules bound the copy:
      - PROTECTED paths are never overwritten (the brain's own data + live runtime).
      - Everything else is factory-owned and REFRESHED on every deploy, so fixes ship.
    See _STAGE_PROTECT / _STAGE_REFRESH.

INSTALL ROOT
    Explicit or nothing: `--install-root` → `$AIOS_INSTALL_ROOT` → die. This orchestrator
    never guesses where your brains live and never walks up looking for one.

USAGE
    windows_deploy_brain.py deploy   --brain X --posture personal|server
                             [--port N] [--bind personal|server]
                             [--engine-tar <tar>]
                             [--from-scratch [--imagefile <path>] [--keep-scratch]]
                             --install-root <dir> [--skip-gateway]
    windows_deploy_brain.py build-engine --brain X [--posture personal|server]
                             [--imagefile <path>] [--keep-scratch] [--dry-run]
    windows_deploy_brain.py teardown --brain X [--purge --yes]
    windows_deploy_brain.py verify   --brain X [--port N]
    windows_deploy_brain.py status   --brain X

Run elevated (Administrator), from an elevated console.
"""

import argparse
import contextlib
import ctypes
import inspect
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

# --- Platform seam (Project 001 — consolidate onto one deploy_brain.py) --------
# This deployer began as Windows/WSL2-only and is the TRUNK the cross-platform
# deployer is built from. Windows behavior stays byte-for-byte: every OS-forced
# step branches on these flags with the existing Windows path unchanged in the
# `else`. Only the five OS-forced concepts (identity switch, engine host+snapshot,
# seam mount, residency, firewall) — and their Windows-only support subsystems —
# branch. Each concept is centralized behind ONE helper that branches internally
# (see NOTE 001-5), so call sites stay unchanged rather than sprouting `if` forks.
_IS_WINDOWS = sys.platform.startswith("win")
_IS_LINUX = sys.platform.startswith("linux")

# --- Robust console on legacy Windows code pages (never crash on non-ASCII) ---
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

FACTORY_ROOT = Path(__file__).resolve().parent          # the repo root — this file lives here
# ONE-TO-ONE image of the brain root. RESOLVED: the staging filter compares walked dirs
# against it by identity, so a symlinked component would make the root compare unequal to
# itself — and the "at the source root" rules (which files are seeded rather than copied)
# would quietly stop firing.
SOURCE_ROOT  = (FACTORY_ROOT / "factory" / "source").resolve()
STANDALONE_CREATE_BRAIN = FACTORY_ROOT / "factory" / "create_brain.py"

# Brain-root context/policy files. They load into agent context and are ACL-locked read-only
# by installer_1 (its CONTEXT_FILES — keep the two lists identical: a name here that the
# installer does not know is a file the brain can rewrite).
#
# These live in source/ AT THEIR FINAL NAMES and are the ONE upstream — there is no template
# store above the repo root, no *.template suffix, and no policy_templates/ staging hop to
# collect them into. They are the only source members the copy does NOT copy: staging skips
# them (see _make_stage_ignore) and seed_brain_context_files() places them instead, because
# they need [BRAIN_NAME] substitution and must NEVER clobber a live brain's tuned policy.
CONTEXT_FILES = ("brain_invariants.md", "CLAUDE.md", "agents.md", "brain_core.md")

# Provider seam. When this deploy runs inside a Horizon.AIOS install, the platform provisions
# OS accounts through its own scripts under $HORIZON_SBIN. Detection is simply "is Horizon.AIOS
# installed here" — $HORIZON_ROOT set and a real directory (see is_provider_host()). On any
# other host (a bare clone) $HORIZON_ROOT is absent and the deploy uses the factory's own
# standalone create_brain.py. $HORIZON_SBIN falls back to $HORIZON_ROOT/horizon_system/sbin.
_HORIZON_ROOT_ENV = "HORIZON_ROOT"
_HORIZON_SBIN_ENV = "HORIZON_SBIN"
_horizon_root = os.environ.get(_HORIZON_ROOT_ENV)
_horizon_sbin = os.environ.get(_HORIZON_SBIN_ENV)
HORIZON_ROOT  = Path(_horizon_root) if _horizon_root else None
HORIZON_SBIN  = (Path(_horizon_sbin) if _horizon_sbin
                 else (HORIZON_ROOT / "horizon_system" / "sbin") if HORIZON_ROOT else None)
PROVIDER_CREATE_BRAIN = (HORIZON_SBIN / "horizon_aios_create_brain.py") if HORIZON_SBIN else None
PROVIDER_REMOVE_BRAIN = (HORIZON_SBIN / "horizon_aios_remove_brain.py") if HORIZON_SBIN else None

BRAIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,19}$")

# The factory-side tools used for host preflight (before anything is staged) — read out of
# source/, the same copy the brain will get.
FACTORY_PY_RESOLVER = SOURCE_ROOT / "system" / "brain_sbin" / "brain_python_resolver.py"


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def banner(text):
    line = "=" * (len(text) + 6)
    print(f"\n{line}\n=== {text} ===\n{line}")


def stage(n, total, text):
    print(f"\n[{n}/{total}] {text}")


_VERBOSE = False


def _tag():
    """`script.py:function ` for the caller of the emitter, when -v is on.

    Two scripts print the same lines with the same prefixes (this file and the
    factory's standalone create_brain.py, which runs as a subprocess). Without a
    tag, an identical [ERROR] from either is indistinguishable in the transcript.
    Frame 2 is the emitter's caller: _tag <- ok/warn/err/info <- caller."""
    if not _VERBOSE:
        return ""
    try:
        return f"{Path(__file__).name}:{inspect.stack()[2].function} "
    except Exception:
        return f"{Path(__file__).name}:? "


def info(m):  print(f"  {_tag()}{m}")
def ok(m):    print(f"  [OK]   {_tag()}{m}")
def warn(m):  print(f"  [WARN] {_tag()}{m}")
def err(m):   print(f"  [ERROR] {_tag()}{m}", file=sys.stderr)


def die(m, code=1):
    err(m)
    sys.exit(code)


def _elapsed(start):
    s = int(time.monotonic() - start)
    return f"{s//60}m{s%60:02d}s" if s >= 60 else f"{s}s"


def _hb_glyph():
    """A liveness marker stderr can actually ENCODE. The console is reconfigured to utf-8 up
    top, but stderr may still be a legacy code page (redirected to a file, an old console) that
    cannot encode ⏳ — probe once and fall back to ASCII so a tick never raises mid-deploy or
    prints a replacement box."""
    try:
        "⏳".encode(sys.stderr.encoding or "ascii")
        return "⏳"
    except Exception:
        return "*"


@contextlib.contextmanager
def heartbeat(label, interval=5):
    """Proof-of-life tick while a long, often SILENT blocking child runs — so the deploy never
    looks hung. The acute case (NOTE 001-53): the from-scratch engine build's in-distro apt /
    `unattended-upgrade --dry-run` buffers its stdout until it completes, so the terminal shows
    NOTHING for minutes — indistinguishable from a hang. Other quiet stretches: downloading a WSL
    base image, exporting/importing multi-GB distros, pulling Ollama models.

    A daemon thread rewrites ONE line on STDERR every `interval`s —
        \\r  ⏳ <label> — still working (Ns)
    — carriage-return in place (no scrollback spam), stopped by a threading.Event the instant the
    block exits. On exit it clears the tick line and drops to a fresh line so whatever prints next
    starts clean at column 0.

    STDERR on purpose: the tick must never pollute a child's captured stdout / log parsing (the
    PYTHON_EXIT capture, curl http_code, etc. all read stdout). CARRIAGE-RETURN on purpose: a
    single self-overwriting line on Windows consoles, no ANSI required, ASCII glyph fallback when
    ⏳ is unencodable. A fast call (< one interval) prints nothing at all — no tick, no 'done'.

    Caveat: for a child that ALSO streams live output to stdout, the \\r tick and that output share
    the screen; the finalizer's newline keeps the tick from bleeding into later output, but a rare
    mid-stream overlap is possible. The acute apt case is fully buffered, so it never streams."""
    stop = threading.Event()
    start = time.monotonic()
    glyph = _hb_glyph()
    clear = "\r" + " " * (len(label) + 48) + "\r"

    def _tick():
        # First tick fires after one full interval — a call that returns fast stays silent.
        while not stop.wait(interval):
            try:
                sys.stderr.write(f"\r  {glyph} {label} — still working ({_elapsed(start)}) ")
                sys.stderr.flush()
            except Exception:
                return

    t = threading.Thread(target=_tick, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=1)
        # Only tidy up (and print 'done') if at least one tick could have fired; otherwise leave
        # the stream untouched so a fast call is truly silent.
        if (time.monotonic() - start) >= interval:
            try:
                sys.stderr.write(clear)
                sys.stderr.write(f"  {glyph} {label} — done ({_elapsed(start)})\n")
                sys.stderr.flush()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

def run(cmd, check=True, capture=False, env=None):
    """Run an argv command. Returns CompletedProcess. Never shell=True."""
    display = " ".join(str(a) for a in cmd)
    info(f"run: {display}")
    return subprocess.run(cmd, check=check, capture_output=capture, text=True, env=env)


def run_out(cmd):
    """Run and return (returncode, stdout, stderr) without raising."""
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def _http_code(out):
    """Extract the HTTP status from `curl -o /dev/null -w '%{http_code}'` output. curl writes the
    3-digit code with NO trailing newline and run_as_brain appends its identity banner on the SAME
    stdout stream, so the raw capture is glued: '403run_as_brain: sorcerypunk_dev …'. The curl code
    always LEADS the stream, so take the first 3-digit run at the start (not-followed-by-a-digit so
    a longer number can't over-match). Fall back to the last whitespace-delimited all-digit token
    for the newline-terminated case (`-w '%{http_code}\\n'`). (The old 'banner is on stderr' premise
    no longer holds — the banner lands on stdout, which is why the plain split returned '' and every
    heartbeat probe looked like a failure.)"""
    m = re.match(r"\s*(\d{3})(?!\d)", out or "")
    if m:
        return m.group(1)
    toks = [t for t in (out or "").split() if t.isdigit()]
    return toks[-1] if toks else ""


def _probe_gateway(run_as_brain, brain, curl_cmd, want, timeout=30, interval=3):
    """Poll a curl-through-the-brain heartbeat until it returns the wanted HTTP code
    (str) or `timeout` seconds elapse. Returns (rc, out, err, code) from the LAST probe.

    verify() runs immediately after `reapply_stack` force-recreates the gateway, so the
    FIRST probe routinely hits a connection-refused (curl rc 7, empty code) for a second
    or two while nginx re-binds — that is NOT a failure, it's readiness lag. Polling with a
    short backoff lets the just-recreated gateway come up before we declare the deploy dead
    (bug 18: verify had no readiness retry and killed an otherwise-healthy deploy)."""
    deadline = time.monotonic() + timeout
    rc, out, e, code = 1, "", "", ""
    attempt = 0
    while True:
        attempt += 1
        rc, out, e = run_out(run_as_brain_argv(run_as_brain, brain, curl_cmd))
        code = _http_code(out)
        if code == want:
            if attempt > 1:
                info(f"gateway ready after {attempt} probes (~{(attempt - 1) * interval}s)")
            return rc, out, e, code
        if time.monotonic() >= deadline:
            return rc, out, e, code
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Environment / identity
# ---------------------------------------------------------------------------

def require_supported_os():
    """Fail closed on an unsupported OS with the same honest message brain_doctor uses.
    Windows and Linux are supported; macOS is not yet implemented."""
    if not (_IS_WINDOWS or _IS_LINUX):
        die(f"{Path(__file__).name} supports Windows and Linux only; detected "
            f"sys.platform={sys.platform!r} (macOS is not yet supported).")


def require_admin():
    # OS-forced (privilege check). Linux: root via sudo; Windows: Administrator token.
    if _IS_LINUX:
        if os.geteuid() != 0:
            die(f"{Path(__file__).name} must run as root (re-run under sudo).")
        ok("running as root")
        return
    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        is_admin = False
    if not is_admin:
        die(f"{Path(__file__).name} must run elevated (Administrator).\n"
            "    Launch an elevated console and re-run.")
    ok("running elevated")


def run_as_brain_argv(run_as_brain, brain, cmd, *, wsl=True, root=False):
    """OS-forced IDENTITY SWITCH (NOTE 001-5): build the argv that runs `cmd` as the brain.

    Windows — reproduces today's staged-bridge call byte-for-byte: pipe `cmd` through the
    per-brain `run_as_brain.py`, which enters the `brain-<brain>` WSL distro (or the brain's
    Windows account when wsl=False) and space-joins the post-`--` args into one `bash -lc`.
    `cmd` may be a single command string (the usual case) or a pre-split token list.

    Linux — there is no distro; the brain's rootless Docker runs directly as the brain user, so
    the switch is `sudo -u <brain> -H bash -lc <cmd>`. The `-H` + login shell are load-bearing:
    they resolve the rootless-Docker env (DOCKER_HOST / XDG_RUNTIME_DIR). Unlike the Windows
    bridge, sudo glues no identity banner onto stdout — but the shared `_http_code` leading-digit
    parse already tolerates both, so capture sites need no change. `cmd` is joined to one string.

    root=True (run as ROOT in the brain runtime — Windows data-seam mount/umount/chown) has NO
    'run as brain' analog on Linux: that work is a systemd .mount + direct root chown done by the
    already-root deployer (Section 4 seam). So on Linux this REFUSES root=True rather than
    fabricate an argv — the caller must branch to the seam mechanism, not this identity switch."""
    if _IS_LINUX:
        if root:
            raise RuntimeError(
                "run_as_brain_argv(root=True) has no Linux analog — Linux root seam ops use a "
                "systemd .mount + direct chown (Section 4), not an identity switch into a runtime.")
        payload = cmd if isinstance(cmd, str) else " ".join(cmd)
        return ["sudo", "-u", brain, "-H", "bash", "-lc", payload]
    argv = [sys.executable, str(run_as_brain), "--brain", brain]
    if wsl:
        argv.append("--wsl")
    if root:
        argv.append("--root")
    argv.append("--")
    argv.extend([cmd] if isinstance(cmd, str) else list(cmd))
    return argv


def validate_brain_name(name):
    if not BRAIN_NAME_RE.match(name):
        die(f'invalid brain name "{name}" — must match ^[a-z][a-z0-9_]{{1,19}}$ '
            "(lowercase start, then 1-19 lowercase letters/digits/underscores).")


def user_exists(brain):
    # OS-forced (account probe). Linux: the brain is an OS user (`id`); Windows: a local account.
    if _IS_LINUX:
        rc, _, _ = run_out(["id", brain])
        return rc == 0
    rc, out, _ = run_out(["powershell", "-NonInteractive", "-Command",
                          f'Get-LocalUser -Name "{brain}" -ErrorAction SilentlyContinue'])
    return bool(out.strip())


def is_provider_host():
    """True when this deploy is running inside a Horizon.AIOS install — $HORIZON_ROOT is set and
    a real directory. The platform then provisions the OS account through its own
    $HORIZON_SBIN/horizon_aios_create_brain.py rather than the factory's standalone script."""
    return HORIZON_ROOT is not None and HORIZON_ROOT.is_dir()


def _export_provider_keyring_seam():
    """On a Horizon.AIOS provider host, advertise the platform's keystore namespace to child
    processes (notably run_as_brain.py) via the unbranded $BRAIN_KEYRING_SERVICE seam, so they
    resolve the platform-written password ('horizon_aios' / 'brain_account:<brain>') instead of
    falling back to a stale brain-owned entry. run_as_brain stays platform-agnostic; we supply
    the value. setdefault: an already-set seam (a differently-branded host) wins."""
    if is_provider_host():
        os.environ.setdefault("BRAIN_KEYRING_SERVICE", "horizon_aios")
        os.environ.setdefault("BRAIN_KEYRING_USER", "brain_account:{brain}")


INSTALL_ROOT_ENV = "AIOS_INSTALL_ROOT"


def resolve_install_root(args):
    """The directory that contains brains/<brain>/. EXPLICIT OR NOTHING.

    Precedence: --install-root → $AIOS_INSTALL_ROOT → $HORIZON_ROOT (Horizon.AIOS install) → die.
    Outside a Horizon.AIOS install an unset install root is a USAGE ERROR, not something to
    guess at.

    This used to walk up six levels looking for a directory with a brains/ subdir and
    then fall back to a fixed offset from this file. That walk is what silently bound a
    clone to whatever tree it happened to be unpacked inside — it would find an
    unrelated ancestor and deploy a live brain into it, which is a very expensive way to
    learn where your brains/ dir is. Guessing a destructive destination is never better
    than asking."""
    if getattr(args, "install_root", None):
        return Path(os.path.abspath(args.install_root))
    env_root = os.environ.get(INSTALL_ROOT_ENV)
    if env_root:
        if not os.path.isdir(env_root):
            die(f"${INSTALL_ROOT_ENV} is set to {env_root!r}, which is not a directory.\n"
                f"    Point it at the dir that holds (or will hold) brains/<brain>/, or pass\n"
                f"    --install-root <dir> explicitly.")
        return Path(os.path.abspath(env_root))
    if HORIZON_ROOT is not None and HORIZON_ROOT.is_dir():
        # Inside a Horizon.AIOS install: default to $HORIZON_ROOT so the brain lands at
        # $HORIZON_ROOT/brains/<brain> (== $HORIZON_BRAIN_ROOT/<brain>). An explicit
        # --install-root or $AIOS_INSTALL_ROOT above still overrides this.
        return HORIZON_ROOT
    die("no install root: pass --install-root <dir> or set "
        f"${INSTALL_ROOT_ENV}.\n"
        "    That is the dir that holds brains/<brain>/ — the brain is deployed to\n"
        "    <install-root>/brains/<brain>/. It is not guessed and has no default:\n"
        "    this deploy creates an OS account and writes a multi-GB VM disk, so the\n"
        "    destination is yours to name.\n"
        f'    e.g.  --install-root C:\\brains      or  set {INSTALL_ROOT_ENV}=C:\\brains')


def brain_paths(args):
    root = resolve_install_root(args)
    brain_dir = root / "brains" / args.brain
    return root, brain_dir


# ---------------------------------------------------------------------------
# Distro / residency naming (canon: DEPLOYMENT.md)
# ---------------------------------------------------------------------------

def distro_name(brain):       return f"brain-{brain}"
def build_distro_name(brain): return f"brain-build-{brain}"      # throwaway scratch for from-scratch builds
def residency_task(brain):    return f"{brain}-docker-keepalive"


def distro_exists(brain):
    # `wsl -l -q` lists the current user's distros. Under elevation this is the
    # admin session, so a brain-owned distro may not appear here — treat a match
    # as authoritative-present, absence as "unknown/not in this session".
    rc, out, _ = run_out(["wsl", "-l", "-q"])
    if rc != 0:
        return False
    names = [ln.strip().replace("\x00", "") for ln in out.splitlines()]
    return distro_name(brain) in names


def distro_imported_as_brain(args):
    """Confirm the brain's per-user WSL distro brain-<brain> imported AND is launchable,
    checked IN THE BRAIN'S OWN CONTEXT — not this elevated admin/owner session.

    The distro is registered per-user under the brain's Windows account (see
    brain_installer_2_brain.py) and is deliberately invisible to any other logon, so
    `wsl -l -q` run HERE would NOT list it even on a fully successful deploy — using
    that as the gate would false-negate every good run. We instead probe through the
    staged run_as_brain, which becomes the brain and runs `wsl -d brain-<brain> -- true`:
    rc 0 iff the distro exists and boots. Absence/boot-failure → nonzero, which is the
    real stage-5 failure a false-green would otherwise hide."""
    _, brain_dir = brain_paths(args)
    run_as_brain = brain_dir / "system" / "brain_sbin" / "run_as_brain.py"
    if not run_as_brain.is_file():
        die(f"run_as_brain not staged: {run_as_brain} — cannot confirm the distro imported.")
    rc, _, _ = run_out([sys.executable, str(run_as_brain), "--brain", args.brain,
                        "--wsl", "--", "true"])
    return rc == 0


def residency_task_exists(brain):
    rc, _, _ = run_out(["schtasks", "/query", "/tn", residency_task(brain)])
    return rc == 0


def residency_task_running(brain):
    """Return (exists, running, last_result) for the residency task.

    'Running' is the whole point of residency: the keepalive holds the WSL distro resident so
    the gateway survives idle-shutdown/reboot. A forever-holding keepalive reports Last Result
    267009 (0x41301, "currently running") — the HEALTHY state, not an error. A task that exists
    but is NOT running means the keepalive died (or never launched — see SeBatchLogonRight):
    the stack may answer right now but the distro will idle down and the gateway vanish. That
    exists-but-not-running case is precisely the false-green the verify gate must catch."""
    rc, out, _ = run_out(["schtasks", "/query", "/tn", residency_task(brain), "/v", "/fo", "LIST"])
    if rc != 0:
        return (False, False, None)
    status = last = None
    for line in out.splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("status:"):
            status = s.split(":", 1)[1].strip()
        elif low.startswith("last result:"):
            last = s.split(":", 1)[1].strip()
    return (True, (status or "").lower() == "running", last)


# ---------------------------------------------------------------------------
# Stage: preflight
# ---------------------------------------------------------------------------

def preflight(args):
    """Verify the host can host a brain; fails loud with remedies.

    It also MUTATES the host (see _reset_wsl_host_network): it cycles the WslService +
    vmcompute + hns stack, because a previous teardown/unregister leaves stale mirrored-network
    state on the host that makes EVERY subsequent VM boot loopback-only. Done here, before
    stage 4 builds in a VM that needs the network to pull Debian/apt/models and long before
    stage 5 boots the runtime VM — a later reset would come after the damage.

    Deploy does not assume teardown did it: an older teardown, a fresh clone, or a hand-run
    `wsl --unregister` all leave the same wreckage.
    """
    require_admin()

    # ⚠️ BLAST RADIUS: this stops ALL Hyper-V compute on the box (every WSL VM, any Windows
    # container), including OTHER brains that are currently serving. Their residency tasks
    # bring them back, but they take an outage.
    _reset_wsl_host_network()

    # brain-runnable Python — the resolver must find an interpreter the BRAIN
    # account can execute (not the admin's per-user Store shim).
    if FACTORY_PY_RESOLVER.is_file():
        rc, out, e = run_out([sys.executable, str(FACTORY_PY_RESOLVER), "preflight"])
        if rc != 0:
            die("brain-runnable Python preflight FAILED — the brain account needs a\n"
                "    machine-wide Python it can execute. Install Python for all users\n"
                f"    (system-wide), then re-run.\n    resolver said:\n{out}{e}")
        ok("brain-runnable Python present")
    else:
        warn(f"python resolver not found at {FACTORY_PY_RESOLVER} — skipping that check")

    # keyring importable (FINDING 5: no silent getpass hang on a TTY-less deploy).
    rc, _, _ = run_out([sys.executable, "-c", "import keyring"])
    if rc != 0:
        die("`keyring` is not importable by this Python — the account password\n"
            "    cannot be stored/retrieved for runas / residency, and a TTY-less\n"
            "    deploy would hang. Fix: pip install keyring, then re-run.")
    ok("keyring importable")

    # WSL2 present.
    rc, out, _ = run_out(["wsl", "--status"])
    if rc != 0:
        die("WSL2 not available. Install it (wsl --install), reboot if prompted,\n"
            "    then re-run. The brain engine runs in a WSL2 distro.")
    ok("WSL2 present")

    ok("preflight passed")


# ---------------------------------------------------------------------------
# Stage: create brain (provider seam)
# ---------------------------------------------------------------------------

def create_brain(args):
    """Provision the OS account/group/folder. Provider host → provider script; else the
    factory standalone create_brain.py. Idempotent: skip if the account exists.

    The standalone script is not a fallback for exotic hosts — it is the DEFAULT path.
    A host that advertises no provider is the normal case (it is what a clone of this repo
    on someone else's machine looks like), so its absence is a broken checkout, not a
    reason to look for a platform to borrow."""
    if user_exists(args.brain):
        ok(f'account "{args.brain}" already exists — skipping create-brain')
    else:
        root, _ = brain_paths(args)
        if is_provider_host():
            info(f"provider host — using {PROVIDER_CREATE_BRAIN}")
            # Request the 'scheduled' automation tier: this deploy registers a boot residency
            # (the docker keepalive scheduled task, stage 8), which needs the brain to hold
            # "Log on as a batch job" (SeBatchLogonRight). The platform create-brain grants it
            # under --automation scheduled; without this it defaults to 'none' and schtasks
            # later fails with a MISLEADING "user name or password is incorrect".
            # (No -v: the provider script's verbose contract is not ours to assume.)
            run([sys.executable, str(PROVIDER_CREATE_BRAIN), args.brain,
                 "--automation", "scheduled"])
        else:
            if not STANDALONE_CREATE_BRAIN.is_file():
                die(f"standalone create_brain.py not found: {STANDALONE_CREATE_BRAIN}\n"
                    f"    This host is not a Horizon.AIOS install (${_HORIZON_ROOT_ENV} is unset),\n"
                    "    so the factory's own standalone provisioner is what creates the OS\n"
                    "    account — and it is missing from this checkout. Nothing can be\n"
                    "    provisioned without it.")
            info(f"no provider — using standalone {STANDALONE_CREATE_BRAIN.name}")
            # -v propagates into the standalone (ours, so its argv contract is known):
            # it prints the same prefixes as this script, and an unattributed line from
            # a subprocess is what makes the two indistinguishable in the transcript.
            run([sys.executable, str(STANDALONE_CREATE_BRAIN), args.brain,
                 "--install-root", str(root)] + (["-v"] if _VERBOSE else []))

        if not user_exists(args.brain):
            die("create-brain ran but the account still does not exist — see output above.")
        ok(f'account "{args.brain}" provisioned')

    # GAP B: seed the brain account's per-user .wslconfig (networkingMode=mirrored) so the
    # brain's own WSL2 VM boots mirrored, not NAT. Done here — after the account + its profile
    # dir exist (both create_brain scripts materialize C:\Users\<brain>) and BEFORE the distro
    # is first booted in deploy_engine — so mirrored is in effect on the first real boot. Runs
    # unconditionally (including the account-already-exists redeploy path) because it is
    # idempotent and must be ensured every deploy, not only on first provision.
    _, _brain_dir = brain_paths(args)
    write_brain_wslconfig(args.brain, brain_etc_wsl=_brain_dir / "brain_etc" / "wsl")

    # WS1 tail B: create_brain's ~/.claude redirect DEFERS on a first provision (it runs before the
    # profile-loading logon). write_brain_wslconfig just forced that logon + resolved the profile,
    # so (re)establish the ~/.claude symlink now — within the FIRST deploy, at the resolved path.
    # Idempotent (a redeploy re-links harmlessly).
    link_brain_home_claude(args.brain, _brain_dir)


# ---------------------------------------------------------------------------
# Brain-account .wslconfig (GAP B — per-Windows-user mirrored networking)
# ---------------------------------------------------------------------------

# Credential namespaces in the ONE OS-native keystore — MUST stay compatible with
# run_as_brain.py's _keyring_namespaces(). Order = lookup precedence (first hit wins).
# Read-only here: we NEVER prompt (a headless elevated deploy must not block on getpass), so
# a miss just means "no forced logon" and profile resolution degrades to whatever ProfileList
# already knows.
#
# A Horizon.AIOS provider host's create-brain (horizon_aios_create_brain.py) writes the account
# password to the platform namespace (service 'horizon_aios', user 'brain_account:<brain>') —
# prefer it. An unbranded host may instead advertise its namespace via $BRAIN_KEYRING_SERVICE
# (+ optional $BRAIN_KEYRING_USER). The brain-owned 'brain:<brain>'/account_password namespace
# (the standalone factory create_brain) is always tried LAST — a stale entry there (e.g. left by
# a prior standalone incarnation) must never shadow the platform's current credential.
def _keyring_namespaces():
    ns = []
    host_service = os.environ.get("BRAIN_KEYRING_SERVICE")
    if host_service:
        ns.append((host_service, os.environ.get("BRAIN_KEYRING_USER", "brain_account:{brain}")))
    elif is_provider_host():
        ns.append(("horizon_aios", "brain_account:{brain}"))   # Horizon.AIOS platform namespace
    ns.append(("brain:{brain}", "account_password"))           # standalone / brain-owned namespace
    return tuple(ns)


def _brain_password(brain):
    """Read the brain's Windows account password from the OS keystore, or None. No prompt."""
    try:
        import keyring
    except ImportError:
        return None
    for service_tmpl, user_tmpl in _keyring_namespaces():
        try:
            # BOTH halves are templates — formatting only the user half asked the vault
            # for a service literally named "brain:{brain}", braces included, which can
            # never hit for any brain. The miss is warn-only (no forced logon → no
            # ProfileImagePath → no .wslconfig), so it read as "no password stored"
            # rather than "wrong question asked".
            pw = keyring.get_password(service_tmpl.format(brain=brain),
                                      user_tmpl.format(brain=brain))
        except Exception:
            pw = None
        if pw:
            return pw
    return None


# PowerShell that performs ONE LOGON_WITH_PROFILE for the brain: a throwaway child
# (`cmd /c ver`) started via CreateProcessWithLogonW with LoadUserProfile=$true (the .NET
# ProcessStartInfo.LoadUserProfile flag sets LOGON_WITH_PROFILE). The child's output is
# irrelevant — the SIDE EFFECT is the point: Windows materializes the account's real profile
# directory AND writes ProfileList\<SID>\ProfileImagePath. UseShellExecute=$false is required
# to pass a credential. Inputs arrive via env (BRAIN_USER/BRAIN_PW) so the password never
# touches argv (nothing logs it).
_PROFILE_LOAD_PS = r"""
$ErrorActionPreference = 'Stop'
$si = New-Object System.Diagnostics.ProcessStartInfo
$si.FileName         = "$env:SystemRoot\System32\cmd.exe"
$si.Arguments        = '/c ver'
$si.UseShellExecute  = $false
$si.CreateNoWindow   = $true
$si.UserName         = $env:BRAIN_USER
$si.Password         = (ConvertTo-SecureString $env:BRAIN_PW -AsPlainText -Force)
$si.LoadUserProfile  = $true
$si.WorkingDirectory = $env:SystemRoot
$p = New-Object System.Diagnostics.Process
$p.StartInfo = $si
[void]$p.Start()
$p.WaitForExit()
exit $p.ExitCode
"""


def _load_brain_profile(brain):
    """Force a first LOGON_WITH_PROFILE for the brain so Windows materializes its REAL profile
    dir + ProfileList entry. Idempotent: a re-logon just re-loads an existing profile. This is
    what produces a CLEAN, UNSUFFIXED profile — Windows only appends the `.<MACHINE>`/`.NNN`
    suffix when C:\\Users\\<brain> is already squatted by an unregistered dir at logon time, which
    is exactly why nothing must pre-create that path (see create_brain phase 5). Returns True on
    a successful profile-loading logon, else False (warn-only — resolution then falls back to an
    already-materialized ProfileList entry, if any)."""
    pw = _brain_password(brain)
    if not pw:
        warn(f"no keystore credential for '{brain}' — cannot force a profile-loading logon; "
             "profile resolution will rely on an already-materialized ProfileList entry")
        return False
    env = dict(os.environ, BRAIN_USER=brain, BRAIN_PW=pw)
    p = subprocess.run(["powershell", "-NonInteractive", "-NoProfile", "-Command",
                        _PROFILE_LOAD_PS], env=env, capture_output=True, text=True)
    if p.returncode != 0:
        warn(f"profile-loading logon for '{brain}' failed (rc={p.returncode}): "
             f"{(p.stderr or p.stdout).strip()}")
        return False
    return True


def _brain_sid(brain):
    """Resolve the brain account's SID via Get-LocalUser, or None."""
    rc, out, _ = run_out(["powershell", "-NonInteractive", "-NoProfile", "-Command",
                          f'(Get-LocalUser -Name "{brain}").SID.Value'])
    sid = (out or "").strip()
    return sid or None


def _profile_image_path(sid):
    """Read ProfileList\\<SID>\\ProfileImagePath (the authoritative profile path) for a SID, or
    None. ProfileImagePath is a REG_EXPAND_SZ (may hold %SystemDrive%\\Users\\… on some hosts) —
    expand it so the caller gets a concrete path."""
    key = r"HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList" + "\\" + sid
    ps = ("$v=(Get-ItemProperty -Path '" + key + "' -Name ProfileImagePath "
          "-ErrorAction SilentlyContinue).ProfileImagePath; "
          "if ($v) { [Environment]::ExpandEnvironmentVariables($v) }")
    rc, out, _ = run_out(["powershell", "-NonInteractive", "-NoProfile", "-Command", ps])
    path = (out or "").strip()
    return path or None


def _profilelist_entry_exists(sid):
    """Whether ProfileList\\<SID> still exists. Remove-LocalUser does not remove the row, and a
    row left behind — even with its directory already gone — is enough to make the next
    create-brain mint a fresh SID and a <name>.NNN profile."""
    key = r"HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList" + "\\" + sid
    rc, out, _ = run_out(["powershell", "-NonInteractive", "-NoProfile", "-Command",
                          "if (Test-Path '" + key + "') { 'yes' }"])
    return (out or "").strip() == "yes"


def brain_profile_dir(brain):
    """Resolve the brain account's REAL Windows profile directory — the %UserProfile% its OWN
    per-user WSL2 VM reads `.wslconfig` from (NOT the operator's, NOT a string-built path).

    The profile path is AUTHORITATIVE only in the registry: after a profile-loading logon Windows
    records it at
        HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\ProfileList\\<SID>\\ProfileImagePath
    We must NEVER string-build C:\\Users\\<brain>: if a stale/leftover dir squats that name the real
    profile is C:\\Users\\<brain>.<MACHINE> (or .NNN), and a `.wslconfig` written to the plain path is
    silently IGNORED by WSL — which reads %UserProfile% = the registry path (this was the GAP-B
    doc-lie; see NOTE 001-33).

    Steps: (1) force a first LOGON_WITH_PROFILE so the profile + ProfileList entry exist;
    (2) get the account SID (Get-LocalUser); (3) read ProfileImagePath for that SID. Returns a
    Path, or None when the profile cannot be resolved — the caller then warns + SKIPS rather than
    falling back to a string-built path that WSL never reads."""
    _load_brain_profile(brain)   # side effect: materialize profile + ProfileList (idempotent)
    sid = _brain_sid(brain)
    if not sid:
        warn(f"could not resolve SID for '{brain}' (Get-LocalUser) — cannot locate its real "
             "profile dir; .wslconfig placement is impossible")
        return None
    image = _profile_image_path(sid)
    if not image:
        warn(f"no ProfileImagePath for SID {sid} ('{brain}') in ProfileList — the profile has "
             "not been materialized by a logon; cannot locate its real profile dir")
        return None
    return Path(image)


def _grant_brain_read(path, brain):
    """Ensure the brain account can READ path. Mirrors create_brain.py's icacls /grant
    pattern. The brain normally owns its own profile (so it can read there by default),
    but an explicit read grant guarantees it regardless of how the profile dir was
    materialized. Warn-only — a failed grant must not fail the deploy."""
    p = subprocess.run(["icacls", str(path), "/grant", f"{brain}:R"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        warn(f"could not grant {brain} read on {path} (rc={p.returncode}): "
             f"{(p.stderr or p.stdout).strip()}")


def _link_wslconfig_visibility(brain_etc_wsl, resolved_cfg):
    """Surface the brain's canonical .wslconfig (which physically lives at the RESOLVED
    %UserProfile%\\.wslconfig) inside the brain's own config tree as a symlink, so an operator
    browsing brain_etc/wsl/ can see it. VISIBILITY ONLY — non-critical, warn on failure. Deploy
    runs elevated, so the symlink privilege is covered."""
    if not brain_etc_wsl:
        return
    link = Path(brain_etc_wsl) / ".wslconfig"
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        warn(f"could not create {link.parent} for the .wslconfig visibility symlink "
             f"(non-critical): {e}")
        return
    # Replace an existing link/file as a reparse point (delete the LINK, never follow it).
    if link.is_symlink() or link.exists():
        subprocess.run(["cmd", "/c", "del", "/q", str(link)], capture_output=True, text=True)
    r = subprocess.run(["cmd", "/c", "mklink", str(link), str(resolved_cfg)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        warn(f"could not symlink {link} -> {resolved_cfg} (visibility only): "
             f"{(r.stderr or r.stdout).strip()}")
    else:
        ok(f"brain_etc/wsl/.wslconfig -> {resolved_cfg} (visibility symlink)")


def write_brain_wslconfig(brain, brain_etc_wsl=None):
    """GAP B: write networkingMode=mirrored into the BRAIN account's REAL %UserProfile%\\.wslconfig.

    .wslconfig is PER-WINDOWS-USER: the brain runs its distro under its OWN Windows account, so
    its VM reads the .wslconfig in the brain's profile, NOT the operator's. The profile path is
    RESOLVED FROM THE REGISTRY (brain_profile_dir) — never string-built C:\\Users\\<brain>, because
    a suffixed profile (C:\\Users\\<brain>.<MACHINE>) makes the plain path a file WSL never reads.
    Without a mirrored .wslconfig at the resolved path the brain VM boots default NAT, so a
    --posture server 0.0.0.0 in-distro bind only surfaces on host 127.0.0.1 and never on the LAN
    (DEPLOYMENT.md / TROUBLESHOOTING.md / NOTE 001-31 / NOTE 001-33).

    Idempotent + non-clobbering: if a .wslconfig already exists we PRESERVE any operator-added
    [wsl2] knobs and only ensure networkingMode is present (leaving an existing networkingMode
    untouched); a fresh provision gets the canonical file (with a top-of-file provenance comment).

    Also surfaces the file as a visibility symlink at brain_etc/wsl/.wslconfig when brain_etc_wsl
    is given. (Whether mirrored actually ENGAGES is validated separately at Phase 9 — some hosts
    fall back to NAT with a persistent 0x8007054f; that host issue is NOT this function's concern.
    The tooling's job is only to RESOLVE and WRITE the config correctly.)"""
    profile = brain_profile_dir(brain)
    if profile is None:
        warn("brain profile dir unresolved — .wslconfig NOT written; server-posture LAN reach "
             "needs networkingMode=mirrored at the brain's real %UserProfile%\\.wslconfig "
             "(DEPLOYMENT.md / NOTE 001-33)")
        return
    cfg = profile / ".wslconfig"
    # The resolved profile dir already exists (the logon materialized it); this guard is just an
    # edge-case safety. This is the RESOLVED real dir, never a string-built C:\Users\<brain>.
    try:
        profile.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        warn(f"resolved brain profile dir {profile} not writable: {e} — .wslconfig not written; "
             "server-posture LAN reach needs networkingMode=mirrored there (DEPLOYMENT.md gap 1)")
        return

    # Top-of-file provenance comment (surfaced through the brain_etc/wsl/.wslconfig symlink).
    header = "# canonical location is %USERPROFILE%\\.wslconfig; surfaced here via symlink\n"
    body = "[wsl2]\nnetworkingMode=mirrored\n"
    wrote = True
    if cfg.is_file():
        try:
            existing = cfg.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        # Already sets networkingMode? Leave the file untouched (never clobber operator knobs).
        if re.search(r"(?im)^\s*networkingMode\s*=", existing):
            ok(f"brain .wslconfig already sets networkingMode ({cfg}) — left as-is")
            wrote = False
            content = None
        else:
            # File exists WITHOUT networkingMode — add it under [wsl2], preserving everything else
            # (do NOT inject the provenance header into an operator-authored file).
            text = existing if (existing == "" or existing.endswith("\n")) else existing + "\n"
            if re.search(r"(?im)^[ \t]*\[wsl2\][ \t]*$", text):
                content = re.sub(r"(?im)^([ \t]*\[wsl2\][ \t]*)$",
                                 r"\1\nnetworkingMode=mirrored", text, count=1)
            else:
                content = text + body
    else:
        content = header + body

    if content is not None:
        try:
            cfg.write_text(content, encoding="utf-8")
        except OSError as e:
            warn(f"could not write {cfg}: {e} — server-posture LAN reach needs "
                 "networkingMode=mirrored there (DEPLOYMENT.md gap 1)")
            return
        ok(f"brain .wslconfig written: {cfg} (networkingMode=mirrored)")

    _grant_brain_read(cfg, brain)
    _link_wslconfig_visibility(brain_etc_wsl, cfg)


def link_brain_home_claude(brain, brain_dir):
    """WS1 tail B: (re)establish the brain's home ~/.claude -> brains/<brain>/.claude symlink at
    the RESOLVED profile path, WITHIN the first deploy.

    Why here (and not only in create_brain): create_brain runs BEFORE the profile-loading logon,
    so its ~/.claude redirect is GATED on os.path.isdir(C:\\Users\\<brain>) and DEFERS on a first
    provision (the profile isn't materialized yet) — leaving ~/.claude unlinked until a redeploy
    (its CLAUDE.md/settings/skills unsurfaced). By the time deploy reaches this point,
    write_brain_wslconfig has already forced the LOGON_WITH_PROFILE, so brain_profile_dir resolves
    the REAL (possibly suffixed) profile and we link there — never a string-built C:\\Users\\<brain>.

    Idempotent + reparse-safe: an existing correct/incorrect link is deleted as a reparse point
    (rmdir, never followed); a real non-empty ~/.claude makes the rmdir fail safely and the mklink
    is skipped. None-guard: unresolved profile → warn + defer to the next redeploy (no guess-link).
    """
    profile = brain_profile_dir(brain)
    if profile is None:
        warn("brain profile unresolved — ~/.claude redirect deferred to the next redeploy "
             "(needs networkingMode/CLAUDE.md surfaced at the resolved %UserProfile%)")
        return
    brain_claude = Path(brain_dir) / ".claude"
    if not brain_claude.is_dir():
        warn(f"{brain_claude} missing — cannot redirect ~/.claude (create-brain incomplete?)")
        return
    home_claude = profile / ".claude"
    # Reparse-safe delete: remove only a symlink/empty dir WITHOUT following it (a real, non-empty
    # ~/.claude makes rmdir fail safely, leaving operator data intact).
    if home_claude.is_symlink() or home_claude.exists():
        subprocess.run(["cmd", "/c", "rmdir", str(home_claude)], capture_output=True, text=True)
    r = subprocess.run(["cmd", "/c", "mklink", "/D", str(home_claude), str(brain_claude)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        warn(f"could not link {home_claude} -> {brain_claude} "
             f"({(r.stderr or r.stdout).strip()}) — surfaced on the next redeploy")
    else:
        ok(f"~/.claude -> {brain_claude} (at resolved profile {profile})")


# ---------------------------------------------------------------------------
# Stage: stage the code
# ---------------------------------------------------------------------------

# Brain-relative homes for RUNTIME state (ADR-0019 amended: runtime moved under system/).
# Kept as constants, not literals: the 2026-07-13 `wsl` → `wsl_engine` rename reached one
# staging path's exclude list and not the other's, and the two disagreed silently for two
# days. One-to-one staging retires the second list; the constants stay.
WSL_RUNTIME_REL   = ("system", "wsl_engine")    # live distro: disk/, _build/, residency_task.xml
DEPLOY_LOGS_REL   = ("system", "deploy_logs")   # host-side phase logs (<date>_deploy_phase*.log)
ENGINE_EXPORT_DIR = "wsl_engine_export"         # brain ROOT; EXISTS ONLY under --export-engine


def wsl_runtime_dir(brain_dir):
    """<brain>/system/wsl_engine — the live WSL distro workspace."""
    return Path(brain_dir).joinpath(*WSL_RUNTIME_REL)


def deploy_logs_dir(brain_dir):
    """<brain>/system/deploy_logs — host-side deploy phase logs, one dir for every phase."""
    return Path(brain_dir).joinpath(*DEPLOY_LOGS_REL)


# ---------------------------------------------------------------------------
# WHAT THE COPY MAY TOUCH — path-scoped, not member-scoped
#
# Staging is one copytree(SOURCE_ROOT, brain_dir). The tables below are the only things
# that bound it. All paths are BRAIN-RELATIVE and matched as exact paths, never as bare
# names: "knowledge" and "knowledge/brain_rw" are opposite rules — one is refreshed, one is
# the brain's data — and a bare name would apply the wrong one at the wrong depth. That is
# not hypothetical: the member-scoped predicate this replaces tested bare names, so it had
# to blanket-protect "knowledge" to keep brain_rw safe, and brain_ro therefore never
# received a fix after first deploy.
# ---------------------------------------------------------------------------

# NEVER OVERWRITE. The brain's own data + the live engine. Skipped only when they ALREADY
# EXIST, so a first deploy still lays the empty scaffold down and a redeploy never touches it.
#
#   knowledge/brain_rw  the brain WRITES this. It holds chroma — a LIVE VECTOR STORE, and
#                       from stage 6 on, the DATA DOOR: brain_truths.py:163 links
#                       knowledge/brain_rw/chroma to \\wsl.localhost\<distro>\... . Copying
#                       over it, or letting a recursive perm pass follow it, reaches through
#                       into the running distro's 9p share. There is no undo for either.
#   skills/brain_rw     the brain writes its own skills here.
#   system/wsl_engine   the live distro disk. (source/ has none — this is belt-and-braces.)
_STAGE_PROTECT = ("system/wsl_engine", "knowledge/brain_rw", "skills/brain_rw")

# ALWAYS OVERWRITE. Strictly factory-owned: the deployer is the only writer, so a redeploy
# must ship fixes here rather than silently keep first-deploy content. Nothing enforces this
# on the host (see brain_security_model.md / the ro-rw split) — it is a staging invariant,
# which is exactly why it is written down. Listed for the reader and asserted at staging time
# (_assert_zone_tables): the copy overwrites by default, so these are simply never protected.
_STAGE_REFRESH = ("knowledge/brain_ro", "skills/brain_ro")

# Dirs that hold a DATA DOOR — a `mklink /D` reparse point into the RUNNING distro
# (brain_truths.py:163-164: knowledge/brain_rw/chroma and system/brain_logs/live).
# `icacls /reset /T` FOLLOWS a reparse point, so no recursive perm pass may ever be rooted
# at or above one of these. This is why _reset_targets() descends instead of sweeping tops.
_DOOR_PARENTS = ("knowledge/brain_rw", "system/brain_logs")

# EXCLUDES — never carry runtime dirs, build cruft, or secrets into a brain. A gitignore-clean
# checkout still carries certs/ and *.pem/*.key on a machine that has ever run the gateway
# locally, so this filter is load-bearing, not decorative. The *.example TEMPLATES
# (cert.pem.example, .env.example, *.map.example) are NOT matched here, so they ship.
_CODE_EXCL_DIRS    = {"wsl_engine", ENGINE_EXPORT_DIR, "chroma_store", "__pycache__",
                      "certs", ".transient"}
_CODE_EXCL_SUFFIX  = (".pyc", ".pem", ".key")          # real key material (…​.example is kept)

# Repo furniture: real at the SOURCE ROOT ONLY, never staged into a brain. Matched at depth 0
# so a brain-legitimate dir of the same name deeper in the tree is unaffected.
_SOURCE_ONLY_ROOT  = ("policy_templates",)


def _is_example(name):
    return name.endswith(".example")


def _rel_posix(src_dir, name):
    """Brain-relative path of <name> inside source dir <src_dir>, as posix.
    At the source root this is just <name> (pathlib drops the '.' component)."""
    return (Path(src_dir).relative_to(SOURCE_ROOT) / name).as_posix()


def _is_protected(rel, brain_dir):
    """True if <rel> is protected AND already exists in the brain — the only case in which
    the copy must keep its hands off. First deploy: nothing exists, everything is staged."""
    return rel in _STAGE_PROTECT and (Path(brain_dir) / rel).exists()


def _assert_zone_tables():
    """The ro/rw tables are a security boundary; a mistake in one of them is a silent
    clobber of the brain's data (protect) or a brain that never receives fixes (refresh).
    Nothing downstream would notice either, so check them here, before the copy, while it
    is still cheap.

    The tables name paths, and paths get renamed. When a zone dir is renamed in source/ and
    the table is not, the entry stops matching anything and simply stops applying — no
    error, no clobber, until the day it matters. That already happened once on this code
    (`wsl` → `wsl_engine`), so REFRESH entries are checked against the tree that is about
    to be copied. PROTECT entries are deliberately NOT: they name runtime state (the live
    engine, the vector store) that source/ correctly does not carry."""
    both = set(_STAGE_PROTECT) & set(_STAGE_REFRESH)
    if both:
        die(f"staging tables disagree: {sorted(both)} listed as BOTH protected and "
            "refreshed — refusing to guess which the brain's data deserves.")
    for rel in _STAGE_PROTECT + _STAGE_REFRESH + _DOOR_PARENTS:
        if rel != rel.strip("/") or "\\" in rel:
            die(f"staging table entry must be a clean brain-relative posix path: {rel!r}")
    for rel in _STAGE_REFRESH:
        if not (SOURCE_ROOT / rel).exists():
            die(f"_STAGE_REFRESH names {rel!r}, which does not exist in {SOURCE_ROOT}.\n"
                "    Either the zone was renamed and the table was not, or the checkout is\n"
                "    incomplete. A refresh zone that matches nothing silently ships no fixes.")


# Advisory only — icacls localizes this line. Never treat its ABSENCE as failure.
_ICACLS_TRAILER = re.compile(r"Failed processing (\d+) ", re.IGNORECASE)


def _icacls_or_die(argv, what):
    """Run icacls and DIE unless it succeeded. Callers pass argv WITHOUT /C — read on.

    Fatal, not a warning, on purpose. Every caller is establishing a permission that
    something downstream assumes is in place; a deploy that prints green while the locks
    silently did not apply is the failure mode this layer exists to prevent.

    /C ("continue on errors") MUST NOT be used with this, and that is the whole trick:
    it suppresses the exit code. Measured on this host —

        icacls C:\\does\\not\\exist /reset /T        → rc 3   (failure reported)
        icacls C:\\does\\not\\exist /reset /T /C     → rc 0   (failure NOT reported)

    — so the old `/C` + unchecked-rc pairing could not have detected a total failure even
    if someone had added the rc check, which is the obvious fix and a useless one. Dropping
    /C makes rc authoritative, and rc is also LOCALE-INDEPENDENT: parsing icacls' English
    "Failed processing N files" trailer would hand a non-English Windows a deploy that dies
    on success. That trailer is read here only as extra detail when it happens to be
    English, never as the verdict."""
    p = subprocess.run(argv, capture_output=True, text=True)
    out = f"{p.stdout or ''}\n{p.stderr or ''}".strip()
    m = _ICACLS_TRAILER.search(out)
    failed = int(m.group(1)) if m else 0
    if p.returncode != 0 or failed:
        die(f"{what} FAILED — the brain's permissions are not what this deploy claims.\n"
            f"    command : {' '.join(argv)}\n"
            f"    rc      : {p.returncode}"
            + (f"  (files failed: {failed})" if failed else "")
            + f"\n    output  : {out or '(none)'}")


def _repair_staged_acls(brain_dir, targets):
    """ACL REPAIR (Windows): make every path we just staged RE-INHERIT the brain-folder
    root, which create_brain set to brain:(OI)(CI)F.

    A restrictive PROTECTED DACL on staged code (SYSTEM/Administrators/OWNER, no brain)
    locks the brain out of its OWN code, so phase 2 — which runs AS the brain — hits
    "[Errno 13] Permission denied" and `wsl --import` never fires. copytree inherits the
    destination's ACL and should not reproduce that, but the reset is cheap, idempotent,
    and the failure it prevents is expensive.

    TWO PATHS MUST NEVER BE RESET, and neither is theoretical:
      - The brain-folder ROOT. Its broken inheritance is deliberate (create_brain does
        /inheritance:r then grants five explicit principals). A /reset there re-enables
        inheritance from the brains/ parent and re-opens the whole tree. _reset_targets()
        only ever returns paths BELOW the root; the assert below is the backstop.
      - Anything at or above a DATA DOOR (_DOOR_PARENTS). /T follows the reparse point
        onto the live 9p share inside the running distro.

    installer_1's lock_edit_source re-applies the intended read-only Denies after this
    (stage 3 → stage 5) — which is also why this must never run later than stage 3: a
    reset after those Denies would erase them."""
    root = Path(brain_dir).resolve()
    for rel, recursive in targets:
        p = (Path(brain_dir) / rel).resolve()
        if p == root or root not in p.parents:
            die(f"refusing to reset ACLs on {p} — not a proper subpath of {root}. "
                "Resetting the brain-folder root would re-open the whole tree.")
        if not p.exists():
            continue
        # No /C: it would suppress the exit code and with it the only failure signal we
        # get. See _icacls_or_die. Nothing here wants to "continue on error" anyway —
        # a failed ACL repair is the end of the deploy, not a note in the log.
        argv = ["icacls", str(p), "/reset"] + (["/T"] if recursive else [])
        _icacls_or_die(argv, f"ACL repair of staged {rel}")


def stage_package(args):
    """Stage the factory code into brains/<brain>/ WITHOUT clobbering runtime state.

    source/ IS the brain root, so this is a copy of a tree, not an assembly of parts.
    There is no build step, no tarball and no member list: the delivered artifact IS the
    git repo you are running from. Idempotent — re-staging code is safe (code is tier-1);
    the brain's own data is protected by path (_STAGE_PROTECT), not by luck."""
    _, brain_dir = brain_paths(args)
    brain_dir.mkdir(parents=True, exist_ok=True)
    _stage_from_source(brain_dir)
    # Materialize the brain-root context/policy files (CLAUDE/agents/brain_core/invariants)
    # BEFORE installer_1's ACL lock (deploy stage 5), so the brain ships with a real, locked
    # policy instead of nothing. That ordering is load-bearing — see the docstring below.
    seed_brain_context_files(brain_dir, args.brain)


def _make_stage_ignore(brain_dir):
    """The copytree(ignore=) filter. Everything the copy must NOT carry, in one place:
    excluded runtime/build/secret paths, the brain's own protected data, and the source-only
    furniture. Also the last line of defence against staging a populated token map."""
    def _ignore(src, names):
        here = Path(src).resolve()
        if here != SOURCE_ROOT and SOURCE_ROOT not in here.parents:
            die(f"copytree walked outside {SOURCE_ROOT}: {src} — refusing to stage.")
        at_root = here == SOURCE_ROOT
        drop = set()
        for n in names:
            rel = _rel_posix(src, n)
            # The brain's own data + the live engine: present ⇒ never touch. (Absent ⇒ fall
            # through and stage the scaffold, which is what a first deploy needs.)
            if _is_protected(rel, brain_dir):
                info(f"protected, not overwriting: {rel}/")
                drop.add(n)
                continue
            # Seeded, not copied: they need [BRAIN_NAME] substitution and only-if-absent
            # idempotence, both of which a copy would defeat. See seed_brain_context_files().
            if at_root and (n in CONTEXT_FILES or n in _SOURCE_ONLY_ROOT):
                drop.add(n)
                continue
            if n in _CODE_EXCL_DIRS:
                drop.add(n)
            elif n == ".env":
                drop.add(n)                       # live dotenv — only .env.example ships
            elif n.endswith(".map") and not _is_example(n):
                # Empty token-map templates SHIP (the seam seeds the gateway from them);
                # a POPULATED map is a real secret leak — refuse loudly.
                fp = Path(src) / n
                try:
                    populated = any(re.match(r"^[^#\s]", ln)
                                    for ln in fp.read_text(encoding="utf-8",
                                                           errors="ignore").splitlines())
                except OSError:
                    populated = False
                if populated:
                    die(f"refusing to stage a POPULATED token map (secret leak): {fp}")
                # empty template → keep it (not added to drop)
            elif n.endswith(_CODE_EXCL_SUFFIX) and not _is_example(n):
                drop.add(n)
        return drop
    return _ignore


def _holds_something_we_did_not_write(rel, brain_dir):
    """True if anything BELOW <rel> was not written by this copy — a door parent, or a
    protected path the copy skipped. `icacls /T` is indiscriminate: rooted at <rel> it
    sweeps those too. So they are the signal to descend rather than sweep."""
    def below(x):
        return x.startswith(rel + "/")
    return (any(below(d) for d in _DOOR_PARENTS)
            or any(below(p) and _is_protected(p, brain_dir) for p in _STAGE_PROTECT))


def _reset_targets(brain_dir):
    """Which staged paths _repair_staged_acls resets, as (brain-relative path, recursive).

    One rule: RESET EXACTLY WHAT THE COPY WROTE, and never sweep past it. The recursion
    stops as soon as a subtree is wholly ours, and descends wherever sweeping the whole top
    would reach something the copy deliberately did not touch:

      knowledge/  → descend. brain_rw is the brain's data and holds the DATA DOOR into the
                    running distro (brain_truths.py:163). /T on knowledge/ follows it onto
                    the live 9p share. Only brain_ro is ours.
      skills/     → descend once brain_rw exists: the brain's skills are the brain's, in
                    ACL as well as in content. On a first deploy we lay that scaffold down
                    ourselves, so there is nothing yet to preserve and the top is swept.
      system/     → descend. brain_logs holds the LOGS DOOR (system/brain_logs/live) and
                    wsl_engine is the live distro disk; neither is in source/, and both sit
                    below system/, so a /T rooted there reaches both. installer_1 re-grants
                    brain:(OI)(CI)F /T on wsl_engine at stage 5 regardless — so declining to
                    sweep it here costs nothing and removes the door hazard.
      a door parent itself → reset NON-recursively: repair the dir, never enter it.

    Protected paths never appear here: the copy skipped them, so they are not what it
    wrote. Same predicate (_is_protected) the copy uses — one table, one meaning, both
    halves."""
    targets = []

    def walk(src_dir, rel):
        for child in sorted(src_dir.iterdir(), key=lambda p: p.name):
            name = child.name
            crel = f"{rel}/{name}" if rel else name
            if name in _CODE_EXCL_DIRS or _is_protected(crel, brain_dir):
                continue
            if not child.is_dir():
                continue          # files inherit from the dir they land in; the root is never reset
            if not rel and name in _SOURCE_ONLY_ROOT:
                continue
            if crel in _DOOR_PARENTS:
                targets.append((crel, False))
            elif _holds_something_we_did_not_write(crel, brain_dir):
                walk(child, crel)
            else:
                targets.append((crel, True))

    walk(SOURCE_ROOT, "")
    return targets


def _stage_from_source(brain_dir):
    """Copy source/ — a one-to-one image of a brain root — into brains/<brain>/, then fix
    up permissions. Copy the dirs, then apply the perms: one tree in, one tree out.

    The old member list is gone with the tree that needed it. It existed because the
    factory was not shaped like a brain, so staging had to name the parts to assemble;
    once source/ IS shaped like a brain, naming them again only creates a second contract
    to keep in sync — and the last time it drifted, a rename landed in one list and not the
    other and the two staging paths disagreed silently for two days."""
    _assert_zone_tables()
    if not SOURCE_ROOT.is_dir():
        die(f"source tree not found at {SOURCE_ROOT} — this is not a complete checkout "
            "of the factory repo. source/ IS the brain: without it there is nothing to "
            "deploy.")
    shutil.copytree(SOURCE_ROOT, brain_dir, ignore=_make_stage_ignore(brain_dir),
                    dirs_exist_ok=True)
    _repair_staged_acls(brain_dir, _reset_targets(brain_dir))
    ok(f"code staged into {brain_dir} from {SOURCE_ROOT} — one-to-one copy of source/, "
       "no tarball, no build step")


def seed_brain_context_files(brain_dir, brain):
    """Place the four brain-root context/policy files, substituting [BRAIN_NAME] and
    [AIOS_INSTALL_ROOT_PATH]. ONLY-IF-ABSENT: a live brain's tuned policy is never clobbered on
    redeploy. This is the reason they are seeded rather than copied with the rest of the
    tree — copytree would overwrite both the substitution and the operator's edits.

    Read straight from source/. There is no template store above the repo root and no
    policy_templates/ hop to collect them into: source/ is the one place they live, which
    is what that machinery was simulating. A clone by a stranger has everything it needs.

    ORDERING — this MUST run at stage 3, before installer_1's CONTEXT_FILES ACL lock at
    stage 5. The lock is skip-if-absent and reports the skip as routine, so a brain whose
    context files do not exist YET gets no policy AND no lock on it, and says [done]. The
    two halves are one control: seed first, lock second.

    A missing file here is FATAL. It used to warn — when the upstream was a shared dir
    outside the repo that a clone would simply not have, and the deploy would carry on and
    hand over a brain with no policy and nothing locked. The upstream is now in-repo, so
    absence means a broken checkout, and there is no version of that worth continuing
    past."""
    subs = {"[BRAIN_NAME]": brain, "[AIOS_INSTALL_ROOT_PATH]": f"${INSTALL_ROOT_ENV}"}
    seeded = kept = 0
    for name in CONTEXT_FILES:
        src = SOURCE_ROOT / name
        dst = Path(brain_dir) / name
        if not src.is_file():
            die(f"context/policy source missing: {src}\n"
                f"    Every brain must ship {name} — installer_1 ACL-locks it read-only so\n"
                "    the brain cannot edit its own leash, and it silently locks NOTHING if\n"
                "    the file is not there. Incomplete checkout of the factory repo?")
        if dst.exists():
            kept += 1
            continue  # never clobber a live/tuned context file on redeploy
        try:
            text = src.read_text(encoding="utf-8")
            for k, v in subs.items():
                text = text.replace(k, v)
            dst.write_bytes(text.encode("utf-8"))
            seeded += 1
        except OSError as e:
            die(f"could not seed {name} into {brain_dir} ({e}) — the brain would deploy "
                "with no policy and no lock on it.")
    ok(f"brain-root context/policy in place ({seeded} seeded, {kept} existing kept) — "
       "from source/, locked read-only by installer_1 at stage 5")


# ---------------------------------------------------------------------------
# Stage: engine (BUILD FROM SCRATCH by default; reuse/restore are opt-in)
# ---------------------------------------------------------------------------

def ensure_engine(args):
    """Make a deployable engine available for stage 5's import.

    The DEFAULT for a fresh deploy is to BUILD ONE FROM SCRATCH (download base →
    provision → export). A prebuilt engine tar is NEVER a required input — it is a
    transfer medium for the Windows cross-account hop (builder-context export →
    brain-account import), which finalize_engine_artifact() removes after deploy.

    Precedence (first match wins):
      --from-scratch     force a fresh build even if a distro/tar already exists
      --engine-tar PATH  restore / build-from-existing: stage that prebuilt engine
      distro imported    reuse the already-imported brain-<brain> (redeploy fast-path)
      tar already in system/wsl_engine/ reuse it (a prior build-engine run left it here)
      otherwise          BUILD FROM SCRATCH — the default, no longer an error
    """
    _, brain_dir = brain_paths(args)
    wsl_dir = wsl_runtime_dir(brain_dir)
    engine_tar = wsl_dir / f"{args.brain}_engine.tar"

    def _build_and_check():
        build_engine(args)
        if not (getattr(args, "dry_run", False) or engine_tar.is_file()):
            die(f"engine build did not produce {engine_tar} — see output above.")

    # Force a fresh build (true cold-start): refuse to reuse anything.
    if getattr(args, "from_scratch", False):
        info("--from-scratch: refusing to reuse any existing distro/engine tar — building fresh")
        _build_and_check()
        return

    # Opt-in restore / build-from-existing: stage the supplied prebuilt engine.
    if getattr(args, "engine_tar", None):
        src = Path(args.engine_tar)
        if not src.is_file():
            die(f"--engine-tar not found: {src}")
        wsl_dir.mkdir(parents=True, exist_ok=True)
        info(f"--engine-tar: staging supplied prebuilt engine → {engine_tar}")
        import shutil
        shutil.copy2(src, engine_tar)
        ok("engine artifact staged from --engine-tar")
        return

    # Redeploy fast-path: an already-imported brain distro IS a usable engine.
    if distro_exists(args.brain):
        ok(f"distro {distro_name(args.brain)} already imported — reusing "
           "(pass --from-scratch to rebuild)")
        return

    # A prior `build-engine` run left a tar here — reuse it.
    if engine_tar.is_file():
        ok(f"engine artifact present: {engine_tar.name} — reusing "
           "(pass --from-scratch to rebuild)")
        return

    # DEFAULT: a fresh deploy with nothing present → build from scratch.
    info("no engine present — building from scratch (the default for a fresh deploy)")
    _build_and_check()


def finalize_engine_artifact(args):
    """Post-deploy disposition of the engine tar (system/wsl_engine/<brain>_engine.tar).

    The tar exists ONLY as the transfer medium for the Windows cross-account hop
    (builder-context `wsl --export` → brain-account `wsl --import` in stage 5).
    Once the distro is imported it is dead weight. Governed by --export-engine:
      omitted        → DELETE it (default — no multi-GB litter tracked/synced under brains/)
      bare flag      → MOVE it to brains/<brain>/wsl_engine_export/ (the export home)
      given a DIR    → MOVE it to DIR (backup/reinstall elsewhere)

    The tar is BUILT inside the live runtime dir (system/wsl_engine/) because that is where
    stage 5 imports from, but an EXPORT is a deliberate, user-asked-for artifact and gets its
    own brain-root home: <brain>/wsl_engine_export/ exists ONLY when --export-engine was
    passed. Keeping the export in place under the live runtime dir is what made a 12 GB
    runtime dir look like an export artifact and invited a manual delete of the live disk.

    Only runs on a successful deploy — a mid-deploy failure raises first, keeping
    the tar so a retry does not rebuild.
    """
    if getattr(args, "dry_run", False):
        return
    _, brain_dir = brain_paths(args)
    engine_tar = wsl_runtime_dir(brain_dir) / f"{args.brain}_engine.tar"
    if not engine_tar.is_file():
        return  # reuse/already-imported path built no tar (nothing to dispose of)

    dest = getattr(args, "export_engine", None)

    # Omitted → default cleanup: the tar was only the account-hop medium.
    if dest is None:
        engine_tar.unlink()
        ok(f"cleaned up transient engine tar ({engine_tar.name}) — distro already imported")
        return

    # Bare --export-engine → move it to the brain-root export home.
    if dest is True:
        dest = brain_dir / ENGINE_EXPORT_DIR

    # --export-engine DIR → move it there (removes the working copy in system/wsl_engine/).
    dest_dir = Path(dest)
    out = dest_dir / engine_tar.name
    if out.resolve() == engine_tar.resolve():
        ok(f"--export-engine: destination is the engine's own dir — keeping in place")
        return
    import shutil
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(engine_tar), str(out))
    ok(f"--export-engine: engine tar moved → {out}")


# ---------------------------------------------------------------------------
# Engine BUILD (from-scratch): base Debian → provision (stages 1–9) → export tar
# ---------------------------------------------------------------------------
#
# The engine tar is normally an INPUT (see ensure_engine). build_engine PRODUCES it
# from nothing, collapsing the by-hand provision/ recipe (system/brain_bin/provision/README.md,
# stages 0–10) into ONE idempotent flow:
#     obtain a base Debian → a throwaway *scratch* distro
#   → run stages 1–9 as the correct identity each, honoring the mid-build restarts
#   → wsl --export the provisioned distro → the shippable <brain>_engine.tar
#   → drop the scratch distro.
#
# IDENTITY: the build runs in the BUILDER's Windows context (the admin running this)
# against the SCRATCH distro — never the brain's Windows account, never through
# run_as_brain (that bridges the *deployed* brain-owned distro, which does not exist
# yet at build time). The "brain" during build is the LINUX user provision_stage2.sh
# creates inside the scratch distro, parametrized by BRAIN=<brain>. So every call is
# a plain `wsl -d <scratch> -u <root|linuxbrain> -- …` (argv, no shell).
#
# The provision scripts are read straight off the host provision/ dir via the WSL
# drvfs automount (their in-distro path is resolved authoritatively with `wslpath`).
# They are LF and non-interactive, so `bash <path>` runs them unattended.

BUILD_WORKSPACE = "_build"   # under system/wsl_engine/: per-brain scratch VHDX + base tar


def _brun(args, cmd, label=None, check=True, hb=None):
    """run honoring --dry-run. In dry mode, print the exact command and skip it.
    On a real nonzero exit, fail loud with the failing stage's label (fail-loud).
    hb=<text> wraps the (often silent, multi-minute) call in a heartbeat tick."""
    if getattr(args, "dry_run", False):
        print(f"  DRYRUN: {' '.join(str(a) for a in cmd)}")
        return None
    try:
        if hb:
            with heartbeat(hb):
                return run(cmd, check=check)
        return run(cmd, check=check)
    except subprocess.CalledProcessError as e:
        if label:
            die(f"{label} FAILED (exit {e.returncode}) — build stopped.\n"
                f"    Command: {' '.join(str(a) for a in cmd)}")
        raise


def _unregister_if_present(args, distro):
    """Idempotent clean slate: drop a leftover scratch distro so the build is fresh."""
    if getattr(args, "dry_run", False):
        print(f"  DRYRUN: wsl --unregister {distro}   (only if present)")
        return
    rc, out, _ = run_out(["wsl", "-l", "-q"])
    names = [ln.strip().replace("\x00", "") for ln in out.splitlines()] if rc == 0 else []
    if distro in names:
        info(f"removing leftover scratch distro {distro}")
        run(["wsl", "--unregister", distro], check=False)
    else:
        info(f"no leftover {distro} (clean start)")


def _wsl_path(args, scratch, winpath):
    """Translate a host Windows path to its in-distro path, authoritatively via
    `wslpath -a` (respects the distro's actual automount root). In dry-run the
    distro may not exist, so fall back to the conventional /mnt/<drive> mapping
    purely for display.

    NOTE: pass a FORWARD-slash path to wslpath. A raw backslash path
    (C:\\brains\\Home\\…) loses its separators crossing the WSL interop
    marshalling boundary (wslpath sees `C:brainsHome…` → exit 1); wslpath
    accepts `C:/brains/Home/…` fine. The dry-run branch already normalized;
    the live branch did not — that asymmetry was the stage-4 build blocker."""
    winpath = str(winpath).replace("\\", "/")
    if getattr(args, "dry_run", False):
        drive = winpath[0].lower()
        rest = winpath[2:].replace("\\", "/").lstrip("/")
        return f"/mnt/{drive}/{rest}"
    rc, out, e = run_out(["wsl", "-d", scratch, "--", "wslpath", "-a", winpath])
    p = (out or "").strip().replace("\x00", "")
    if rc != 0 or not p:
        die(f"could not translate host path into {scratch} via wslpath: {winpath}\n{e}")
    return p


def _obtain_base(args, scratch, install_dir):
    """Register the scratch distro from a base Debian rootfs.

    --imagefile given  → `wsl --import` that rootfs/image directly (pinned/offline;
                         e.g. base_images/debian-base.rootfs.tar for a reproducible,
                         exactly-validated base).
    empty/omitted      → pull the Store meta-distro `Debian`, then RE-HOME it under
                         the controllable scratch name: the meta install registers a
                         distro named literally 'Debian' (not a name we control and one
                         that would collide with a user's own Debian), so we export the
                         fresh rootfs and re-import it as <scratch>, then unregister the
                         meta distro. Refuses to touch a pre-existing 'Debian' (that is
                         the user's, not ours). DEFAULT BASE = Debian (NOTE 001-28: the
                         provision recipe is distro-agnostic via os-release ${ID}; Debian
                         is apt+systemd+glibc, −75% base layer vs Ubuntu, validated live)."""
    if not getattr(args, "dry_run", False):
        import shutil
        shutil.rmtree(install_dir, ignore_errors=True)   # clear any stale ext4.vhdx / base tar
        install_dir.mkdir(parents=True, exist_ok=True)

    if getattr(args, "imagefile", None):
        src = Path(args.imagefile)
        if not getattr(args, "dry_run", False) and not src.is_file():
            die(f"--imagefile not found: {src}")
        info(f"importing scratch {scratch} from image {src}")
        _brun(args, ["wsl", "--import", scratch, str(install_dir), str(src), "--version", "2"],
              label="wsl --import (imagefile)", hb="importing base image into scratch distro")
        return

    # Store meta-distro path.
    if not getattr(args, "dry_run", False):
        rc, out, _ = run_out(["wsl", "-l", "-q"])
        names = [ln.strip().replace("\x00", "") for ln in out.splitlines()] if rc == 0 else []
        if "Debian" in names:
            die("a WSL distro named 'Debian' already exists — refusing to clobber it to\n"
                "    stage a base. For a from-scratch build, either remove that distro, or\n"
                "    pass --imagefile <rootfs> to build from an explicit base instead of the\n"
                "    Store meta-distro.")
    base_tar = install_dir / "debian-base.tar"
    info("installing base Debian (Store meta-distro), no launch (no OOBE)")
    _brun(args, ["wsl", "--install", "Debian", "--no-launch"], label="wsl --install Debian",
          hb="downloading base Debian from the Store")
    info(f"re-homing base rootfs → controllable scratch {scratch}")
    _brun(args, ["wsl", "--export", "Debian", str(base_tar)], label="wsl --export Debian (base)",
          hb="exporting base rootfs")
    _brun(args, ["wsl", "--import", scratch, str(install_dir), str(base_tar), "--version", "2"],
          label="wsl --import (scratch)", hb="importing base into scratch distro")
    _brun(args, ["wsl", "--unregister", "Debian"], label="wsl --unregister Debian (meta)", check=False)


def _runtime_image_refs(brain_dir):
    """Resolve the container image refs the RUNTIME will use, so the build can PREFETCH
    them into the engine. The brain's per-user WSL2 VM comes up with NO network interface
    under mirrored networking, so it cannot `docker pull` at runtime — the images must be
    baked into the engine tar during the (networked) build instead.

    Mirrors the runtime's resolution so build == runtime (else the cache misses): the
    *_VERSION knobs from brain.env (default :latest), nginx pinned to match the compose.
    Reads brain_etc/brain.env if an operator pinned versions, else the staged
    brain_etc.example template, else falls back to :latest. Live public tags only —
    nothing large is committed to git (the tar rides gitignored under brains/*)."""
    env = {}
    for rel in ("brain_etc/brain.env", "brain_etc.example/brain.env"):
        p = brain_dir / rel
        if p.is_file():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, _, v = s.partition("=")
                env.setdefault(k.strip(), v.strip())
            break
    def ver(key):
        return env.get(key) or "latest"      # empty or absent → :latest (runtime default)
    return [
        f"chromadb/chroma:{ver('CHROMA_VERSION')}",
        f"ollama/ollama:{ver('OLLAMA_VERSION')}",
        f"crazymax/fail2ban:{ver('FAIL2BAN_VERSION')}",
        "nginx:1.27",   # pinned in brain_etc/docker/compose.yaml (no version knob)
    ]


def _runtime_model_roster(brain_dir):
    """The ollama models the RUNTIME requires (the brain_etc/ollama/models roster), so the build
    can BAKE them into the engine's ollama volume — the NIC-less runtime VM cannot `ollama pull`
    them (container images are baked by prefetch_images; models live in the data volume and need
    their own build-time seed, prefetch_models.sh). Reads the seeded roster if present, else the
    staged template. One model name per line; '#' comments and blank lines ignored."""
    for rel in ("brain_etc/ollama/models", "brain_etc.example/ollama/models"):
        p = brain_dir / rel
        if p.is_file():
            models = []
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    models.append(s)
            return models
    return []


def build_engine(args):
    """From-scratch engine build → system/wsl_engine/<brain>_engine.tar.

    Idempotent + fail-loud: a leftover scratch distro is unregistered first, a stale
    workspace/tar is overwritten, and ANY provision stage's nonzero exit stops the
    build (does not march on). See the module comment above for identity/rationale."""
    validate_brain_name(args.brain)
    brain   = args.brain
    posture = getattr(args, "posture", None) or "personal"
    scratch = build_distro_name(brain)

    _, brain_dir = brain_paths(args)
    wsl_dir    = wsl_runtime_dir(brain_dir)
    engine_tar = wsl_dir / f"{brain}_engine.tar"
    workspace  = wsl_dir / BUILD_WORKSPACE / brain
    prov_win   = SOURCE_ROOT / "system" / "brain_bin" / "provision"
    if not (prov_win / "provision_stage2.sh").is_file():
        die(f"provision recipe not found at {prov_win} — cannot build the engine.")

    banner(f"Build engine: {brain}  (scratch={scratch}, posture={posture})")
    total = 6

    # 1. Clean slate + obtain base into the scratch distro (refuse reuse).
    stage(1, total, "Obtain base Debian into scratch distro")
    _unregister_if_present(args, scratch)
    _obtain_base(args, scratch, workspace)
    ok(f"scratch distro {scratch} registered")

    def _drop_scratch():
        """Unregister the scratch distro + remove its workspace (idempotent, dry-run aware)."""
        _brun(args, ["wsl", "--unregister", scratch],
              label="wsl --unregister (scratch)", check=False)
        if not getattr(args, "dry_run", False):
            import shutil
            shutil.rmtree(workspace, ignore_errors=True)

    # Everything from here can leave a multi-GB scratch distro + base tar on disk, so
    # guard it: on ANY failure (a stage die raises SystemExit, a BaseException, Ctrl-C)
    # tear the scratch down — UNLESS --keep-scratch was passed to inspect the failure.
    # Without this, a mid-build die exited before cleanup and leaked ~2.7 GB (the
    # scratch vhdx + debian-base.tar) — flagged by the v0.6.1 cold-start test.
    try:
        # Resolve the on-host provision dir to its in-distro path (drvfs automount).
        # Forward-slash it: a backslash path loses its separators through wslpath.
        prov = _wsl_path(args, scratch, prov_win)
        def sh(name): return f"{prov}/{name}"

        # 2. Stage 1 (ROOT, pre-systemd): linux brain user, subuid/subgid, wsl.conf
        #    (systemd + default user), Docker CE + rootless extras, linger.
        stage(2, total, "Provision — stage 1 (root, pre-systemd)")
        _brun(args, ["wsl", "-d", scratch, "-u", "root", "--",
                     "env", f"BRAIN={brain}", "bash", sh("provision_stage2.sh")],
              label="provision_stage2.sh (root)",
              hb="provision stage 1 — apt + docker-ce install (in-distro, output buffers)")
        # RESTART #1 (required): terminate so systemd=true + default user=<brain> apply.
        info("restart #1 — wsl --terminate so systemd + default user take effect")
        _brun(args, ["wsl", "--terminate", scratch], label="wsl --terminate (#1)", check=False)

        # 3. Stages 2b–9 (POST-systemd), each as its correct identity. `env BRAIN=` is
        #    passed only to the stages that key off it (root-run stages, and stage4 which
        #    reads BRAIN_NAME/BRAIN); brain-run stages default to id -un = the linux brain.
        #    check=True → any nonzero raises → _brun converts to a labelled die → STOP.
        stage(3, total, "Provision — stages 2b–9 (post-systemd)")
        # PREFETCH the runtime container images into the engine (LAST, after cleanup: the
        # brain's per-user WSL VM has no network under mirrored, so it cannot pull at
        # runtime — bake the public images in here where the scratch distro DOES network).
        image_refs = _runtime_image_refs(brain_dir)
        # Bake the runtime's ollama models + neuron images too (same NIC-less-runtime reason as
        # the container images): models into the ollama volume (prefetch_models.sh), and the
        # pip-installed neuron images pre-built (prefetch_neurons.sh) — both networked, here.
        model_roster = _runtime_model_roster(brain_dir)
        neuron_in_ctx  = brain_dir / "system" / "common_neuron_platform" / "input"
        neuron_act_ctx = brain_dir / "system" / "common_neuron_platform" / "action"
        have_neuron_src = ((neuron_in_ctx / "Dockerfile").is_file()
                           and (neuron_act_ctx / "Dockerfile").is_file())
        steps = [
            ("root", "stage2b_root.sh",  [],          True,  "stage2b_root.sh (root)"),
            (brain,  "stage3_brain.sh",  [],          False, "stage3_brain.sh (brain)"),
            (brain,  "stage4_brain.sh",  [posture],   True,  "stage4_brain.sh (brain)"),
            ("root", "stage5_root.sh",   [],          False, "stage5_root.sh (root)"),
            ("root", "stage6_root.sh",   [],          False, "stage6_root.sh (root)"),
            (brain,  "stage6_brain.sh",  [prov],      False, "stage6_brain.sh (brain)"),  # arg = maint-script src dir
            ("root", "stage7_harden.sh", [posture],   True,  "stage7_harden.sh (root)"),
            (brain,  "cleanup_brain.sh", [],          False, "cleanup_brain.sh (brain)"),
            (brain,  "prefetch_images.sh", image_refs, False, "prefetch_images.sh (brain)"),
        ]
        # Models (into the ollama volume) — need_brain=True: the script derives the volume name
        # ${BRAIN}_ollama_models from BRAIN. Skipped when the roster is empty.
        if model_roster:
            steps.append((brain, "prefetch_models.sh", model_roster, True,
                          "prefetch_models.sh (brain)"))
        # Neuron images (pre-built) — pass the in-distro (automount) build-context paths; the
        # script tags them ${BRAIN}-{input,action}_neurons. Skipped for a bare factory (no
        # Dockerfile), exactly as the runtime neuron_bundles stage skips the image build.
        if have_neuron_src:
            steps.append((brain, "prefetch_neurons.sh",
                          [_wsl_path(args, scratch, neuron_in_ctx),
                           _wsl_path(args, scratch, neuron_act_ctx)],
                          True, "prefetch_neurons.sh (brain)"))
        # stage7 asserts a SECOND contract (`${BRAIN_ETC_HOST:?}`): the HOST-spelled path
        # of this brain's brain_etc, which becomes the drvfs `What=` of the read-only
        # /opt/brain_truths mount. Host-spelled, not _wsl_path'd — drvfs resolves it from
        # the Windows side. Forward-slashed: a backslash path loses its separators through
        # the WSL bridge. Derived from brain_paths so it follows --install-root rather than
        # pinning a tree. The script refuses to default it because a wrong-but-real path
        # mounts ANOTHER brain's truths over this one read-only, and every check downstream
        # would confirm the healthy-looking mount.
        etc_host = str(brain_dir / "brain_etc").replace("\\", "/")
        for user, script, extra, need_brain, label in steps:
            cmd = ["wsl", "-d", scratch, "-u", user, "--"]
            if need_brain:
                cmd += ["env", f"BRAIN={brain}"]
                if script == "stage7_harden.sh":
                    cmd += [f"BRAIN_ETC_HOST={etc_host}"]
            cmd += ["bash", sh(script)] + extra
            # Every provision step is an in-distro bash run whose stdout can buffer for minutes
            # with no console output — stage5_root.sh's `unattended-upgrade --dry-run` is the
            # acute one (NOTE 001-53). Wrap each in a heartbeat so the deploy never looks hung.
            _brun(args, cmd, label=label, hb=f"{script} (in-distro, output may buffer)")
        ok("provision stages 2b–9 complete")

        # 4. RESTART #2: terminate before export so the image is captured from a quiesced,
        #    shut-down filesystem (cleanup already brought containers down) and any posture
        #    wsl.conf change (server: automount off) is baked into the exported image.
        stage(4, total, "Quiesce scratch for a clean export")
        info("restart #2 — wsl --terminate before export")
        _brun(args, ["wsl", "--terminate", scratch], label="wsl --terminate (#2)", check=False)

        # 5. Export the provisioned distro → the shippable engine tar (overwrite stale).
        stage(5, total, "Export engine tar")
        if not getattr(args, "dry_run", False):
            wsl_dir.mkdir(parents=True, exist_ok=True)
            if engine_tar.exists():
                info(f"overwriting stale engine tar {engine_tar.name}")
                engine_tar.unlink()
        _brun(args, ["wsl", "--export", scratch, str(engine_tar)], label="wsl --export (engine)",
              hb="exporting provisioned engine (multi-GB, please wait)")
        if not getattr(args, "dry_run", False) and not engine_tar.is_file():
            die(f"export reported no error but {engine_tar} is missing — build FAILED.")
        ok(f"engine artifact built: {engine_tar}")
    except BaseException:
        if getattr(args, "keep_scratch", False):
            warn(f"--keep-scratch: build FAILED — leaving {scratch} + {workspace} for inspection")
        else:
            warn(f"build FAILED — cleaning up scratch {scratch} + workspace "
                 "(pass --keep-scratch to keep it for debugging)")
            _drop_scratch()
        raise

    # 6. Tear down the scratch distro + workspace on success (unless kept for debug).
    stage(6, total, "Tear down scratch")
    if getattr(args, "keep_scratch", False):
        warn(f"--keep-scratch: leaving {scratch} + {workspace} in place for inspection")
    else:
        _drop_scratch()
        ok(f"scratch distro {scratch} unregistered, workspace removed")

    banner(f"BUILD ENGINE COMPLETE: {brain}  → {engine_tar.name}")


# ---------------------------------------------------------------------------
# Stage: deploy engine (installer_1 → phase2 + residency)
# ---------------------------------------------------------------------------

def deploy_engine(args):
    """Invoke the STAGED admin installer, which does host prep, hands off to the
    brain-side phase 2 (imports + brings up the stack), and registers residency."""
    _, brain_dir = brain_paths(args)
    installer = brain_dir / "system" / "brain_bin" / "deploy" / "brain_installer_1_admin.py"
    if not installer.is_file():
        die(f"staged installer not found: {installer} — did stage_package run?")

    # Reset the host stack AGAIN, immediately before the RUNTIME VM is born.
    #
    # preflight()'s reset protects the BUILD VM, and stage 4 then re-poisons the host on its way
    # out: `wsl --unregister brain-build-<brain>` orphans that VM's mirrored state in vmcompute
    # (see _reset_wsl_host_network). The very next VM created is the brain's runtime VM, so it
    # inherits the wreckage and boots loopback-only — measured 2026-07-15: a build VM that
    # happily pulled apt + a 986 MB model, followed by a runtime VM with no NIC at all, failing
    # `[8/10] ollama models` and then VERIFY. Resetting here is what makes the deploy's OWN
    # cleanup survivable. Safe: the brain's VM does not exist yet.
    _reset_wsl_host_network()

    cmd = [sys.executable, str(installer), "--brain", args.brain,
           "--posture", args.posture]
    if getattr(args, "skip_residency", False):
        cmd.append("--skip-residency")
    run(cmd)

    # Hard gate before advancing to the seam/gateway stages: prove the brain-side phase 2
    # actually imported the distro. installer_1 prints its own [OK] banners, but a phase-2
    # launch that never fired (e.g. Start-Process rejecting an unreachable -WorkingDirectory)
    # can leave brain-<brain> un-imported while this stage still "returns". Seeding the seam
    # and registering residency against a distro that does not exist is the false-green that
    # turned a stage-5 failure into a misleading stage-7 crash. Refuse to continue.
    if not distro_imported_as_brain(args):
        die(f"engine deploy did NOT import {distro_name(args.brain)} — the brain-side phase 2\n"
            "    (brain_installer_2_brain.py, which runs `wsl --import`) never completed. This\n"
            "    stage is the REAL failure; do not trust later stages. Re-run the deploy and\n"
            "    check the phase-2 launch (brain_installer_1_admin.py launch_phase2 → the\n"
            "    system/deploy_logs/<date>_deploy_phase2.log[.err] files).")
    ok("engine deploy (installer_1) complete — distro imported")


# ---------------------------------------------------------------------------
# Stage: brain-truths config-exposure seam (host source of truth + RO mount + door)
# ---------------------------------------------------------------------------

# deploy --posture initializes the brain.env network stance ON FIRST SEED ONLY; thereafter the
# seam brain.env is authoritative (a redeploy never re-seeds an existing brain.env, so a tuned
# posture is preserved). The CLI term maps to brain.env's own two-value vocabulary + its bind:
#   personal → workstation stance, all listeners on 127.0.0.1 (loopback-only, nothing on the LAN)
#   server   → server stance, listeners on 0.0.0.0 (LAN-eligible, per-surface PUBLISH_TO_LAN gates)
# Without this the template's shipped defaults (server / 0.0.0.0) always won: render_dotenv emits
# them into .env.rendered and the seam-sync clobbers gateway_port's posture-correct bind, so a
# `--posture personal` brain still published every surface on 0.0.0.0 (root-caused 2026-07-13).
_POSTURE_TO_BRAIN_ENV = {
    "personal": ("workstation", "127.0.0.1"),
    "server":   ("server",      "0.0.0.0"),
}


def _apply_posture_to_brain_env(text, posture):
    """Rewrite BRAIN_POSTURE + GW_BIND_ADDRESS values in a freshly-seeded brain.env to match the
    deploy --posture. Replaces ONLY the value token, leaving each line's alignment padding and
    inline comment verbatim. Unknown posture → text unchanged (fail-open to the template)."""
    stance, bind = _POSTURE_TO_BRAIN_ENV.get(posture, (None, None))
    if stance is None:
        return text
    text = re.sub(r'(?m)^(BRAIN_POSTURE=)\S+',   rf'\g<1>{stance}', text)
    text = re.sub(r'(?m)^(GW_BIND_ADDRESS=)\S+', rf'\g<1>{bind}',   text)
    return text


def seed_brain_etc_from_example(brain_dir, brain, posture="personal"):
    """Seed the human knob panel brain_etc/ from the packaged ADR-0015 TEMPLATE
    brain_etc.example/, substituting the __BRAIN_NAME__ provision-time literal with the
    real brain name (and the deploy --posture into a freshly-seeded brain.env — see
    _apply_posture_to_brain_env). This is what makes a fresh brain path-router shaped: the template
    ships brain.env + gateway/{gateway.conf,token_registry,route_registry} + neuron/
    {bundles,sources}.yaml + docker/compose*.yaml + chroma/ollama env + tls templates.

    ONLY-IF-ABSENT per file (idempotent, non-destructive): it never clobbers a live
    brain's tuned knobs or minted tokens on a REDEPLOY, and it only fills in the ADR-0015
    files that the base-engine distro capture (brain_truths provision, which runs first)
    did not produce. Derived config (nginx_auto_gen/, token maps) is regenerated from these
    seeded knobs by reapply_brain_configs.py in the gateway stage, so it need not be exact
    here. Runs AFTER brain_truths provision so the /etc ACL posture (inheritance broken,
    brain RX-only) is already in force and the seeded files are born brain-read-only."""
    example = brain_dir / "brain_etc.example"
    etc = brain_dir / "brain_etc"
    if not example.is_dir():
        warn(f"brain_etc.example not staged ({example}) — cannot seed the ADR-0015 config "
             "seam; the deploy will fall back to whatever the engine baked (base engine).")
        return
    etc.mkdir(parents=True, exist_ok=True)
    seeded = 0
    for src in sorted(example.rglob("*")):
        rel = src.relative_to(example)
        dst = etc / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        if dst.exists():
            continue  # never clobber a live/tuned knob or a minted token
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = src.read_text(encoding="utf-8")
            # __BRAIN_NAME__ is the provision-time literal (service names, upstreams);
            # ${BRAIN_NAME} is a runtime env ref resolved by docker compose — leave it.
            text = text.replace("__BRAIN_NAME__", brain)
            # Only the top-level brain.env carries the network posture knobs; seed --posture into it.
            if rel == Path("brain.env"):
                text = _apply_posture_to_brain_env(text, posture)
            dst.write_bytes(text.encode("utf-8"))
        except UnicodeDecodeError:
            dst.write_bytes(src.read_bytes())  # binary template (none expected) — copy raw
        seeded += 1
    ok(f"brain_etc seeded from template ({seeded} file(s) filled in; existing knobs kept)")

    # config-flow Phase 5 / WS3: brain_etc.example/ is a factory TEMPLATE SOURCE only — it must
    # NOT persist as a sibling of the rendered brain_etc/ in a DEPLOYED root. Now that brain_etc/
    # is populated, remove the template copy from the brain root (a fresh tarball re-extracts it
    # each deploy, so re-seeding stays available). Never removed unless brain_etc/ is non-empty.
    try:
        if etc.is_dir() and any(etc.iterdir()):
            shutil.rmtree(example, ignore_errors=True)
            ok("brain_etc.example/ removed from the deployed root (template source stays in the factory)")
    except Exception as e:
        warn(f"could not remove the deployed brain_etc.example/ ({e}) — harmless, but the "
             "deployed root will carry a stray template copy")


def seam(args):
    """Expose all config on the host in brain_etc/ (human-named, admin-RW, brain-RO),
    mounted read-only into the distro at /opt/brain_truths, plus the knowledge/ data
    door (knowledge/chroma_store). Runs after the stack is up (needs in-distro config to seed).
    install-mount is idempotent and also covers engines that predate the seam.

    Then overlay the ADR-0015 template (brain_etc.example -> brain_etc) so the seam source
    is path-router shaped even on a base engine whose distro capture predates it."""
    _, brain_dir = brain_paths(args)
    bt = brain_dir / "system" / "brain_sbin" / "brain_truths.py"
    if not bt.is_file():
        warn(f"brain_truths.py not staged ({bt}) — skipping config-exposure seam")
        return
    run([sys.executable, str(bt), "--brain", args.brain, "--brain-dir", str(brain_dir),
         "provision"])
    # ADR-0015: overlay the template knobs BEFORE install-mount, so the RO mount exposes a
    # complete path-router config seam (chroma+ollama+action+neuron), not just the base
    # engine's single-route capture.
    seed_brain_etc_from_example(brain_dir, args.brain, getattr(args, "posture", None) or "personal")
    run([sys.executable, str(bt), "--brain", args.brain, "--brain-dir", str(brain_dir),
         "install-mount"])

    # Install the wsl_in_distro_scripts seam mount too. This is the RO mount that carries the
    # CANONICAL apply primitive (/opt/brain_wsl_in_distro_scripts/apply_brain_truths.sh) + the
    # neuron_schedule reconciler — both of which the gateway-stage reapply AND the boot
    # keepalive now run BY PATH. Without it the apply primitive would be missing (reapply's
    # seam apply fails; the keepalive skips brain-truths). Idempotent.
    ws = brain_dir / "system" / "brain_sbin" / "wsl_scripts.py"
    if ws.is_file():
        run([sys.executable, str(ws), "--brain", args.brain, "--brain-dir", str(brain_dir),
             "install"], check=False)
        ok("wsl_in_distro_scripts seam mounted (apply primitive + neuron_schedule available)")
    else:
        warn(f"wsl_scripts.py not staged ({ws}) — the in-distro apply primitive seam is not "
             "mounted; the gateway reapply + boot keepalive brain-truths apply will be skipped")
    ok("brain-truths seam: host brain_etc/ exposed, /opt/brain_truths mounted RO, data door open")


# ---------------------------------------------------------------------------
# Bootstrap-token minting (host-side, into the unified token registry)
# ---------------------------------------------------------------------------
# Mode C denies everything without a credential, so a fresh deploy MUST provision a
# reader+writer pair. Tokens live in the SEAM source of truth — the unified
# brain_etc/gateway/token_registry — NOT in-distro: the seam re-syncs
# brain_etc -> ~/docker on every keepalive, so an in-distro token would be reverted.
# Writing them host-side here also sidesteps the `run_as_brain --script` automount-off
# 127 that broke the old in-distro mint. The registry is the single source of truth;
# the nginx *_tokens.map / ollama_*.map files are GENERATED from it (gateway_tokens.py),
# so list/rotate/revoke via gateway_token(s).py interoperate byte-for-byte.

def _load_gateway_tokens(brain_dir):
    """Load the staged gateway_tokens.py (registry model + generator) as a library.
    It is self-contained (no sibling imports) and resolves its own BRAIN_DIR from its
    folder, but every call here passes brain_dir explicitly."""
    import importlib.util
    p = Path(brain_dir) / "system" / "brain_sbin" / "gateway_tokens.py"
    if not p.is_file():
        die(f"staged gateway_tokens.py not found: {p}")
    spec = importlib.util.spec_from_file_location("gateway_tokens", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bootstrap_grant(role):
    return "chroma:writer" if role == "writer" else "chroma:reader"


def _read_seam_token(brain_dir, role):
    """Return the first registry token granted the <role>'s chroma grant, or None."""
    gt = _load_gateway_tokens(brain_dir)
    grant = _bootstrap_grant(role)
    for e in gt.read_registry(brain_dir):
        if grant in e.grants:
            return e.token
    return None


def _ensure_bootstrap_token(brain_dir, role, label="bootstrap"):
    """Idempotently ensure a <role> token exists in the registry; return its secret.
    Reuses an existing token on re-runs (no duplicate churn), then regenerates the
    nginx maps from the registry so the seam sync has fresh artifacts to push."""
    import secrets
    from datetime import datetime, timezone
    gt = _load_gateway_tokens(brain_dir)
    entries = gt.read_registry(brain_dir)
    grant = _bootstrap_grant(role)
    for e in entries:
        if grant in e.grants:
            gt.generate(entries, gt.gateway_dir(brain_dir))   # ensure maps exist/current
            return e.token
    token = secrets.token_hex(32)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries.append(gt.Entry(token, [grant], f"{label}-{role}", created))
    gt.write_registry(entries, brain_dir)
    gt.generate(entries, gt.gateway_dir(brain_dir))
    return token


def _persist_machine_token_env(brain, role, token):
    """Persist a bootstrap token as a Windows SYSTEM (Machine) scope environment variable so it
    survives across users/sessions and new shells. Reader -> <BRAIN>_CHROMA_R, writer ->
    <BRAIN>_CHROMA_RW (the brain name uppercased; the brain-name regex only admits chars that
    are valid in an env var name once uppercased).

    Machine-scope env vars live in HKLM\\SYSTEM\\...\\Session Manager\\Environment and REQUIRE an
    elevated process to write. The deploy orchestrator runs elevated (require_admin, asserted up
    front in main), so this gateway stage is the correct — and only viable — place to write them.

    The secret is passed to PowerShell via the child's ENVIRONMENT, never on the argv, so it
    never lands in a command line / process listing / shell history. Returns the env var name on
    success, else None (warn-only: failing to cache the token must not fail the deploy — the raw
    tokens are still printed above)."""
    suffix = "CHROMA_RW" if role == "writer" else "CHROMA_R"
    name = f"{brain.upper()}_{suffix}"
    ps = f"[Environment]::SetEnvironmentVariable('{name}', $env:BRAIN_TOKEN_VALUE, 'Machine')"
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True,
                       env=dict(os.environ, BRAIN_TOKEN_VALUE=token))
    if p.returncode != 0:
        warn(f"could not persist {name} at Machine scope (rc={p.returncode}): "
             f"{(p.stderr or p.stdout).strip()}")
        return None
    return name


# NOTE: the old _ensure_seam_gateway_config() (hand-copy of a single-route
# system/brain_bin/gateway/nginx/nginx.conf.template into the seam) is RETIRED. The ADR-0015
# gateway is REGENERATED from the gateway.conf knobs by reapply_brain_configs.py
# (gateway_config generate), which produces the full path-router nginx_auto_gen/ tree —
# so there is no hand template to enforce anymore.


def reapply_stack(args, brain_dir):
    """Lay the FULL ADR-0015 stack onto the on-disk knobs via reapply_brain_configs.py —
    the "1 tool to rule them all". It (1) regenerates ALL backend config from the seeded
    knob files (brain.env + gateway.conf + token_registry -> nginx_auto_gen/ path-router
    [chroma/ollama/action/internal + njs/inspect.js], token_maps_auto_gen/, fail2ban),
    (2) regenerates wsl/apply.manifest, and (3) syncs the seam into the runtime and
    FORCE-RECREATES the whole stack (chroma, ollama, gateway, fail2ban) with the exposure
    overlays layered from the *_EXPOSE knobs. It runs the apply primitive in-distro at the
    CANONICAL path /opt/brain_wsl_in_distro_scripts/apply_brain_truths.sh (the same path the
    boot keepalive and gateway_tokens use), so residency and reapply agree.

    Replaces the old hand-sync of a single-route nginx.conf.template + a gateway-only
    recreate (which predated the path-router and used a stale /opt/brain_truths/scripts path)."""
    rc = brain_dir / "system" / "brain_sbin" / "reapply_brain_configs.py"
    if not rc.is_file():
        warn(f"reapply_brain_configs.py not staged ({rc}) — cannot lay the ADR-0015 stack; "
             "the gateway will come up on whatever config the engine baked. Check that "
             f"{SOURCE_ROOT / 'system' / 'brain_sbin'} is intact and re-deploy.")
        return
    # STREAM (run), do NOT capture (run_out): on a fresh distro the in-distro `docker compose
    # up --pull` streams the full image-layer download progress (~1000+ lines). Buffering that
    # in memory OOM'd the capturing caller (System.OutOfMemoryException) → the subprocess died
    # rc=1 → apply_brain_truths rolled the config back → only a partial stack came up. Streaming
    # to our stdout (the deploy log) has no such bound. (reapply itself now defaults to
    # --pull missing, so a re-apply on cached images produces no pull flood at all.)
    code = run([sys.executable, str(rc), "--brain", args.brain,
                "--brain-dir", str(brain_dir)], check=False).returncode
    if code != 0:
        warn(f"reapply_brain_configs returned {code}; the path-router stack may not be fully "
             "live until the next keepalive apply (see the streamed output above).")
    else:
        ok("ADR-0015 stack reapplied (path-router gateway + chroma + ollama + fail2ban, "
           "mode C admission + bootstrap tokens live)")


# ---------------------------------------------------------------------------
# Input-side DATA seams: deliver staged host content onto the distro ext4
# ---------------------------------------------------------------------------

def _deliver_data_seams(args, brain_dir, rab):
    """Copy the staged INPUT-side DATA seams — impulses/ (provider scripts) and knowledge/brain_ro/
    (shipped fixture docs) — from the host brain folder into the distro's EXT4 home, where the
    neuron containers bind-mount them at /impulses and /knowledge.

    They MUST land on ext4: a nested 9p (drvfs) mount does NOT propagate into rootless docker, so a
    bind-mounted 9p seam shows EMPTY inside the neuron container and the input neuron ingests 0 docs
    (root-caused 2026-07-13 on the first clean from-scratch deploy — /ask then had nothing to
    retrieve). Unlike the neuron CODE seams (/opt/*_neurons, drvfs mounts used only as BUILD
    contexts), these are RUNTIME bind mounts, so a mount won't do — the content must be copied.

    The copy SOURCE is a TRANSIENT drvfs mount (deliver_data_seam.sh), not /mnt/c. Server posture
    bakes `[automount] enabled=false` (stage7_harden), so the old /mnt/c read could never work
    there: it failed `cp: cannot stat '/mnt/c/...'`, the input neuron found no provider script and
    exited 1, and the deploy still reported [10/10] green. An explicit mount is posture-independent
    — the same mechanism the code seams and /opt/brain_truths already use — so one path serves both
    postures. Delivery is FATAL on failure: a brain whose ingest side cannot start is not a
    successful deploy, and warning-and-continuing is what hid this for two sessions.

    Idempotent (cp -r merges, removes nothing); re-run every deploy so edits to the shipped
    fixtures/scripts reach the runtime. brain_ro's git-delivered sources (neuron_deliver.py) are a
    SEPARATE, additive path — cp -r merges the shipped example fixtures alongside them, never
    clobbering a git-delivered tree."""
    deliver = "/opt/brain_wsl_in_distro_scripts/deliver_data_seam.sh"
    seams = [("impulses",          f"/home/{args.brain}/impulses"),
             ("knowledge/brain_ro", f"/home/{args.brain}/knowledge/brain_ro")]
    for host_sub, dst in seams:
        host_src = brain_dir / host_sub
        if not host_src.is_dir():
            continue
        if _IS_LINUX:
            # OS-forced (root seam delivery). No distro / 9p barrier on Linux: host_src already
            # lives on the real fs the brain's rootless docker bind-mounts, so the transient-drvfs
            # copy dance the Windows path needs does not apply. Copy-merge into the brain home and
            # chown to the brain — run directly as root (we are the already-root deployer), NOT via
            # an identity switch (run_as_brain_argv refuses root=True on Linux by design, NOTE 001-5).
            # Same idempotent `cp -r` merge semantics (removes nothing).
            run(["mkdir", "-p", dst], check=True)
            rc = run(["cp", "-r", f"{host_src}/.", dst], check=False).returncode
            if rc == 0:
                rc = run(["chown", "-R", f"{args.brain}:{args.brain}", dst], check=False).returncode
        else:
            # Forward-slash the Windows path: a BACKSLASH path loses its separators across the
            # run_as_brain bridge (bash eats the backslash + following char, e.g. `\H` -> `H`, so a
            # path like C:\Home\... arrives as C:Home... and the mount dies "special device does not
            # exist"). drvfs accepts
            # the forward-slash form as-is. Same hazard `_wsl_path` already documents for wslpath.
            src_win = str(host_src).replace("\\", "/")
            # Non-trivial shell rides as a SCRIPT by path with positional args, never inline `--`:
            # bare $VAR marshals to EMPTY across the run_as_brain bridge (ratified contract). Root:
            # mount/umount/chown need it; the script chowns the tree back to the brain.
            rc = run(run_as_brain_argv(rab, args.brain,
                                       ["bash", deliver, src_win, dst, args.brain],
                                       root=True), check=False).returncode
        if rc != 0:
            die(f"data-seam delivery of {host_sub} -> {dst} failed (rc={rc}). The input neuron "
                f"bind-mounts this tree and will exit 1 without it ('delivery script not found'), "
                f"so the brain would come up with a dead ingest side. See the error above.")
        ok(f"data seam delivered onto ext4: {host_sub} -> {dst}")


# ---------------------------------------------------------------------------
# Stage: neuron bundles (mount the code-in seams, render bundles, build images)
# ---------------------------------------------------------------------------

def neuron_bundles(args):
    """Deliver the neuron-bundle layer (ADR-0015: neurons are container bundles built from the
    SHARED per-role platform image source at system/common_neuron_platform/{input,action}/,
    reaching the backends only through the gateway). Replaces the vestigial /opt/neurons code-seam
    sync. In order:

      1. neurons_mount.py install — RO drvfs mount those host dirs into the distro at
         /opt/input_neurons + /opt/action_neurons (the build contexts, mount targets unchanged).
      2. (compose bundle regions are rendered host-side by `gateway_config.py generate`, which the
         gateway stage's reapply_brain_configs.py already ran — it materializes BOTH the DEFAULT and
         the ADDITIONAL bundle regions from the brain.env ===NEURONS=== zone through the ONE shared
         renderer, config-flow P0 unification. `add_neuron_bundle.py` is now the operator's
         scaffold+guidance entry point for a NEW bundle, not a separate render path — this deploy
         stage no longer needs to invoke it.)
      3. docker compose --profile neurons build — build ${brain}-input_neurons /
         ${brain}-action_neurons from the mounted contexts.

    The factory ships the shared platform image source under system/common_neuron_platform/ (a
    TODO, not the prototype's private code). When only a scaffold is present (no Dockerfile),
    the image build is SKIPPED with a clear notice — the base RAG stack still runs; a working
    brain needs real neuron code dropped in (or the code-in seam wired)."""
    _, brain_dir = brain_paths(args)
    nmount = brain_dir / "system" / "brain_sbin" / "neurons_mount.py"
    rab = brain_dir / "system" / "brain_sbin" / "run_as_brain.py"

    if nmount.is_file():
        run([sys.executable, str(nmount), "--brain", args.brain, "--brain-dir",
             str(brain_dir), "install"], check=False)
        ok("neuron code-in seams mounted (/opt/input_neurons, /opt/action_neurons)")
    else:
        warn(f"neurons_mount.py not staged ({nmount}) — skipping code-in seam mount")

    # Only real neuron code (a Dockerfile) can be built; a scaffold-only dir cannot.
    have_input = (brain_dir / "system" / "common_neuron_platform" / "input" / "Dockerfile").is_file()
    have_action = (brain_dir / "system" / "common_neuron_platform" / "action" / "Dockerfile").is_file()
    if not (have_input or have_action):
        warn("neuron source is a TEMPLATE SCAFFOLD (no system/common_neuron_platform/input/Dockerfile) "
             "— skipping the neuron IMAGE build. The base RAG stack (chroma+ollama+gateway) is up, "
             "but no neuron bundle will run until the shared platform image source is present under "
             "system/common_neuron_platform/{input,action}/ or the code-in seam is wired. This is "
             "EXPECTED for a bare factory deploy.")
        return

    if not rab.is_file():
        warn(f"run_as_brain.py not staged ({rab}) — cannot build neuron images")
        return
    # Deliver the input-side DATA seams (impulses provider scripts + knowledge/brain_ro fixture
    # docs) onto the distro's ext4 BEFORE the `up` below — the input neuron ingests as it starts,
    # and the neuron container bind-mounts /impulses + /knowledge from the distro home (a 9p mount
    # there would not propagate into rootless docker). Without this the ingest finds 0 sources.
    _deliver_data_seams(args, brain_dir, rab)
    # Build the profile images AND start the bundle services in-distro AS THE BRAIN
    # (rootless docker socket lives in the brain's XDG_RUNTIME_DIR). The build contexts are
    # the just-mounted /opt/{input,action}_neurons. We must activate the FULL profile set,
    # not just `neurons`: a CLI `--profile` REPLACES (does not merge with) the .env
    # COMPOSE_PROFILES, so `--profile neurons` alone drops the profile-gated `gateway`
    # (+ollama/fail2ban) service out of the project — and the neuron's `depends_on: gateway`
    # then fails validation ("depends on undefined service gateway: invalid compose project").
    # Naming all four keeps the already-running base stack defined (up -d is a no-op for it)
    # while the neuron bundle services start from their images.
    #
    # NO `--build` at runtime: the neuron Dockerfile pip-installs from PyPI and builds FROM
    # python:3.12-slim — both need network, which the per-user WSL VM lacks under mirrored. The
    # ${BRAIN}-{input,action}_neurons images are PRE-BUILT into the engine at build time
    # (prefetch_neurons.sh), so here we just `up` from the baked images. `--pull never` keeps
    # compose from contacting the registry for the (locally-present) images; with the images
    # baked, `up` neither pulls nor builds. (A bare-factory engine with no baked neuron image
    # would fail here — that engine also carries no neuron source, so nothing to run anyway.)
    build = ("cd ~/docker && docker compose --profile gateway --profile ollama "
             "--profile fail2ban --profile neurons up -d --pull never")
    code, out, e = run_out(run_as_brain_argv(rab, args.brain, build))
    if code != 0:
        # FATAL, not a warning. The scaffold case (no Dockerfile — nothing to run) already
        # returned above, so reaching here means this brain HAS neuron source and its bundle
        # genuinely failed to start. Warning-and-continuing handed that to a verify stage that
        # could not see it either, and the deploy finished [10/10] green with a dead bundle.
        die(f"neuron bundle up failed (rc={code}) — this brain has neuron source, so the bundle "
            f"is expected to run. The stack is up but its ingest/query side is not.\n{out}{e}")
    # Claim only what was checked: `up -d` returning 0 means compose STARTED the services, not
    # that they are healthy — a neuron can start and then exit non-zero (the input neuron did
    # exactly that for two sessions, while this line said "started"). Liveness is asserted by
    # the verify stage, which now fails on any neuron that exited non-zero.
    ok("neuron bundle services started by compose — health asserted at verify (input_neurons"
       f"{' + action_neurons' if have_action else ''})")


# ---------------------------------------------------------------------------
# Stage: gateway (port + firewall, then mint the bootstrap token pair)
# ---------------------------------------------------------------------------

def gateway(args):
    """Set the gateway host port + bind (+ firewall on server) via the staged
    host-elevated gateway_port, then provision the bootstrap reader+writer token
    pair host-side into the seam and push mode C live."""
    _, brain_dir = brain_paths(args)
    brain_sbin = brain_dir / "system" / "brain_sbin"
    gport = brain_sbin / "gateway_port.py"
    gtoken = brain_sbin / "gateway_token.py"
    run_as_brain = brain_sbin / "run_as_brain.py"

    if not gport.is_file():
        die(f"staged gateway_port not found: {gport}")

    # Provision the bootstrap reader+writer token pair HOST-SIDE into the seam source
    # (brain_etc/gateway/token_registry -> regenerated maps). Mode C denies everything
    # without a credential, so this is mandatory, not optional. Idempotent: reused on
    # re-runs. Shown once. Minted BEFORE reapply so the maps regenerate carrying them.
    reader_tok = _ensure_bootstrap_token(brain_dir, "reader")
    writer_tok = _ensure_bootstrap_token(brain_dir, "writer")
    ok("bootstrap tokens provisioned in brain_etc/gateway (SHOWN ONCE — save them):")
    print(f"    reader (read-only): Bearer {reader_tok}")
    print(f"    writer (read+write): Bearer {writer_tok}")
    # Persist the raw tokens as SYSTEM (Machine) scope Windows env vars so the operator keeps
    # them across users/sessions and freshly-opened shells. This stage runs ELEVATED
    # (require_admin, asserted up front in main), which the HKLM Machine-scope write requires.
    r_name = _persist_machine_token_env(args.brain, "reader", reader_tok)
    w_name = _persist_machine_token_env(args.brain, "writer", writer_tok)
    if r_name and w_name:
        ok(f"tokens persisted as SYSTEM (Machine) env vars: {r_name} (reader), "
           f"{w_name} (writer) — open a NEW shell to pick them up")
    # Auto-mint the NAMED neuron tokens the brain.env YAML zone references (config-flow Phase 3).
    # Each neuron declares `gateway_token: <name>`; gateway_config generate fails CLOSED if that
    # name isn't in the registry. seed_neuron_tokens walks the zone and mints any missing name with
    # its type-default grant (input=chroma:writer, action=chroma:reader, +ollama:use), idempotent -
    # so a fresh install "just works" without the operator pre-creating the shipped example's
    # tokens. --action-caller also ensures a world->:8443 admission token (action:call), shown once.
    # Minted BEFORE reapply so the regenerated maps + generated .env carry them. (Replaces the old
    # brain-wide NEURON/ACTION bearers, which wrote now-dead flat keys into brain.env.)
    seeder = brain_sbin / "seed_neuron_tokens.py"
    if not seeder.is_file():
        die(f"staged seed_neuron_tokens.py not found: {seeder} — check that "
            f"{SOURCE_ROOT / 'system' / 'brain_sbin'} is intact and re-deploy, so a fresh "
            "deploy can auto-mint the neuron tokens.")
    run([sys.executable, str(seeder), "--brain-dir", str(brain_dir), "--action-caller"])
    ok("named neuron tokens auto-minted from the brain.env neuron zone (registry + maps)")
    # Regenerate ALL backend config from the seeded knobs (now carrying the minted tokens)
    # and force-recreate the whole ADR-0015 path-router stack so the admission mode +
    # credentials + every route (chroma/ollama/action) are live for verify.
    reapply_stack(args, brain_dir)

    # Port/bind + firewall LAST — this must not run before reapply_stack. `gateway_port set`
    # ends in an in-distro `docker compose up -d gateway` (gateway_set_port.sh), and compose
    # interpolates EVERY service in the file, including the neurons. Their per-neuron
    # NEURON_TOKEN__* vars are minted by seed_neuron_tokens and rendered into ~/docker/.env by
    # reapply's `gateway_config generate` — so running this first fails closed ("required
    # variable NEURON_TOKEN__... is missing a value"). It only survived historically because the
    # staged compose was a cut-down prototype with no neuron services to interpolate.
    #
    # Note this step does NOT own the port: brain.env is the source of truth (ADR-0013) and
    # reapply renders ~/docker/.env from it, so gateway_port's runtime-.env write is redundant
    # while --port matches the seam. What it genuinely owns is host-side: the Defender rules and
    # the root port registry. A non-default --port still will not stick (known residual defect).
    port = args.port
    bind = args.bind or args.posture   # personal→127.0.0.1, server→0.0.0.0
    info(f"setting gateway port {port} (bind={bind})")
    # --force: reapply_stack (above) has just published this brain's gateway on :port, and
    # teardown cleared the registry row — so gateway_port's live-listener check would die
    # ("port already in use ... pass --force if it's THIS brain's own gateway"). During deploy
    # the listener IS always this brain's own gateway re-asserting its port, so we force past it.
    # Cross-brain reservations are still caught FIRST (assert_port_free step (a)), before --force
    # applies — so this never lets two brains collide on a port.
    run([sys.executable, str(gport), "--brain", args.brain, "set",
         "--port", str(port), "--bind", bind, "--force"])
    ok(f"gateway port {port} set")


# ---------------------------------------------------------------------------
# Stage: ollama models (converge the store to the roster BEFORE neurons start)
# ---------------------------------------------------------------------------

def ollama_models(args):
    """Pull the brain's DECLARED models (brain_etc/ollama/models) into the sealed Ollama store
    so the embedder AND the action neuron's synthesis LLM are present BEFORE the action bundle
    starts. Without this a fresh deploy comes up with an empty store and /ask 404s on a model
    the box never pulled — the roster is authoritative, but nothing else in the deploy pulls it.
    Additive (--no-remove): never deletes an operator-added model. Idempotent + re-runnable:
    ollama_models.py skips models already present, so a transient registry EOF is recovered by
    re-running deploy (or `ollama_models.py sync`); `ollama pull` itself resumes partial blobs."""
    _, brain_dir = brain_paths(args)
    tool = brain_dir / "system" / "brain_sbin" / "ollama_models.py"
    if not tool.is_file():
        warn("ollama_models.py not staged — skipping model sync. Pull manually "
             "(system/brain_sbin/ollama_models.py --brain <brain> sync) or /ask will 404.")
        return
    # Retry the whole sync a few times: each `sync` is idempotent (present models are skipped),
    # and this box's registry link EOFs mid-pull intermittently, so a later pass completes what
    # an earlier one left partial. Also covers ollama not being ready the instant the stack came up.
    attempts = 3
    for n in range(1, attempts + 1):
        info(f"sync ollama store -> roster (brain_etc/ollama/models), attempt {n}/{attempts}")
        with heartbeat(f"pulling ollama models (attempt {n}/{attempts})", interval=30):
            rc = run([sys.executable, str(tool), "--brain", args.brain, "sync", "--no-remove"],
                     check=False).returncode
        if rc == 0:
            ok("ollama store in sync with roster (embedder + synthesis LLM present)")
            return
        if n < attempts:
            warn(f"model sync incomplete (rc={rc}) — likely a transient registry EOF or ollama "
                 "still warming; retrying")
    warn("ollama model sync did not fully converge after retries. The stack is up, but /ask may "
         "404 until the LLM finishes pulling. Re-run: system/brain_sbin/ollama_models.py "
         "--brain <brain> sync")


# ---------------------------------------------------------------------------
# Stage: verify (TLS heartbeat + reset=403 through the gateway)
# ---------------------------------------------------------------------------

def verify(args):
    """Prove the deployment: a TLS heartbeat over the gateway CA returns 200 JSON,
    and the reset endpoint returns 403 (write-sealed). Runs through the brain."""
    _, brain_dir = brain_paths(args)
    run_as_brain = brain_dir / "system" / "brain_sbin" / "run_as_brain.py"
    if not run_as_brain.is_file():
        die(f"run_as_brain not staged: {run_as_brain}")

    port = args.port
    _, brain_dir = brain_paths(args)

    # --- NIC gate (run FIRST — everything below it is blind to this fault) ------------------
    # Every other check in this stage rides 127.0.0.1 INSIDE the distro: the heartbeat, the 403
    # admission gates, `docker ps`. Loopback works perfectly on a VM with NO network interface at
    # all, so a NIC-less VM scored a full VERIFY PASSED — a brain that answers itself and can
    # reach nothing on the LAN. That false-green is what this gate exists to kill, so it must be
    # fatal and it must run before the probes that cannot see the problem.
    #
    # Two independent facts, both required (either alone still false-greens):
    #   * a global-scope IPv4 on a NON-lo link — the trap is 10.255.255.254/32, which WSL parks on
    #     `lo` at global scope; it satisfies `scope global` while carrying nothing, so `lo` is
    #     excluded by NAME, not by scope.
    #   * a default route — an address with no way off the link is still not reachability.
    addr_cmd = "ip -o -4 addr show scope global"
    rc, out, e = run_out(run_as_brain_argv(run_as_brain, args.brain, addr_cmd))
    # run_as_brain glues its identity banner onto the same stdout stream (see _http_code) — drop it
    # before parsing, or the banner line itself parses as a bogus interface.
    nics = []
    for line in (out or "").splitlines():
        f = line.split()
        # `ip -o` emits: '2: eth0    inet 192.168.1.5/24 brd ... scope global eth0'
        if len(f) >= 4 and f[0].rstrip(":").isdigit() and f[2] == "inet" and f[1] != "lo":
            nics.append(f"{f[1]}={f[3]}")
    if not nics:
        die("VERIFY FAILED — the distro has NO usable network interface: "
            f"`{addr_cmd}` returned no global-scope IPv4 on any non-lo link.\n"
            f"    expected: at least one non-lo link with an address, e.g. '2: eth0    inet 192.168.1.5/24'\n"
            f"    found:    {' | '.join(l.strip() for l in (out or '').splitlines() if l.strip()) or '<no output — loopback only>'}\n"
            "    Loopback-only means every check below this one would still pass while the brain\n"
            "    cannot reach the LAN or anything else. Note 10.255.255.254/32 sits on `lo` and is\n"
            "    a WSL artifact — it is NOT a NIC.\n"
            "    This host requires networkingMode=mirrored at the BRAIN account's real\n"
            "    %UserProfile%\\.wslconfig; confirm it is set there, then `wsl --shutdown` and\n"
            f"    re-boot the distro so the VM comes up with a link.{(' rc=' + str(rc)) if rc else ''}\n{e}")
    rc, out, e = run_out(run_as_brain_argv(run_as_brain, args.brain, "ip route"))
    routes = out or ""
    if "default via" not in routes:
        die(f"VERIFY FAILED — the distro has a NIC ({', '.join(nics)}) but NO default route: "
            "`ip route` has no `default via` line.\n"
            f"    routes: {' | '.join(routes.split(chr(10))) or '<none>'}\n"
            "    An address with no route off the link is not reachability — the brain is still\n"
            "    cut off from the LAN. This host requires networkingMode=mirrored at the BRAIN\n"
            "    account's real %UserProfile%\\.wslconfig; confirm it, then `wsl --shutdown` and\n"
            "    re-boot the distro.\n" + e)
    ok(f"NIC present with a default route ({', '.join(nics)}) — the VM is not loopback-only")

    # Prove the gateway is serving WITHOUT tripping its OWN fail2ban jail (bug 18). The gateway
    # jail bans a source IP after maxretry (default 20) `status:403` denials in findtime — and a
    # no-token heartbeat is DELIBERATELY a 403. Every host-originated probe reaches nginx as the
    # shared docker-bridge address, so a BURST of no-token probes (e.g. a readiness poll) self-bans
    # the verify client, after which every request dies at L3 (curl 000 / TLS error). So we do the
    # readiness WAIT on the reader-token path — a 200, which the jail does NOT count (nor is the
    # 000 of a connection refused during warm-up) — and hit the no-token 403 gate exactly ONCE.
    # That holds verify to a single 403 (+1 for reset), far under maxretry. (The old code polled the
    # no-token 403 and self-banned; it ALSO mis-parsed the glued curl+banner output — see _http_code.)
    reader = _read_seam_token(brain_dir, "reader")
    hb_notoken = (f"curl -s -o /dev/null -w '%{{http_code}}\\n' --cacert ~/gateway/gateway_out/cert.pem "
                  f"https://127.0.0.1:{port}/api/v2/heartbeat")
    cert_hint = ("  (rc 77 = can't read --cacert ~/gateway/gateway_out/cert.pem: the deployed engine "
                 "predates the cert rename — rebuild the engine from current canon.)")

    if reader:
        # Readiness gate + reader check in one: poll the READER heartbeat until 200. A 200 proves the
        # gateway is up, chroma is reachable THROUGH it, AND the bootstrap credential path works —
        # and none of these probes count toward the jail (200 on success, 000 while warming).
        hb_reader = (f"curl -s -o /dev/null -w '%{{http_code}}\\n' --cacert ~/gateway/gateway_out/cert.pem "
                     f"-H 'Authorization: Bearer {reader}' https://127.0.0.1:{port}/api/v2/heartbeat")
        rc, out, e, code = _probe_gateway(run_as_brain, args.brain, hb_reader, "200")
        if rc != 0 or code != "200":
            die(f"VERIFY FAILED — reader-token heartbeat expected 200, got '{code}' (rc={rc}). The "
                f"gateway is not serving, or the bootstrap reader token is not being accepted (map "
                f"not synced, or gateway not recreated after the seam apply).\n{cert_hint}\n{out}{e}")
        ok("reader-token heartbeat 200 — Chroma reachable through the gateway")

        # Mode C: a NO-TOKEN request MUST be refused (403). Readiness is already proven, so this is a
        # SINGLE shot — no poll, no 403 burst, no self-ban.
        rc, out, e = run_out(run_as_brain_argv(run_as_brain, args.brain, hb_notoken))
        code = _http_code(out)
        if code != "403":
            die(f"VERIFY FAILED — no-token heartbeat expected 403 (mode C gate closed), got '{code}' "
                f"(rc={rc}).\n   (HTTP 200 = the gateway is running read-open mode B, not mode C.)\n{out}{e}")
        ok(f"no-token heartbeat 403 on :{port} (mode C — admission gate closed)")
    else:
        # No reader token to prove readiness with an uncounted 200 — fall back to polling the no-token
        # 403 for readiness, but CAP the attempts well under maxretry so the poll itself cannot self-ban.
        warn("no reader token in brain_etc/gateway/reader_tokens.map — using the no-token gate for readiness")
        rc, out, e, code = _probe_gateway(run_as_brain, args.brain, hb_notoken, "403", timeout=24, interval=4)
        if rc != 0 or code != "403":
            die(f"VERIFY FAILED — no-token heartbeat expected 403 (mode C gate closed), got '{code}' "
                f"(rc={rc}).\n{cert_hint}\n   (HTTP 200 = read-open mode B, not mode C.)\n{out}{e}")
        ok(f"no-token heartbeat 403 on :{port} (mode C — admission gate closed)")

    reset = (f"curl -s -o /dev/null -w '%{{http_code}}\\n' --cacert ~/gateway/gateway_out/cert.pem "
             f"-X POST https://127.0.0.1:{port}/api/v2/reset")
    rc, out, e = run_out(run_as_brain_argv(run_as_brain, args.brain, reset))
    code = _http_code(out)
    if code != "403":
        warn(f"reset endpoint returned '{code}', expected 403 (write-sealed). "
             "Confirm the gateway authz posture.")
    else:
        ok("reset endpoint 403 (write-sealed) — gateway posture correct")

    # --- Per-service liveness (ADR-0015 is a MULTI-service stack) -------------------------
    # A single chroma TLS heartbeat does not prove ollama or the neuron bundles are up. Assert
    # the whole path-router stack is running (by container), not just the one route above.
    ps = "docker ps --format '{{.Names}}'"
    rc, out, e = run_out(run_as_brain_argv(run_as_brain, args.brain, ps))
    names = out or ""
    def _up(needle): return needle in names
    b = args.brain
    # Core stack: gateway + chroma are load-bearing (fatal); ollama + fail2ban are posture-
    # dependent (warn). Neuron bundles are informational (scaffold deploys have none).
    if not _up(f"{b}-gateway"):
        die(f"VERIFY FAILED — {b}-gateway container is not running; the path-router is down.\n"
            f"    running: {names.strip() or '<none>'}")
    if not _up(f"{b}-chroma"):
        die(f"VERIFY FAILED — {b}-chroma container is not running; the vector backend is down.\n"
            f"    running: {names.strip() or '<none>'}")
    ok(f"path-router core up: {b}-gateway + {b}-chroma running")
    if _up(f"{b}-ollama"):
        ok(f"ollama route backend up: {b}-ollama running")
    else:
        warn(f"{b}-ollama not running (OLLAMA_EXPOSE off, or ollama not yet up) — the LLM "
             "route will not answer until it is started")
    # --- Neuron liveness ------------------------------------------------------------------
    # This check used to filter `docker ps` for "_input_"/"_action_" — substrings that CANNOT
    # occur: containers are named <brain>-<service> (sorcerypunk_dev-input_neuron_example), so
    # the separator is a DASH. It therefore always reported "no neuron bundle containers
    # running" and blamed a "TEMPLATE-scaffold deploy", while a crash-exited neuron sailed
    # through as a WARN. That is how a brain with a DEAD ingest side scored VERIFY PASSED.
    #
    # Two fixes: match the real naming scheme, and ask the right question. `docker ps` lists
    # only RUNNING containers, so a crashed neuron was invisible to it — use `-a`. And "is it
    # running?" is wrong for neurons: input neurons and the CLI action neuron are ONE-SHOT
    # jobs that exit 0 when their work is done; only the API action neuron is long-running.
    # Exited(0) is SUCCESS. A NON-ZERO exit never is, so it is fatal.
    psa = "docker ps -a --format '{{.Names}}|{{.Status}}'"
    rc, out, e = run_out(run_as_brain_argv(run_as_brain, args.brain, psa))
    rows = [l.strip() for l in (out or "").splitlines() if "|" in l]
    neurons = [r for r in rows
               if r.split("|", 1)[0].startswith(f"{b}-") and "neuron" in r.split("|", 1)[0]]
    if not neurons:
        # A brain that declares no neurons in the brain.env zone renders no neuron services —
        # genuinely nothing to check (the real scaffold case the old message guessed at).
        warn("no neuron bundle containers exist (no neurons declared in the brain.env neuron "
             "zone; populate system/common_neuron_platform/{input,action}/ and re-run the "
             "neuron-bundles stage if this brain should have them)")
    else:
        dead = []
        for r in neurons:
            name, status = (x.strip() for x in r.split("|", 1))
            m = re.match(r"Exited \((\d+)\)", status)
            if m and m.group(1) != "0":
                dead.append(f"{name} [{status}]")
        if dead:
            die("VERIFY FAILED — neuron container(s) exited non-zero:\n    "
                + "\n    ".join(dead)
                + "\n    The bundle is dead, so this brain's ingest/query side does not work. "
                  "Diagnose with: run_as_brain.py --brain <brain> --wsl -- docker logs <name>")
        ok(f"neuron bundle(s) healthy: {len(neurons)} container(s) — "
           f"{', '.join(sorted(r.split('|', 1)[0] for r in neurons))} "
           f"(one-shot ingest/CLI neurons exit 0 when done; only the action API stays up)")

    # --- Persistence gate -----------------------------------------------------------------
    # The heartbeat above proves the stack is up RIGHT NOW. It does NOT prove the gateway
    # survives WSL idle-shutdown or a reboot — that is what the boot residency task is for.
    # Asserting the task is *Running* (holding the distro resident) is the deterministic proxy
    # for persistence: a true idle test needs minutes, and the reboot proof is LIFECYCLE-02.
    # Skipping this check is exactly what let a deploy false-green — stack up at deploy time,
    # keepalive dead, gateway gone minutes later. Verify now refuses to pass on that.
    if getattr(args, "skip_residency", False):
        warn("residency skipped (--skip-residency) — persistence across idle/reboot is NOT "
             "guaranteed; the gateway lives only while the distro is held open by hand")
    else:
        exists, running, last = residency_task_running(args.brain)
        if not exists:
            die("VERIFY FAILED — no residency task registered, so nothing holds the distro\n"
                "    resident; the gateway will vanish when WSL idles the distro down. Deploy\n"
                "    without --skip-residency (or register residency), then re-verify.")
        if not running:
            die(f"VERIFY FAILED — residency task '{residency_task(args.brain)}' exists but is\n"
                f"    NOT running (Last Result {last}); the keepalive is not holding the distro,\n"
                "    so persistence across idle/reboot is NOT proven. This is the false-green the\n"
                "    verify gate now catches. Check: schtasks /query /tn "
                f"\"{residency_task(args.brain)}\" /v /fo LIST,\n"
                "    and that the brain holds SeBatchLogonRight (267011 = missing that right).")
        ok(f"residency task holding the distro resident (Running, last result {last}) — "
           "persistence wired")

    ok("VERIFY PASSED")


# ---------------------------------------------------------------------------
# Verb: deploy
# ---------------------------------------------------------------------------

def cmd_deploy(args):
    validate_brain_name(args.brain)
    # --dry-run only previews the from-scratch build (which honors it) then stops — the
    # rest of deploy (installer/gateway/verify) has live side effects and is not dry-runnable.
    if getattr(args, "dry_run", False):
        if not getattr(args, "from_scratch", False):
            die("--dry-run only previews the --from-scratch engine build; the rest of deploy "
                "has live side effects and is not dry-runnable. Add --from-scratch, or drop --dry-run.")
        banner(f"DRY-RUN: from-scratch engine build for {args.brain} (deploy stages NOT run)")
        build_engine(args)
        banner("DRY-RUN complete — no changes made; deploy stages were skipped")
        return
    banner(f"Deploy brain: {args.brain}  (posture={args.posture})")
    # Export the resolved install root so staged child CLIs that read $AIOS_INSTALL_ROOT
    # (gateway_port.py's host-scoped port registry, etc.) inherit it via run()'s
    # env-inheritance. Derived from the explicit --install-root — propagation, not a guess.
    _root, _ = brain_paths(args)
    os.environ[INSTALL_ROOT_ENV] = str(_root)
    _export_provider_keyring_seam()
    total = 10 if not args.skip_gateway else 6

    stage(1, total, "Preflight");                 preflight(args)
    stage(2, total, "Create brain");              create_brain(args)
    stage(3, total, "Stage code (source/ → brain)"); stage_package(args)
    stage(4, total, "Build engine (from scratch, forced)" if getattr(args, "from_scratch", False)
                    else "Engine (reuse if present, else build from scratch)"); ensure_engine(args)
    stage(5, total, "Deploy engine");             deploy_engine(args)
    stage(6, total, "Config-exposure seam");      seam(args)
    if not args.skip_gateway:
        stage(7, total, "Gateway (port + token)"); gateway(args)
        stage(8, total, "Ollama models");         ollama_models(args)
        stage(9, total, "Neuron bundles");        neuron_bundles(args)
        stage(10, total, "Verify");               verify(args)
    else:
        info("--skip-gateway: engine deployed; gateway + neuron bundles + verify skipped")

    finalize_engine_artifact(args)
    banner(f"DEPLOY COMPLETE: {args.brain}")


# ---------------------------------------------------------------------------
# Verb: build-engine (standalone — produces the tar, does NOT deploy)
# ---------------------------------------------------------------------------

def cmd_build_engine(args):
    validate_brain_name(args.brain)
    build_engine(args)


# ---------------------------------------------------------------------------
# Verb: teardown
# ---------------------------------------------------------------------------

def _rmtree_clear_readonly(path):
    """shutil.rmtree for a Windows PROFILE tree — clears READONLY on the way down.

    Windows stamps the READONLY attribute on a profile's shell folders (Documents, Music,
    Pictures, Videos, Favorites, WinX/GroupN...) to mark them shell-customized (desktop.ini).
    A READONLY *directory* cannot be removed, and the failure surfaces as a bare
    WinError 5 "Access is denied" — indistinguishable from a permissions problem. That
    misled this tool for a long time: the old handler blamed "a loaded NTUSER.DAT hive,
    remove after a reboot", but a reboot does NOT clear an attribute, so the profile
    survived every teardown. Each survivor made the next deploy mint a NEW SID and a
    <name>.NNN profile, leaving an orphaned ProfileList entry per cycle (the .000->.010
    pileup). Elevation/takeown/icacls never helped because it was never an ACL.

    os.chmod on Windows maps to exactly one thing: the READONLY bit. Clear it, retry.
    """
    def _retry(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)      # Windows: clears READONLY (dirs included)
        func(p)
    # 3.12 deprecated onerror in favour of onexc; the callable signature is compatible here.
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_retry)
    else:
        shutil.rmtree(path, onerror=_retry)


# --- Profile handle release (the WinError 32 layer, under the READONLY one) ------------
#
# Clearing READONLY (above) fixed WinError 5. Underneath it sits a SECOND blocker that
# surfaces as WinError 32 "used by another process", and it has two distinct causes:
#
#   1. The account's registry hives (HKU\<SID> + HKU\<SID>_Classes) stay MOUNTED after the
#      account is deleted. A mounted hive IS an open handle on NTUSER.DAT / UsrClass.dat, so
#      no delete can win until it is unloaded. This is the grain of truth in the old "loaded
#      NTUSER.DAT hive" warning — but its remedy was wrong: a reboot is NOT required, and
#      `reg unload` alone returns ERROR_ACCESS_DENIED here because (a) an elevated token holds
#      SeRestorePrivilege DISABLED and (b) open keys remain inside the hive. Enabling the
#      privilege and calling NtUnloadKey2(REG_FORCE_UNLOAD) evicts it in place, no reboot.
#
#   2. A live process holds a file INSIDE the profile. Observed: wslsettings.exe pinning
#      %LocalAppData%Low\Intel\ShaderCache\*. `wsl --shutdown` does NOT kill it (it stops
#      distros, not the WSL GUI apps), so it silently leaked the profile on every teardown.
#
# Both are diagnosed/handled here so teardown completes unaided. Restart Manager names the
# actual holder, which is the difference between a 1-minute fix and another reboot hunt.

_REG_FORCE_UNLOAD = 1
_SE_PRIVILEGE_ENABLED = 0x2
_TOKEN_ADJUST_PRIVILEGES = 0x20
_TOKEN_QUERY = 0x8
_OBJ_CASE_INSENSITIVE = 0x40


def _enable_privilege(name):
    """Enable a privilege the current (elevated) token already HOLDS but leaves DISABLED.

    Elevation grants SeRestorePrivilege but does not switch it on; hive unload fails with
    ERROR_PRIVILEGE_NOT_HELD (1314) until it is enabled. Returns True on success.
    """
    import ctypes
    from ctypes import wintypes

    class _LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", ctypes.c_long)]

    class _LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", _LUID), ("Attributes", wintypes.DWORD)]

    class _TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD),
                    ("Privileges", _LUID_AND_ATTRIBUTES * 1)]

    # use_last_error=True is REQUIRED: AdjustTokenPrivileges reports the "privilege not in
    # token" case only via GetLastError, and ctypes.get_last_error() reads nothing unless the
    # DLL was loaded this way.
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # Explicit prototypes are NOT optional on x64: without a restype, GetCurrentProcess's
    # (HANDLE)-1 pseudo-handle comes back as a 32-bit int and is passed truncated, and the
    # token/LUID pointers are likewise mis-marshalled. The symptom is a silent False here
    # and STATUS_PRIVILEGE_NOT_HELD from the unload.
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                               ctypes.POINTER(_LUID)]
    advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
    advapi32.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL,
                                               ctypes.POINTER(_TOKEN_PRIVILEGES),
                                               wintypes.DWORD, ctypes.c_void_p,
                                               ctypes.c_void_p]
    advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                     _TOKEN_ADJUST_PRIVILEGES | _TOKEN_QUERY,
                                     ctypes.byref(token)):
        return False
    try:
        luid = _LUID()
        if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            return False
        tp = _TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = _SE_PRIVILEGE_ENABLED
        ctypes.set_last_error(0)
        if not advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(tp),
                                              ctypes.sizeof(tp), None, None):
            return False
        # AdjustTokenPrivileges "succeeds" with ERROR_NOT_ALL_ASSIGNED (1300) when the token
        # does not hold the privilege at all — only GetLastError separates that from success.
        return ctypes.get_last_error() == 0
    finally:
        kernel32.CloseHandle(token)


def _force_unload_profile_hives(sid):
    """Force-unload HKU\\<sid> and HKU\\<sid>_Classes. Returns a list of human-readable results.

    Force (rather than a plain unload) because open keys inside the hive otherwise make it
    unloadable; the owning account is already gone at this point, so nothing legitimate is
    still reading it.
    """
    import ctypes
    from ctypes import wintypes

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", ctypes.c_ushort),
                    ("MaximumLength", ctypes.c_ushort),
                    ("Buffer", ctypes.c_wchar_p)]

    class _OBJECT_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Length", ctypes.c_ulong),
                    ("RootDirectory", ctypes.c_void_p),
                    ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
                    ("Attributes", ctypes.c_ulong),
                    ("SecurityDescriptor", ctypes.c_void_p),
                    ("SecurityQualityOfService", ctypes.c_void_p)]

    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtUnloadKey2.argtypes = [ctypes.POINTER(_OBJECT_ATTRIBUTES), ctypes.c_ulong]
    ntdll.NtUnloadKey2.restype = ctypes.c_long
    _enable_privilege("SeRestorePrivilege")
    _enable_privilege("SeBackupPrivilege")

    results = []
    for key in (f"{sid}_Classes", sid):
        nt_path = f"\\Registry\\User\\{key}"
        name = _UNICODE_STRING()
        name.Buffer = nt_path
        name.Length = len(nt_path) * 2
        name.MaximumLength = name.Length + 2
        oa = _OBJECT_ATTRIBUTES()
        oa.Length = ctypes.sizeof(_OBJECT_ATTRIBUTES)
        oa.RootDirectory = None
        oa.ObjectName = ctypes.pointer(name)
        oa.Attributes = _OBJ_CASE_INSENSITIVE
        status = ntdll.NtUnloadKey2(ctypes.byref(oa), _REG_FORCE_UNLOAD) & 0xFFFFFFFF
        # "Not loaded" is the COMMON, healthy case (a brain that never logged on, or a rerun),
        # so it must not read as a failure. Windows reports it as STATUS_INVALID_PARAMETER
        # (0xC000000D) when the name is not a mounted hive root — verified empirically against
        # both an absent SID and a real `reg load`ed hive; STATUS_OBJECT_NAME_NOT_FOUND
        # (0xC0000034) is accepted too for the paths that do report it.
        if status == 0:
            results.append(f"unloaded HKU\\{key}")
        elif status in (0xC000000D, 0xC0000034):
            results.append(f"HKU\\{key} not loaded (nothing to do)")
        else:
            results.append(f"HKU\\{key} unload FAILED (NTSTATUS 0x{status:08X})")
    return results


def _profile_lockers(paths):
    """Restart Manager: [(pid, app_name), ...] for processes holding handles on `paths`.

    Named holders turn "Access is denied" into "wslsettings.exe holds this file" — the whole
    reason this teardown chased reboots for months instead of killing one process.
    """
    import ctypes
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class _RM_UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", _FILETIME)]

    class _RM_PROCESS_INFO(ctypes.Structure):
        _fields_ = [("Process", _RM_UNIQUE_PROCESS),
                    ("strAppName", ctypes.c_wchar * 256),
                    ("strServiceShortName", ctypes.c_wchar * 64),
                    ("ApplicationType", ctypes.c_uint),
                    ("AppStatus", ctypes.c_ulong),
                    ("TSSessionId", wintypes.DWORD),
                    ("bRestartable", wintypes.BOOL)]

    try:
        rm = ctypes.WinDLL("rstrtmgr")
    except OSError:
        return []

    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(33)
    if rm.RmStartSession(ctypes.byref(session), 0, key) != 0:
        return []
    try:
        arr = (ctypes.c_wchar_p * len(paths))(*paths)
        if rm.RmRegisterResources(session, len(paths), arr, 0, None, 0, None) != 0:
            return []
        needed, count, reasons = wintypes.UINT(), wintypes.UINT(0), wintypes.DWORD()
        rc = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None,
                          ctypes.byref(reasons))
        if rc != 234 or needed.value == 0:      # 234 = ERROR_MORE_DATA (i.e. there ARE lockers)
            return []
        count = wintypes.UINT(needed.value)
        info_arr = (_RM_PROCESS_INFO * needed.value)()
        if rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), info_arr,
                        ctypes.byref(reasons)) != 0:
            return []
        return [(info_arr[i].Process.dwProcessId, info_arr[i].strAppName)
                for i in range(count.value)]
    finally:
        rm.RmEndSession(session)


def _stop_wsl_vm():
    """Stop the WSL utility VM and its GUI apps, which hold files inside a brain profile.

    Unconditional: Restart Manager CANNOT name vmmemWSL — AppData\\Local\\Temp\\<guid>\\swap.vhdx
    is owned by the utility VM, not by a user-mode process with an ordinary handle — so
    _profile_lockers finds nobody and there is no PID to kill. Unregistering the distro does not
    stop the VM; `wsl --shutdown` is the only thing that releases the VHDX. It does NOT stop the
    WSL GUI apps, so wslsettings is killed by name afterwards.

    Safe here: the brain's distro is already unregistered, and other distros restart on next use.
    """
    info("  stopping the WSL VM (holds swap.vhdx inside the brain profile)")
    run(["wsl", "--shutdown"], check=False)
    run(["taskkill", "/IM", "wslsettings.exe", "/F"], check=False)
    _wait_for_wsl_vm_exit()


# The host virtualization stack that caches mirrored-networking state, in STOP order.
# Start order is the exact reverse (hns -> vmcompute -> WslService).
#
# The failure being cleared: a created VM fails ConfigureNetworking (0x8007054f) and WSL SILENTLY
# falls back to `networkingMode None` — loopback-only, no eth0, no default route, no resolv.conf.
# WSL prints that fallback ("Failed to configure network (networkingMode Mirrored), falling back
# to networkingMode None") in UTF-16, so it does NOT survive into the deploy log and the VM just
# looks inexplicably NIC-less. `.wslconfig` is a red herring here: mirrored IS set and IS read —
# the host cannot APPLY it.
#
# THE WHOLE VIRTUALIZATION+NETWORK STACK GOES DOWN TOGETHER, NOT A CURATED SUBSET.
# Every attempt to identify "the one service that matters" has been wrong, because a
# still-running neighbour re-registers the stale state into whichever service just restarted.
# Narrowing this list is how this bug came back three times. If you are tempted to trim it,
# read the measurements below first.
#
# MEASURED 2026-07-15, each verified against a live VM:
#   WslService alone ............... NO NIC  (once shipped as "THE REBOOT KILLER")
#   hns alone ...................... NO NIC
#   WslService + hns ............... NO NIC
#   WslService + vmcompute + hns ... NO NIC  ← shipped as ✅ PROVEN; it is NOT.
#       One green run made it look right. It then ran 3x inside a single from-scratch deploy
#       and the runtime VM STILL came up networkingMode None — that deploy failed at verify.
#   ALL NINE BELOW ................. eth0 <LAN_IP>/22 + default via <LAN_GATEWAY_IP>
#                                    + ping <LAN_GATEWAY_IP> 0% loss  ✅ (no reboot)
#
# This is why "just reboot" looked like the only remedy: a reboot cycles the WHOLE stack. It was
# never evidence that a reboot is required — only that the subset was too small. Do not
# reintroduce a reboot as the recommended fix.
#
# ⚠️ BLAST RADIUS: this stops ALL Hyper-V compute + host virtual networking (every WSL VM, any
# Windows container, any Hyper-V VM) on the box, not just this brain's. Other brains take an
# outage; their residency tasks bring them back.
_WSL_HOST_NET_SERVICES = ["WslService", "vmcompute", "hns", "nvagent", "WinNat",
                          "SharedAccess", "vmms", "HvHost", "NetSetupSvc"]


def _reset_wsl_host_network():
    """Cycle the host virtualization stack so the NEXT VM gets a NIC. No reboot needed.

    All three go DOWN together and only then come back UP: restarting them one-by-one lets a
    still-running neighbour re-register the stale state into the service that just restarted,
    which is precisely why the piecemeal restarts above measured as no-ops.

    Stopping the stack also kills vmmemWSL synchronously, which is what makes the swap.vhdx
    handle release deterministic rather than a race against an async `wsl --shutdown`.

    This does NOT assert the outcome — it cannot see a VM from here. Whether a NIC actually
    appeared is verify()'s job (it asserts a non-lo global IPv4 + a default route against the
    real VM). Claiming "the next VM will get a NIC" here is exactly the false green that let a
    broken reset look fixed for a week.
    """
    # Derive the list from the constant — never restate it. A hardcoded "(WslService + vmcompute
    # + hns)" here survived the widening to nine and printed a stale three-service claim.
    info(f"  resetting host WSL network state ({' + '.join(_WSL_HOST_NET_SERVICES)}) "
         f"— no reboot needed")
    run_out(["wsl", "--shutdown"])

    failed = []
    for svc in _WSL_HOST_NET_SERVICES:                       # down, in dependency order
        # Skip a service this host does not have (the list spans Hyper-V + WSL + NAT/ICS, and
        # not every box carries all nine) — absent is not a failure worth warning about.
        rc, _out, err = run_out(["powershell", "-NonInteractive", "-NoProfile", "-Command",
                                 f"if (Get-Service {svc} -ErrorAction SilentlyContinue) "
                                 f"{{ Stop-Service {svc} -Force }}"])
        if rc != 0:
            failed.append(f"stop {svc}: {(err or '').strip()}")
    for svc in reversed(_WSL_HOST_NET_SERVICES):             # up, reverse order
        rc, _out, err = run_out(["powershell", "-NonInteractive", "-NoProfile", "-Command",
                                 f"if (Get-Service {svc} -ErrorAction SilentlyContinue) "
                                 f"{{ Start-Service {svc} }}"])
        if rc != 0:
            failed.append(f"start {svc}: {(err or '').strip()}")

    if failed:
        warn("host WSL network reset did not fully succeed — a VM may come up loopback-only "
             "(no eth0). Details: " + "; ".join(failed))
    else:
        info("  host WSL network state reset (stack cycled; a NIC is not asserted here — "
             "verify() proves it against the real VM)")


def _wait_for_wsl_vm_exit(timeout=90):
    """Block until the WSL utility VM process is really gone.

    `wsl --shutdown` is ASYNCHRONOUS: it returns once the shutdown is REQUESTED, while vmmemWSL
    lives on for seconds still holding swap.vhdx. Deleting the profile immediately after the
    call races the VM and loses (WinError 32), with the VM exiting moments later — leaving
    residue that mints the next deploy's <name>.NNN profile.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc, out, _ = run_out(["powershell", "-NonInteractive", "-NoProfile", "-Command",
                              "@(Get-Process -Name vmmemWSL,vmmem "
                              "-ErrorAction SilentlyContinue).Count"])
        if (out or "").strip() == "0":
            info("  WSL VM stopped (swap.vhdx released)")
            return True
        time.sleep(1)
    warn(f"  WSL VM still running {timeout}s after `wsl --shutdown` — the profile delete will "
         f"likely fail with WinError 32 on swap.vhdx")
    return False


def _release_profile_handles(profile, sid):
    """Free a torn-down brain profile so it can actually be deleted. No reboot, ever.

    Stops the WSL VM, unloads the leftover hives, then reports/clears any process still holding
    a file inside the profile. Only WSL-owned holders are killed: teardown has already
    unregistered the distro, so they have no business in this profile, and they are trivially
    restartable. Anything else is REPORTED, never killed — a teardown must not shoot down a
    user's apps.
    """
    _stop_wsl_vm()
    if sid:
        for line in _force_unload_profile_hives(sid):
            info(f"  profile hive: {line}")
    else:
        warn("  brain SID unresolved — leftover HKU hives (if any) not unloaded; "
             "a profile delete may fail with WinError 32")

    files = []
    for root, _dirs, names in os.walk(profile):
        files.extend(os.path.join(root, n) for n in names)
        if len(files) > 500:                      # RM only needs a representative sample
            break
    if not files:
        return
    for pid, app in _profile_lockers(files):
        image = (app or "").lower()
        if "wsl" in image:
            info(f"  releasing WSL holder of the brain profile: {app} (PID {pid})")
            run(["taskkill", "/PID", str(pid), "/F"], check=False)
        else:
            warn(f"  {app} (PID {pid}) holds a file inside {profile} and was NOT killed — "
                 f"close it and re-run teardown if the profile delete below fails")


def cmd_teardown(args):
    validate_brain_name(args.brain)
    destructive = args.purge
    banner(f"Teardown brain: {args.brain}  ({'PURGE' if destructive else 'stop/reset'})")

    if destructive and not args.yes:
        die("--purge is destructive (unregisters the distro → deletes all engine data,\n"
            "    removes the account, deletes the brain folder). Re-run with --yes to confirm.")

    root, brain_dir = brain_paths(args)
    # Export the resolved install root so staged child CLIs that read $AIOS_INSTALL_ROOT
    # (gateway_port.py release, etc.) inherit it — same contract as deploy.
    os.environ[INSTALL_ROOT_ENV] = str(root)
    _export_provider_keyring_seam()

    # 1. Residency task — stop + delete.
    if residency_task_exists(args.brain):
        run(["schtasks", "/end", "/tn", residency_task(args.brain)], check=False)
        run(["schtasks", "/delete", "/tn", residency_task(args.brain), "/f"], check=False)
        ok("residency task deleted")
    else:
        info("no residency task registered")

    # 2. Gateway release (firewall rule + registry row).
    gport = brain_dir / "system" / "brain_sbin" / "gateway_port.py"
    if gport.is_file():
        run([sys.executable, str(gport), "--brain", args.brain, "release"], check=False)
        ok("gateway released")

    # 3. Distro — terminate (non-destructive) or unregister (purge, deletes data).
    #
    # AS THE BRAIN, not this host session. brain-<brain> is registered in the BRAIN
    # account's per-user WSL hive and is invisible to this elevated host/owner logon
    # (the same per-user invisibility documented on distro_exists / distro_imported_as_brain).
    # A `wsl --unregister` run HERE returns WSL_E_DISTRO_NOT_FOUND and NO-OPS, so ext4.vhdx
    # stays locked and remove_brain.py's rmtree fails (WinError 32) and leaks the folder
    # (NOTE 001-28). run_as_brain's account target loads the brain's HKCU, so `wsl` there
    # sees the per-user distro — mirroring how deploy runs `wsl -d brain-<brain>` as the brain.
    distro = distro_name(args.brain)
    verb, past = (("--unregister", "unregistered (data deleted)") if destructive
                  else ("--terminate", "terminated (data preserved — deploy to rebuild)"))
    run_as_brain = brain_dir / "system" / "brain_sbin" / "run_as_brain.py"
    if run_as_brain.is_file():
        run_out([sys.executable, str(run_as_brain), "--brain", args.brain,
                 "--", "wsl", verb, distro])
    else:
        warn(f"run_as_brain not staged ({run_as_brain}) — falling back to host-session "
             f"wsl {verb} (may no-op on a per-user distro; see NOTE 001-28)")
        run(["wsl", verb, distro], check=False)
    # Verify rather than trust: an unconditional ok() is exactly what masked the leak.
    # On purge the distro MUST be gone before the rmtree — fail loud if it survived.
    if destructive and run_as_brain.is_file() and distro_imported_as_brain(args):
        die(f"distro {distro} still present after unregister — aborting purge to avoid a\n"
            f"    locked-vhdx folder leak. Investigate (credential/profile load) before retrying.")
    ok(f"distro {distro} {past}")

    # 4. Purge only: remove the account + folder + the brain's RESOLVED Windows profile.
    if destructive:
        # Resolve the brain's REAL profile dir BEFORE removing the account — brain_profile_dir
        # (the Phase-6 resolver) reads ProfileList\<SID>\ProfileImagePath and needs the account
        # (Get-LocalUser) + its keystore credential to still exist. Doing it here deletes the REAL
        # profile even when Windows suffixed it (C:\Users\<brain>.<MACHINE> / .NNN), NOT a
        # string-built C:\Users\<brain> — a plain-path delete would MISS the suffixed dir and leak
        # it (the ~1 GB stale-profile pileup this teardown + the Phase-7 sweep exist to prevent).
        profile = brain_profile_dir(args.brain)
        # The SID must be captured HERE too: it keys the leftover HKU\<SID> hives that pin the
        # profile, and Get-LocalUser stops resolving the moment remove_brain deletes the account.
        sid = _brain_sid(args.brain)
        if PROVIDER_REMOVE_BRAIN is not None and PROVIDER_REMOVE_BRAIN.is_file():
            run([sys.executable, str(PROVIDER_REMOVE_BRAIN), args.brain, "--yes"], check=False)
        else:
            warn("no provider remove_brain on this host — remove the account + folder manually:\n"
                 f"      Remove-LocalUser {args.brain}; Remove-LocalGroup {args.brain}_group\n"
                 f"      Remove-Item -Recurse -Force \"{brain_dir}\"")
        # The profile lives under C:\Users — OUTSIDE the brain folder — so remove_brain's rmtree of
        # brain_dir never touches it. None-guard: if resolution failed (no credential / no
        # ProfileList entry), WARN + SKIP rather than guess-delete a string-built path (which could
        # hit an active or unsuffixed profile). Never guess a destructive path.
        if profile is None:
            warn(f"could not resolve {args.brain}'s real profile dir — its Windows profile was "
                 f"NOT removed (no guess-delete). Remove manually (elevated) via Win32_UserProfile "
                 f"filtered to C:\\Users\\{args.brain}*, excluding any active/unsuffixed profile.")
        elif profile.is_dir():
            info(f"removing resolved brain profile dir: {profile}")
            # Release BEFORE the rmtree: READONLY (WinError 5) is cleared on the way down by
            # _rmtree_clear_readonly, but mounted hives / live file holders (WinError 32) must
            # be dealt with up front — no amount of retrying beats an open handle.
            _release_profile_handles(profile, sid)
            try:
                _rmtree_clear_readonly(profile)
                ok(f"brain profile dir removed: {profile}")
            except OSError as e:
                warn(f"could not remove brain profile dir {profile} ({e}). A reboot is NOT the "
                     f"remedy and never was — WinError 5 means READONLY (cleared automatically "
                     f"above) and WinError 32 means a live handle. Re-run teardown; if it "
                     f"persists, the holder named above still has the file open. Do NOT leave "
                     f"it: every stale profile makes the next deploy mint a fresh SID + "
                     f"<name>.NNN profile (orphaning a ProfileList entry each cycle).")
        else:
            info(f"resolved brain profile dir {profile} already absent — nothing to remove")

        # The distro is gone and the profile is dealt with; now reset the HOST state it
        # orphaned, so the next deploy's VM is not born loopback-only. Last, because cycling
        # the service stack mid-teardown would fight the profile-handle release above.
        _reset_wsl_host_network()

        # Residue gate. A teardown that leaves the profile dir or its ProfileList row behind has
        # NOT torn down: the next deploy mints a suffixed profile off exactly this residue. This
        # used to print TEARDOWN COMPLETE and exit 0 over a profile still on disk, which is how a
        # broken teardown kept getting reported as a pass.
        residue = []
        if profile is not None and profile.is_dir():
            residue.append(f"profile dir still present: {profile}")
        if sid and _profilelist_entry_exists(sid):
            residue.append(f"ProfileList row still present: {sid}")
        if residue:
            for line in residue:
                err(line)
            die(f"TEARDOWN FAILED: {args.brain} — residue above will make the next deploy mint "
                f"a <name>.NNN profile. Do NOT deploy over this.", code=2)
    banner(f"TEARDOWN COMPLETE: {args.brain}")


# ---------------------------------------------------------------------------
# Verb: verify / status
# ---------------------------------------------------------------------------

def cmd_verify(args):
    validate_brain_name(args.brain)
    banner(f"Verify brain: {args.brain}")
    # verify() drives EVERY in-distro probe (NIC gate, gateway heartbeats, docker ps/-a)
    # through run_as_brain.py, because brain-<brain> is registered in the BRAIN account's
    # per-user WSL hive and is invisible to this elevated host/owner logon (NOTE 001-28; the
    # same per-user invisibility handled in cmd_teardown / distro_imported_as_brain). Those
    # hops only run NON-INTERACTIVELY if run_as_brain can resolve the brain credential from
    # the OS keystore — which needs the platform's namespace advertised via
    # $BRAIN_KEYRING_SERVICE. deploy's INLINE verify passes because cmd_deploy exports that
    # seam (+ install root) first; standalone verify did NOT, so the FIRST hop (the NIC gate)
    # fell through to run_as_brain's getpass fallback and blocked on stdin forever — the
    # observed "banner, then zero output, had to be killed" hang. Establish the SAME env
    # deploy/teardown set before hopping, so the hops authenticate and RETURN (a bad
    # credential surfaces as a nonzero rc that verify's checks already die on) instead of
    # blocking. Mirrors cmd_teardown (2951-2955) and cmd_deploy (2520-2522) exactly.
    root, _ = brain_paths(args)
    os.environ[INSTALL_ROOT_ENV] = str(root)
    _export_provider_keyring_seam()
    verify(args)


def cmd_status(args):
    validate_brain_name(args.brain)
    banner(f"Status: {args.brain}")
    root, brain_dir = brain_paths(args)
    print(f"  account exists   : {user_exists(args.brain)}")
    print(f"  brain folder     : {brain_dir}  ({'present' if brain_dir.is_dir() else 'MISSING'})")
    staged = (brain_dir / 'system' / 'brain_bin' / 'deploy' / 'brain_installer_1_admin.py').is_file()
    print(f"  code staged      : {staged}")
    engine = (wsl_runtime_dir(brain_dir) / f'{args.brain}_engine.tar').is_file()
    print(f"  engine artifact  : {engine}")
    print(f"  distro imported  : {distro_exists(args.brain)} ({distro_name(args.brain)})")
    print(f"  residency task   : {residency_task_exists(args.brain)} ({residency_task(args.brain)})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args():
    # -v on a parent so it is accepted on either side of the verb: `-v deploy`
    # and `deploy -v` both work. Without this it would be positional-order-
    # sensitive, which is a bad trait for a debug flag you reach for mid-failure.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true",
                        help="prefix every output line with the script:function that "
                             "emitted it (incl. the create-brain subprocess, which prints "
                             "the same prefixes as this script)")

    ap = argparse.ArgumentParser(
        description="Brain deploy orchestrator — one elevated entry, all blocks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deploy", help="stand up a brain end-to-end", parents=[common])
    d.add_argument("--brain", required=True)
    d.add_argument("--posture", choices=("personal", "server"), default="personal")
    d.add_argument("--port", type=int, default=8000,
                   help="chroma gateway host port (default 8000 — the standard chroma port, and "
                        "what brain_etc/brain.env's CHROMA_PORT seeds. Must match the seam: "
                        "gateway_port writes the runtime .env, but reapply then syncs the seam OVER "
                        "it, so a mismatch (the old 8443 default) lands chroma on the seam's 8000 "
                        "while verify probes 8443 and fails. 8443 is the ACTION surface, not chroma.)")
    d.add_argument("--bind", choices=("personal", "server", "127.0.0.1", "0.0.0.0"),
                   default=None, help="gateway bind (default: follow --posture)")
    d.add_argument("--engine-tar", default=None,
                   help="restore / build-from-existing: deploy from this prebuilt "
                        "<brain>_engine.tar instead of building. Opt-in — a from-scratch build "
                        "is the default when no engine is present.")
    d.add_argument("--from-scratch", action="store_true",
                   help="force a fresh engine build even if a distro/tar already exists (refuses "
                        "all reuse — a true cold start). NOTE: deploy already builds from scratch "
                        "by default when nothing is present; this flag only forces a rebuild.")
    d.add_argument("--export-engine", nargs="?", const=True, default=None, metavar="DIR",
                   help="disposition of the built engine tar after a successful deploy. Omitted → "
                        "delete it (default; it was only the Windows account-hop transfer medium). "
                        "Bare flag → move it to brains/<brain>/wsl_engine_export/. Given a DIR → move it there "
                        "(for backup/reinstall).")
    d.add_argument("--imagefile", default=None,
                   help="base rootfs/image to `wsl --import` instead of the "
                        "Store base (pinned/offline). Empty = pull latest Debian.")
    d.add_argument("--keep-scratch", action="store_true",
                   help="leave the brain-build-<brain> scratch build distro in "
                        "place after export (debug)")
    d.add_argument("--dry-run", action="store_true",
                   help="with --from-scratch: preview the engine-build commands only, then STOP "
                        "(the deploy stages have live side effects and are not dry-runnable)")
    d.add_argument("--install-root", default=None,
                   help="REQUIRED unless $AIOS_INSTALL_ROOT is set: the dir that holds "
                        "brains/<brain>/. Never guessed — no autodetect, no default.")
    d.add_argument("--skip-residency", action="store_true",
                   help="deploy the engine but do not register the boot residency task")
    d.add_argument("--skip-gateway", action="store_true",
                   help="stop after engine deploy (no gateway/token/verify)")
    d.set_defaults(func=cmd_deploy)

    b = sub.add_parser("build-engine",
                       help="build the <brain>_engine.tar from scratch (download base → provision "
                            "→ export); does NOT deploy",
                       parents=[common])
    b.add_argument("--brain", required=True)
    b.add_argument("--posture", choices=("personal", "server"), default="personal",
                   help="posture baked into the built engine (stage4 bind + stage7 harden); "
                        "default personal (deploy sets the live gateway bind)")
    b.add_argument("--imagefile", default=None,
                   help="base rootfs/image to `wsl --import` instead of the Store base "
                        "(pinned/offline). Empty = pull latest Debian via `wsl --install`.")
    b.add_argument("--keep-scratch", action="store_true",
                   help="leave the brain-build-<brain> scratch distro in place after export (debug)")
    b.add_argument("--dry-run", action="store_true",
                   help="print the exact wsl/provision commands it WOULD run, without running them")
    b.add_argument("--install-root", default=None,
                   help="REQUIRED unless $AIOS_INSTALL_ROOT is set: the dir that holds "
                        "brains/<brain>/. Never guessed — no autodetect, no default.")
    b.set_defaults(func=cmd_build_engine)

    t = sub.add_parser("teardown", help="stop/reset (default) or purge a brain", parents=[common])
    t.add_argument("--brain", required=True)
    t.add_argument("--purge", action="store_true",
                   help="destructive: unregister distro (delete data) + remove account + folder")
    t.add_argument("--yes", action="store_true", help="confirm a --purge")
    t.add_argument("--install-root", default=None,
                   help="REQUIRED unless $AIOS_INSTALL_ROOT is set: the dir that holds brains/<brain>/")
    t.set_defaults(func=cmd_teardown)

    v = sub.add_parser("verify", help="TLS heartbeat + reset=403 + residency-holding through the gateway",
                       parents=[common])
    v.add_argument("--brain", required=True)
    v.add_argument("--port", type=int, default=8000,
                   help="chroma gateway port to probe (default 8000 — the chroma surface)")
    v.add_argument("--install-root", default=None,
                   help="REQUIRED unless $AIOS_INSTALL_ROOT is set: the dir that holds brains/<brain>/")
    v.add_argument("--skip-residency", action="store_true",
                   help="do not assert the residency task is holding the distro (use only "
                        "when the brain was deployed --skip-residency)")
    v.set_defaults(func=cmd_verify)

    s = sub.add_parser("status", help="show what exists for a brain", parents=[common])
    s.add_argument("--brain", required=True)
    s.add_argument("--install-root", default=None,
                   help="REQUIRED unless $AIOS_INSTALL_ROOT is set: the dir that holds brains/<brain>/")
    s.set_defaults(func=cmd_status)

    return ap.parse_args()


def main():
    # Force UTF-8 for THIS process and every staged child tool we spawn (gateway_port.py,
    # ollama_models.py, brain_truths.py, the verify curls, …). Windows consoles default to a
    # legacy codepage (cp1252), so a child tool printing a non-ASCII char (e.g. gateway_port.py's
    # "gateway -> host:port" success line, U+2192) dies with UnicodeEncodeError and aborts the
    # deploy AFTER doing its real work — a false failure. setdefault is inherited by children at
    # their interpreter startup; reconfigure fixes our own already-open streams. (Idempotent.)
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # non-reconfigurable stream (already-wrapped pipe); PYTHONUTF8 still covers children
    require_supported_os()
    args = parse_args()
    global _VERBOSE
    _VERBOSE = bool(getattr(args, "verbose", False))
    # Every verb except status touches the box → require elevation up front.
    # build-engine drives `wsl --install/--import/--export` → also elevated.
    # (A --dry-run only prints commands, so it does not need elevation — this applies
    #  to build-engine AND deploy --from-scratch --dry-run, both of which just preview.)
    dry = getattr(args, "dry_run", False)
    if (args.cmd == "deploy" and not dry) or args.cmd == "teardown" or (
            args.cmd == "build-engine" and not dry):
        require_admin()
    args.func(args)


if __name__ == "__main__":
    main()
