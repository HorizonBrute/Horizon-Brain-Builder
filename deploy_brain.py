#!/usr/bin/env python3
"""
deploy_brain.py — the ONE brain deploy orchestrator (cross-platform)
====================================================================

Replaces the two platform drivers (`windows_deploy_brain.py`, `linux_deploy_brain.py`)
with a single tool whose build → deploy → verify PROCESS is shared, and where only the
genuinely OS-forced steps live behind a thin `PlatformBackend`. See the project plan
`docs/project_plans/001_detail-unify_deploy_brain.md` for the full design.

THE LIFECYCLE (identical on every OS)
    build-engine : provision a runtime, BAKE the TLS cert (no-arg gen-cert), prefetch
                   images/models/neurons, and SNAPSHOT it to a portable engine artifact.
    deploy       : RESTORE the engine into the brain, install the seam, run the gateway
                   stage (tokens + gateway_config + seam apply), sync models, start
                   neurons, verify.
    teardown / verify / status : the inverse + the health gates.

THE PLATFORM SEAM — the ONLY things that branch on OS (`PlatformBackend`)
    engine host + snapshot   WSL2 distro + `wsl --export/--import`   |  native rootless +
                                                                        `docker save`/`load`
                                                                        + config/cert bundle
    identity switch          run_as_brain.py --wsl -- …              |  sudo -u <brain> -H
    seam mount               drvfs 9p (ro) + icacls deny-ACE         |  bind mount (ro) + POSIX
    residency                Task Scheduler keepalive                |  systemd --user + linger
    firewall                 Windows Defender rule                   |  ufw/firewalld
Everything else — cert gen, token minting, gateway_config render, seam apply, model
sync, neuron bundles, the verify gates — is SHARED host-side code that calls the seam.

BUILD STATUS (this file is being built section by section; see the project plan)
    [DONE]    Section 1 — PlatformBackend interface + Linux/Windows backends (identity,
                          naming, privilege, account probes implemented; engine/seam/
                          residency/firewall are honest NotImplementedError stubs).
    [DONE]    Section 6 — shared cert stage: no-arg gen-cert (typed SAN only for server)
                          + hard rc check. Closes BUG-001-1 (the false-green that started
                          this project).
    [PENDING] Sections 2,3,4,5,7,8 — build-engine, Linux snapshot/restore, shared deploy,
                          full CLI, migration, dev_brain validation.

USAGE (verbs land as their sections do; unimplemented verbs say so)
    python3 deploy_brain.py status   --brain X [--install-root DIR]
    python3 deploy_brain.py selftest            # unit-checks the cert-arg contract (Section 6)
"""

import abc
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Constants (shared; naming that differs per OS lives in the backend)
# ---------------------------------------------------------------------------

MOUNT_POINT      = "/opt/brain_truths"                 # config-exposure seam (inside the brain view)
INSTALL_ROOT_ENV = "AIOS_INSTALL_ROOT"
BRAIN_NAME_RE    = re.compile(r"^[a-z][a-z0-9_]{1,19}$")
SAN_ENTRY_RE     = re.compile(r"^(DNS|IP):.+")         # a valid SubjectAltName entry is TYPED
POSTURES         = ("personal", "server")


# ---------------------------------------------------------------------------
# Output helpers (same vocabulary as both retiring drivers)
# ---------------------------------------------------------------------------

def banner(text):
    line = "=" * (len(text) + 6)
    print(f"\n{line}\n=== {text} ===\n{line}")

def stage(n, total, text): print(f"\n[{n}/{total}] {text}")
def info(m): print(f"  {m}")
def ok(m):   print(f"  [OK]   {m}")
def warn(m): print(f"  [WARN] {m}")
def err(m):  print(f"  [ERROR] {m}", file=sys.stderr)

def die(m, code=1):
    err(m)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

def run(cmd, check=True):
    return subprocess.run(cmd, check=check, text=True)

def run_out(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


# ---------------------------------------------------------------------------
# Shared: naming, validation, install-root resolution
# ---------------------------------------------------------------------------

def validate_brain_name(name):
    if not BRAIN_NAME_RE.match(name):
        die(f'invalid brain name "{name}" — must match ^[a-z][a-z0-9_]{{1,19}}$ '
            "(lowercase start, then 1-19 lowercase letters/digits/underscores).")

def resolve_install_root(args):
    """The dir that holds brains/<brain>/. EXPLICIT OR NOTHING — never guessed.
    Precedence: --install-root → $AIOS_INSTALL_ROOT → $HORIZON_ROOT → die.
    (Parity with both retiring drivers: a live brain is an OS account + a multi-GB
    runtime; guessing a destructive destination is never better than asking.)"""
    if getattr(args, "install_root", None):
        return Path(os.path.abspath(args.install_root))
    env_root = os.environ.get(INSTALL_ROOT_ENV)
    if env_root:
        if not os.path.isdir(env_root):
            die(f"${INSTALL_ROOT_ENV} is set to {env_root!r}, which is not a directory.")
        return Path(os.path.abspath(env_root))
    horizon_root = os.environ.get("HORIZON_ROOT")
    if horizon_root and os.path.isdir(horizon_root):
        return Path(os.path.abspath(horizon_root))
    die(f"no install root: pass --install-root <dir> or set ${INSTALL_ROOT_ENV} "
        "(the dir that holds brains/<brain>/).")


class Ctx:
    """Everything a stage or backend needs about one deploy, resolved once."""
    def __init__(self, args):
        self.args    = args
        self.brain   = args.brain
        self.posture = getattr(args, "posture", "personal")
        self.port    = getattr(args, "port", 8443)
        self.sans    = list(getattr(args, "san", None) or [])   # extra typed SAN entries (server)
        self.root    = resolve_install_root(args)
        self.brain_dir = self.root / "brains" / self.brain


# ===========================================================================
# Section 1 — the platform seam
# ===========================================================================

class PlatformBackend(abc.ABC):
    """The ONLY code allowed to branch on OS. Everything not defined here is shared.

    Method groups: privilege, account, identity switch, engine host (build), engine
    snapshot/restore, seam, residency, firewall. The shared orchestrator drives a deploy
    entirely through these; a stage that reaches for `sys.platform` directly is a bug."""

    label = "abstract"

    # -- privilege + account -------------------------------------------------
    @abc.abstractmethod
    def require_privilege(self): ...
    @abc.abstractmethod
    def account_exists(self, ctx) -> bool: ...
    @abc.abstractmethod
    def create_account(self, ctx): ...           # [Section 4]

    # -- identity switch (run AS the deployed brain) -------------------------
    @abc.abstractmethod
    def brain_exec(self, ctx, script) -> tuple:  # -> (rc, out, err)
        """Run a shell string as the DEPLOYED brain, with its runtime env resolved."""

    # -- engine host (BUILD-time environment the provision scripts run in) ----
    @abc.abstractmethod
    def engine_host_create(self, ctx, base=None): ...   # [Section 2]
    @abc.abstractmethod
    def engine_host_destroy(self, ctx): ...             # [Section 2]
    @abc.abstractmethod
    def engine_exec(self, ctx, script) -> tuple:        # [Section 2]  -> (rc, out, err)
        """Run a shell string AS THE BRAIN inside the engine BUILD host (where the
        provision stage-scripts and the build-time cert bake run)."""

    # -- engine snapshot / restore (the portable artifact) -------------------
    @abc.abstractmethod
    def engine_snapshot(self, ctx, dest): ...    # [Sections 2/3]
    @abc.abstractmethod
    def engine_restore(self, ctx, src): ...      # [Sections 3/4]

    # -- deploy-time OS-forced steps -----------------------------------------
    @abc.abstractmethod
    def seam_install(self, ctx): ...             # [Section 4]
    @abc.abstractmethod
    def residency_install(self, ctx): ...        # [Section 4]
    @abc.abstractmethod
    def residency_start(self, ctx): ...          # [Section 4]
    @abc.abstractmethod
    def firewall_open(self, ctx): ...            # [Section 4]
    @abc.abstractmethod
    def firewall_close(self, ctx): ...           # [Section 4]

    # -- naming (OS-forced; used by shared status/verify) --------------------
    @abc.abstractmethod
    def residency_status(self, ctx) -> str: ...  # human string for `status`


def _pending(section):
    raise NotImplementedError(f"not built yet — {section} (see docs/project_plans/"
                              "001_detail-unify_deploy_brain.md)")


class LinuxBackend(PlatformBackend):
    label = "native rootless Docker + systemd --user"

    def require_privilege(self):
        if os.geteuid() != 0:
            die("deploy_brain must run as root on Linux.  Re-run with sudo.")
        ok("running as root")

    def account_exists(self, ctx) -> bool:
        rc, _, _ = run_out(["getent", "passwd", ctx.brain])
        return rc == 0

    def create_account(self, ctx): _pending("Section 4 (create_account)")

    def brain_exec(self, ctx, script):
        # sudo -u <brain> -H bash -lc <script>: -H sets HOME, `bash -lc` loads the
        # brain's profile so DOCKER_HOST / XDG_RUNTIME_DIR resolve.
        return run_out(["sudo", "-u", ctx.brain, "-H", "bash", "-lc", script])

    # On native Linux the "engine build host" is the brain's own rootless context —
    # there is no separate VM, so engine_exec == brain_exec once the account exists.
    # The BUILD orchestration (create a clean build context, run the stage scripts)
    # is Section 2; the exec primitive itself is ready.
    def engine_host_create(self, ctx, base=None): _pending("Section 2 (Linux engine host)")
    def engine_host_destroy(self, ctx):           _pending("Section 2 (Linux engine host)")
    def engine_exec(self, ctx, script):
        return run_out(["sudo", "-u", ctx.brain, "-H", "bash", "-lc", script])

    def engine_snapshot(self, ctx, dest): _pending("Section 3 (docker save + volume + config bundle)")
    def engine_restore(self, ctx, src):   _pending("Section 3/4 (docker load + restore)")
    def seam_install(self, ctx):          _pending("Section 4 (bind-mount seam)")
    def residency_install(self, ctx):     _pending("Section 4 (systemd --user unit + linger)")
    def residency_start(self, ctx):       _pending("Section 4")
    def firewall_open(self, ctx):         _pending("Section 4 (ufw)")
    def firewall_close(self, ctx):        _pending("Section 4 (ufw)")

    def residency_status(self, ctx) -> str:
        unit = f"{ctx.brain}-docker-stack.service"
        rc, out, _ = self.brain_exec(ctx, f"systemctl --user is-enabled {unit} 2>/dev/null")
        return f"{unit}: {(out.strip() or 'absent')}"


class WindowsBackend(PlatformBackend):
    label = "WSL2 distro + Task Scheduler (STATIC — first-live on a real host)"

    def _run_as_brain(self):
        return Path(  # staged in the brain dir; the Windows identity-switch primitive
            "system") / "brain_sbin" / "run_as_brain.py"

    def require_privilege(self):
        # Admin check is Windows-specific; the real probe lands with the Windows path.
        _pending("Section 4 (Windows require_admin)")

    def account_exists(self, ctx) -> bool:
        rc, _, _ = run_out(["net", "user", ctx.brain])
        return rc == 0

    def create_account(self, ctx): _pending("Section 4 (create_account)")

    def brain_exec(self, ctx, script):
        rab = ctx.brain_dir / self._run_as_brain()
        if not rab.is_file():
            return 127, "", f"run_as_brain.py not staged at {rab}"
        return run_out([sys.executable, str(rab), "--brain", ctx.brain,
                        "--wsl", "--", "bash", "-lc", script])

    def engine_host_create(self, ctx, base=None): _pending("Section 2 (WSL build distro)")
    def engine_host_destroy(self, ctx):           _pending("Section 2 (WSL build distro)")
    def engine_exec(self, ctx, script):
        # Build-time: run inside the throwaway build distro brain-build-<brain>. Wired in Section 2.
        _pending("Section 2 (engine_exec in build distro)")

    def engine_snapshot(self, ctx, dest): _pending("Section 2 (wsl --export)")
    def engine_restore(self, ctx, src):   _pending("Section 4 (wsl --import)")
    def seam_install(self, ctx):          _pending("Section 4 (drvfs seam + icacls)")
    def residency_install(self, ctx):     _pending("Section 4 (schtasks keepalive)")
    def residency_start(self, ctx):       _pending("Section 4")
    def firewall_open(self, ctx):         _pending("Section 4 (Defender rule)")
    def firewall_close(self, ctx):        _pending("Section 4 (Defender rule)")

    def residency_status(self, ctx) -> str:
        rc, out, _ = run_out(["schtasks", "/query", "/tn", f"{ctx.brain}-docker-keepalive"])
        return f"{ctx.brain}-docker-keepalive: {'present' if rc == 0 else 'absent'}"


def backend_for_host():
    if sys.platform.startswith("linux"):
        return LinuxBackend()
    if sys.platform in ("win32", "cygwin") or sys.platform.startswith("win"):
        return WindowsBackend()
    if sys.platform == "darwin":
        die("macOS is not a brain runtime target yet (deploy is objective 009). "
            "Run deploy_brain inside a Linux VM if hosting a brain on a Mac.")
    die(f"unsupported platform {sys.platform!r} — deploy_brain targets Linux or Windows.")


# ===========================================================================
# Section 6 — the shared TLS-cert stage (BUG-001-1: no false-greens)
# ===========================================================================
#
# The bug this whole project exists for: the Linux driver passed the posture WORD
# ("personal"/"server") to gen-cert.sh, which reads positional args as SubjectAltName
# entries → openssl got a bogus SAN → set -euo pipefail aborted before writing cert.pem
# → and the caller reported success without checking the return code (a false-green).
# The gateway nginx then crash-looped on the missing cert.
#
# The contract (from gen-cert.sh:6-10,26-30):
#   personal → NO args              (SAN = DNS:localhost,IP:127.0.0.1)
#   server   → TYPED SAN entries    (e.g. DNS:brainhost.lan IP:192.168.1.20)
# The posture word is NEVER an argument. And a cert failure is FATAL, never a warning.

def gen_cert_argv(posture, sans=None):
    """Pure: build the argv for gen-cert.sh from posture + optional typed SAN entries.
    This is the exact logic the bug got wrong; kept pure so `selftest` can prove it."""
    if posture not in POSTURES:
        die(f"unknown posture {posture!r} (expected one of {POSTURES}).")
    entries = list(sans or [])
    # A SAN entry MUST be typed (DNS:/IP:). The posture word is not one — reject it loudly
    # rather than silently forwarding it (belt-and-braces against the original bug class).
    for e in entries:
        if not SAN_ENTRY_RE.match(e):
            die(f"invalid SAN entry {e!r} — must be typed, e.g. 'DNS:host.lan' or "
                "'IP:192.168.1.20'. (Never pass the posture word as a SAN.)")
    if posture == "personal":
        # personal is loopback-only; extra SANs are unusual but allowed if explicitly typed.
        return entries
    # server: entries carry the host's LAN name/IP so off-box clients validate.
    return entries


def cert_stage(backend, ctx, *, exec_fn=None):
    """SHARED build-time cert bake. Runs gen-cert.sh AS THE BRAIN in the engine build
    host with the CORRECT argv, then hard-checks the result. Fatal on failure — this is
    where the no-false-green invariant is enforced.

    exec_fn override lets a test drive it without a live engine; defaults to the
    backend's engine_exec (Section 2)."""
    argv = gen_cert_argv(ctx.posture, ctx.sans)
    if ctx.posture == "server" and not argv:
        warn("server posture with no --san entries: cert will be loopback-only, so "
             "off-box clients cannot validate it. Pass --san DNS:<host> --san IP:<addr>.")
    gencert = "system/brain_bin/gateway/gen-cert.sh"
    quoted  = " ".join(_shq(a) for a in argv)
    script  = f"cd ~ && bash {gencert} {quoted}".rstrip()
    runner  = exec_fn or (lambda s: backend.engine_exec(ctx, s))

    info(f"baking gateway TLS cert (posture={ctx.posture}; "
         f"SAN extras={argv or 'none — loopback only'})")
    rc, out, e = runner(script)
    if rc != 0:                      # <-- THE CHECK THE ORIGINAL BUG OMITTED
        die(f"TLS cert generation FAILED (rc={rc}) — gateway would crash-loop on a "
            f"missing cert. Aborting.\n{out}{e}")
    # Belt-and-braces: confirm the file actually exists (a zero rc is necessary, not
    # sufficient — the original failure also left no file behind).
    rc2, _, _ = runner("test -f ~/gateway/gateway_out/cert.pem")
    if rc2 != 0:
        die("gen-cert.sh returned 0 but ~/gateway/gateway_out/cert.pem is absent — "
            "refusing to report a false success.")
    ok("gateway TLS cert baked (~/gateway/gateway_out/cert.pem) — verified present")


def _shq(s):
    """Minimal single-quote shell-quote (SAN entries are DNS:/IP: tokens; still quote)."""
    return "'" + str(s).replace("'", "'\\''") + "'"


# ===========================================================================
# Verbs — only what Sections 1 & 6 support runs; the rest report their section
# ===========================================================================

def cmd_status(args):
    validate_brain_name(args.brain)
    be = backend_for_host()
    ctx = Ctx(args)
    banner(f"Status: {ctx.brain}  [{be.label}]")
    print(f"  account exists   : {be.account_exists(ctx)}")
    print(f"  brain folder     : {ctx.brain_dir}  "
          f"({'present' if ctx.brain_dir.is_dir() else 'MISSING'})")
    print(f"  residency        : {be.residency_status(ctx)}")
    print("\n  (full status lands with Section 4; use brain_doctor.py for a live runtime report.)")

def cmd_selftest(args):
    """Unit-check the cert-arg contract — proves BUG-001-1 is closed without a live host."""
    banner("deploy_brain selftest — cert-arg contract (Section 6)")
    cases = [
        ("personal", None,                      []),
        ("personal", [],                        []),
        ("server",   ["DNS:host.lan", "IP:10.0.0.5"], ["DNS:host.lan", "IP:10.0.0.5"]),
    ]
    for posture, sans, expect in cases:
        got = gen_cert_argv(posture, sans)
        assert got == expect, f"gen_cert_argv({posture!r},{sans!r}) = {got!r}, expected {expect!r}"
        ok(f"gen_cert_argv({posture}, {sans}) -> {got}")
    # The regression itself: the posture word must NEVER be accepted as a SAN.
    for bad in ("personal", "server", "localhost"):
        rc, _, _ = run_out([sys.executable, __file__, "_reject_probe", bad])
        assert rc != 0, f"gen_cert_argv should REJECT bare SAN {bad!r} but accepted it"
        ok(f"rejects bare SAN {bad!r} (the original bug's input) — fatal, as intended")
    print("\n  SELFTEST PASSED — the posture word can never reach gen-cert.sh as a SAN.")

def _cmd_reject_probe(args):
    # Internal: build argv with a bare (untyped) SAN so selftest can assert it dies.
    gen_cert_argv("server", [args.token])   # dies if untyped → nonzero exit
    sys.exit(0)

def _unimplemented(section):
    def _run(args):
        die(f"'{args.cmd}' is not built yet — {section}. "
            "See docs/project_plans/001_detail-unify_deploy_brain.md for the plan.")
    return _run


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args():
    ap = argparse.ArgumentParser(
        description="The one brain deploy orchestrator (Linux / Windows).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for verb, section in (("build-engine", "Section 2"), ("deploy", "Section 4"),
                          ("teardown", "Section 4"), ("verify", "Section 4")):
        p = sub.add_parser(verb, help=f"[pending — {section}]")
        p.add_argument("--brain", required=True)
        p.add_argument("--posture", choices=POSTURES, default="personal")
        p.add_argument("--port", type=int, default=8443)
        p.add_argument("--san", action="append", help="typed SAN entry (server), e.g. DNS:host.lan")
        p.add_argument("--install-root", default=None)
        p.set_defaults(func=_unimplemented(section))

    s = sub.add_parser("status", help="what exists for a brain (account/folder/residency)")
    s.add_argument("--brain", required=True)
    s.add_argument("--install-root", default=None)
    s.set_defaults(func=cmd_status)

    st = sub.add_parser("selftest", help="unit-check the cert-arg contract (Section 6)")
    st.set_defaults(func=cmd_selftest, brain=None)

    rp = sub.add_parser("_reject_probe", help=argparse.SUPPRESS)
    rp.add_argument("token")
    rp.set_defaults(func=_cmd_reject_probe, brain=None)

    return ap.parse_args()


def main():
    args = parse_args()
    if getattr(args, "brain", None):
        validate_brain_name(args.brain)
    args.func(args)


if __name__ == "__main__":
    main()
