#!/usr/bin/env python3
"""
linux_deploy_brain.py — The Brain Deploy Orchestrator (native Linux)
===================================================================

The native-Linux sibling of `windows_deploy_brain.py`. Same job — one entry point
that sequences every brain building block into a converging deploy and its inverse —
but for a real Linux host, with **no WSL and no VM**: the brain runs as a native OS
user with rootless Docker in its home, systemd for residency, and a bind mount for
the config seam.

STATUS
    ⚠ NOT YET RUN LIVE. This is the first Linux implementation; it is static-validated
    (`py_compile`) only. It is exercised + hardened by `developer/test_plan_linux_host.md`
    (phases L0–L8). Treat every stage as first-live until that plan goes green. Where a
    portable building block already had a Linux code path (notably `run_as_brain.py`,
    "implemented; NOT exercised"), this driver is its first real caller.

WINDOWS ↔ LINUX MAPPING (why this is a separate file, not a branch)
    ┌─────────────────┬──────────────────────────────┬──────────────────────────────┐
    │ concern         │ Windows (windows_deploy_brain)│ Linux (this file)            │
    ├─────────────────┼──────────────────────────────┼──────────────────────────────┤
    │ elevation       │ Administrator (IsUserAnAdmin) │ root / sudo (geteuid == 0)   │
    │ identity switch │ run_as_brain → Start-Process  │ sudo -u <brain> -H           │
    │ engine          │ WSL2 distro (wsl --import of a │ NONE — the brain runs native;│
    │                 │ provisioned, exported tar)    │ "engine" = rootless Docker + │
    │                 │                               │ the stack in the brain home  │
    │ account         │ Get-LocalUser / create_brain  │ useradd -m + subuid/subgid   │
    │ residency       │ Task Scheduler boot task +    │ systemd --user unit + linger │
    │                 │ SeBatchLogonRight             │ (no idle-VM to hold open)    │
    │ seam mount      │ drvfs 9p (-o ro) + icacls     │ bind mount (ro) + POSIX perms│
    │ firewall        │ Windows Defender rule         │ ufw/firewalld (optional)     │
    └─────────────────┴──────────────────────────────┴──────────────────────────────┘
    The portable pieces are SHARED verbatim in spirit: the code package + staging, the
    gateway compose stack (compose.yaml / nginx.conf.template / gen-cert.sh), the
    bootstrap-token mint into brain_etc/gateway/*.map, and the verify curl gates
    (no-token 403 / reader 200 / reset 403). Only the OS-forced pieces above diverge.

WHY LINUX IS SIMPLER
    No idle-VM to hold resident, so residency is "enable linger + a user unit that runs
    `docker compose up -d` at boot" rather than a keepalive holding a WSL utility VM open.
    No drvfs 9p, so the seam is an ordinary read-only bind mount — the v0.8.0 drvfs
    deny-ACE read-wall bug simply does not exist here; read-only is enforced by ownership
    (root-owned tree, brain not in a writing group), not a deny ACE.

FOLLOW-ON (tracked in objectives/008 + 009)
    Two sub-tools are still Windows-shaped and are NOT called here; this driver does their
    Linux-native equivalent inline and flags the port as follow-on:
      - create_brain.py (standalone) → Get-LocalUser/Windows groups; Linux uses useradd here.
      - brain_truths.py               → drvfs + icacls; Linux uses a bind mount + POSIX perms here.
    macOS (Lima/Colima VM, `limactl`) is objective 009, deferred.

USAGE
    sudo python3 linux_deploy_brain.py deploy   --brain X --posture personal|server
                                                [--port N] [--bind personal|server]
                                                [--package <tar>] [--skip-gateway]
                                                [--skip-residency]
    sudo python3 linux_deploy_brain.py teardown --brain X [--purge --yes]
    sudo python3 linux_deploy_brain.py verify   --brain X [--port N]
    sudo python3 linux_deploy_brain.py status   --brain X
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

FACTORY_ROOT = Path(__file__).resolve().parent          # brains/brain_workshop/factory
DIST_DIR     = FACTORY_ROOT / "dist"
BRAIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,19}$")

# Where the read-only config seam is mounted inside the brain's view (parity with
# the Windows drvfs mount point, so apply_brain_truths.sh finds it at the same path).
MOUNT_POINT = "/opt/brain_truths"

# ---------------------------------------------------------------------------
# Output helpers (siblings to windows_deploy_brain.py — same vocabulary)
# ---------------------------------------------------------------------------

def banner(text):
    line = "=" * (len(text) + 6)
    print(f"\n{line}\n=== {text} ===\n{line}")

def stage(n, total, text): print(f"\n[{n}/{total}] {text}")
def info(m):  print(f"  {m}")
def ok(m):    print(f"  [OK]   {m}")
def warn(m):  print(f"  [WARN] {m}")
def err(m):   print(f"  [ERROR] {m}", file=sys.stderr)

def die(m, code=1):
    err(m)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

def run(cmd, check=True, capture=False, env=None):
    display = " ".join(str(a) for a in cmd)
    info(f"run: {display}")
    return subprocess.run(cmd, check=check, capture_output=capture, text=True, env=env)

def run_out(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout or ""), (p.stderr or "")

def _http_code(out):
    toks = [t for t in (out or "").split() if t.isdigit()]
    return toks[-1] if toks else ""


# ---------------------------------------------------------------------------
# Identity — run as the brain (sudo -u), the Linux realization of run_as_brain
# ---------------------------------------------------------------------------
# The deployer already runs as root (sudo), so `sudo -u <brain>` needs no password —
# which is exactly why the Linux path is simpler than Windows (no stored credential,
# no keyring). For its OWN in-brain calls the driver uses sudo directly (deterministic);
# `system/brain_sbin/run_as_brain.py` is the OPERATOR-facing equivalent (same `sudo -u` under
# the hood) and is exercised as a first-class citizen by the L-test-plan (phase L5).

def as_brain(brain, argv):
    """Prefix argv so it runs as the brain user with its login environment (-H sets HOME,
    `bash -lc` in the caller loads DOCKER_HOST / XDG_RUNTIME_DIR from profile)."""
    return ["sudo", "-u", brain, "-H"] + list(argv)

def brain_sh(brain, script):
    """Run a shell string as the brain in a login shell (so rootless-Docker env resolves)."""
    return run_out(as_brain(brain, ["bash", "-lc", script]))

def brain_home(brain):
    rc, out, _ = run_out(["getent", "passwd", brain])
    if rc != 0 or not out.strip():
        return None
    return out.strip().split(":")[5] or f"/home/{brain}"


# ---------------------------------------------------------------------------
# Environment / identity checks
# ---------------------------------------------------------------------------

def require_root():
    if os.geteuid() != 0:
        die("linux_deploy_brain must run as root.\n"
            "    Re-run with sudo:  sudo python3 linux_deploy_brain.py ...")
    ok("running as root")

def validate_brain_name(name):
    if not BRAIN_NAME_RE.match(name):
        die(f'invalid brain name "{name}" — must match ^[a-z][a-z0-9_]{{1,19}}$ '
            "(lowercase start, then 1-19 lowercase letters/digits/underscores).")

def user_exists(brain):
    rc, _, _ = run_out(["getent", "passwd", brain])
    return rc == 0

def linger_enabled(brain):
    rc, out, _ = run_out(["loginctl", "show-user", brain, "--property=Linger"])
    return "Linger=yes" in (out or "")


# ---------------------------------------------------------------------------
# Naming (parity with the Windows canon, minus the WSL distro)
# ---------------------------------------------------------------------------

def stack_service(brain):   return f"{brain}-docker-stack"        # systemd --user unit (residency)
def seam_mount_unit():      return "opt-brain_truths.mount"       # system .mount unit


def resolve_install_root(args):
    if getattr(args, "install_root", None):
        return Path(os.path.abspath(args.install_root))
    env_root = os.environ.get("AIOS_INSTALL_ROOT")
    if env_root and os.path.isdir(env_root):
        return Path(os.path.abspath(env_root))
    # On a Horizon.AIOS install $HORIZON_ROOT IS the install root (the folder that holds brains/).
    horizon_root = os.environ.get("HORIZON_ROOT")
    if horizon_root and os.path.isdir(horizon_root):
        return Path(os.path.abspath(horizon_root))
    d = FACTORY_ROOT
    for _ in range(6):
        if (d / "brains").is_dir():
            return d
        d = d.parent
    return FACTORY_ROOT.parent.parent.parent

def brain_paths(args):
    root = resolve_install_root(args)
    return root, root / "brains" / args.brain


# ---------------------------------------------------------------------------
# Stage: preflight
# ---------------------------------------------------------------------------

def preflight(args):
    require_root()

    # systemd (residency + the seam mount unit both need it).
    rc, _, _ = run_out(["systemctl", "--version"])
    if rc != 0:
        die("systemd not found — this deployer targets systemd Linux hosts "
            "(residency + the seam mount are systemd units).")
    ok("systemd present")

    # Docker engine present (rootless mode is set up per-brain in the provision stage,
    # but the docker binary + the rootless setup tool must exist system-wide).
    if not shutil.which("docker"):
        die("`docker` not found on PATH. Install Docker Engine (docker-ce) so rootless\n"
            "    mode can be set up for the brain user, then re-run. (The brain runs its\n"
            "    own rootless daemon; the system daemon is not used.)")
    ok("docker present")

    # Rootless prerequisites: newuidmap/newgidmap (uidmap pkg) + unprivileged userns.
    if not (shutil.which("newuidmap") and shutil.which("newgidmap")):
        die("newuidmap/newgidmap missing (install the `uidmap` package) — rootless Docker\n"
            "    needs them to set up the user namespace. Install, then re-run.")
    ok("uidmap tools present")

    for tool in ("curl", "openssl"):
        if not shutil.which(tool):
            die(f"`{tool}` not found — required (curl: verify gates; openssl: cert gen). Install it.")
    ok("curl + openssl present")

    ok("preflight passed")


# ---------------------------------------------------------------------------
# Stage: create brain (native — useradd + subuid/subgid + linger)
# ---------------------------------------------------------------------------
# NOTE: the factory create_brain.py (standalone) is currently Windows-only
# (Get-LocalUser / local groups / Credential Manager). Until it grows a Linux branch
# (objective 008 follow-on), the Linux user is provisioned natively here.

def create_brain(args):
    brain = args.brain
    if user_exists(brain):
        ok(f'account "{brain}" already exists — skipping create-brain')
    else:
        info(f"creating system user {brain} (home + bash login shell)")
        run(["useradd", "--create-home", "--shell", "/bin/bash", brain])
        if not user_exists(brain):
            die("useradd ran but the account still does not exist — see output above.")
        ok(f'account "{brain}" provisioned')

    # subuid/subgid ranges (rootless Docker user namespaces). useradd usually adds these
    # on modern distros; ensure they exist idempotently.
    for db in ("/etc/subuid", "/etc/subgid"):
        try:
            has = any(line.startswith(brain + ":") for line in Path(db).read_text().splitlines())
        except FileNotFoundError:
            has = False
        if not has:
            info(f"allocating a namespace range for {brain} in {db}")
            run(["usermod", "--add-subuids", "100000-165535", brain], check=False)
            run(["usermod", "--add-subgids", "100000-165535", brain], check=False)
            break  # both flags apply in one usermod on most distros; re-check is cheap

    # Linger: lets the brain's systemd --user manager (and its rootless dockerd) run with
    # no interactive login — the Linux analogue of "run whether logged on or not".
    if not linger_enabled(brain):
        run(["loginctl", "enable-linger", brain])
    ok(f"linger enabled for {brain} (user services run headless)")


# ---------------------------------------------------------------------------
# Stage: stage the code package (portable — identical to the Windows path)
# ---------------------------------------------------------------------------

def _package_sort_key(p):
    m = re.search(r"brain-factory-(\d+)\.(\d+)\.(\d+)-(\d{4}-\d{2}-\d{2})", p.name)
    if not m:
        return ((-1, -1, -1), "", p.name)
    return ((int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(4), p.name)

def _newest_package():
    cands = sorted(DIST_DIR.glob("brain-factory-*.tar.gz"), key=_package_sort_key)
    return cands[-1] if cands else None

def _verify_sha256(tar_path):
    sha_file = tar_path.with_suffix(tar_path.suffix + ".sha256")
    if not sha_file.is_file():
        warn(f"no .sha256 next to {tar_path.name} — skipping integrity check")
        return
    expected = sha_file.read_text(encoding="utf-8").split()[0].strip().lower()
    h = hashlib.sha256()
    with open(tar_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    actual = h.hexdigest().lower()
    if actual != expected:
        die(f"package integrity FAILED for {tar_path.name}\n"
            f"    expected {expected}\n    actual   {actual}")
    ok(f"package integrity verified ({tar_path.name})")

# Runtime state staging must never clobber. wsl is a Windows artifact (absent
# on Linux); knowledge/ (data door target) and the provision marker are protected.
_STAGE_PROTECT = ("knowledge",)

def stage_package(args):
    _, brain_dir = brain_paths(args)
    tar_path = Path(args.package) if getattr(args, "package", None) else _newest_package()
    if not tar_path or not tar_path.is_file():
        die("no code package found. Build one (build_package.sh) or pass --package "
            f"<tar>. Looked in {DIST_DIR}.")
    info(f"package: {tar_path.name}")
    _verify_sha256(tar_path)
    brain_dir.mkdir(parents=True, exist_ok=True)

    def _safe(members):
        for m in members:
            target = (brain_dir / m.name).resolve()
            if not str(target).startswith(str(brain_dir.resolve())):
                die(f"refusing unsafe path in package: {m.name}")
            if any(m.name == p or m.name.startswith(p + "/") for p in _STAGE_PROTECT):
                if (brain_dir / m.name).exists():
                    continue
            yield m

    with tarfile.open(tar_path, "r:gz") as tf:
        tf.extractall(brain_dir, members=list(_safe(tf.getmembers())))
    ok(f"code staged into {brain_dir}")
    # Materialize the brain-root context/policy files from the staged policy_templates/
    # collection (the tarball bundles all four — see build_package.sh) before any lock/provision.
    seed_brain_context_files(brain_dir, args.brain)


# Brain-root context/policy files seeded from the staged policy_templates/ collection (the
# tarball bundles the factory's rich brain_invariants.md plus the three shared context
# templates). (template filename in policy_templates/, deployed filename at brain root).
_CONTEXT_TEMPLATES = (
    ("brain_invariants.md",      "brain_invariants.md"),   # factory-local rich policy
    ("brain_CLAUDE.md.template", "CLAUDE.md"),             # bundled at build
    ("brain_agents.md.template", "agents.md"),
    ("brain_core.md.template",   "brain_core.md"),
)


def seed_brain_context_files(brain_dir, brain):
    """Seed the four brain-root context/policy files (brain_invariants.md, CLAUDE.md, agents.md,
    brain_core.md) from the staged <brain>/policy_templates/ collection, substituting
    [BRAIN_NAME] + [AIOS_INSTALL_ROOT_PATH]. ONLY-IF-ABSENT (idempotent; never clobbers a live
    brain's tuned policy on redeploy). Mirrors the Windows deployer — without it a deployed
    brain ships with NO policy/context. See brain_security_model.md §8."""
    src_dir = brain_dir / "policy_templates"
    if not src_dir.is_dir():
        warn(f"policy_templates/ not staged ({src_dir}) — the deployed brain will have NO "
             "brain-root context/policy files.")
        return
    subs = {"[BRAIN_NAME]": brain, "[AIOS_INSTALL_ROOT_PATH]": "$AIOS_INSTALL_ROOT"}
    seeded = 0
    for tmpl, dest in _CONTEXT_TEMPLATES:
        src = src_dir / tmpl
        dst = brain_dir / dest
        if not src.is_file():
            warn(f"context template missing ({src}) — {dest} will not be seeded")
            continue
        if dst.exists():
            continue  # never clobber a live/tuned context file on redeploy
        try:
            text = src.read_text(encoding="utf-8")
            for k, v in subs.items():
                text = text.replace(k, v)
            dst.write_bytes(text.encode("utf-8"))
            seeded += 1
        except OSError as e:
            warn(f"could not seed {dest} ({e})")
    ok(f"brain-root context/policy seeded ({seeded} file(s); existing kept) — "
       "brain_invariants.md from the canonical 7-invariant policy")
    # policy_templates/ is a TEMPLATE SOURCE, not deployed content — remove it once seeded.
    try:
        shutil.rmtree(src_dir, ignore_errors=True)
    except Exception as e:
        warn(f"could not remove staged policy_templates/ ({e}) — harmless")


# ---------------------------------------------------------------------------
# Stage: provision the brain runtime (rootless Docker + the stack in the home)
# ---------------------------------------------------------------------------
# The Linux equivalent of "import the engine": there is no VM to import, so we set up
# rootless Docker for the brain and lay the gateway stack in its home. The provision/
# stage scripts (stage4_brain.sh etc.) are the RECIPE reference; on a native host the OS
# already exists, so we do the native-relevant subset here rather than run the WSL-shaped
# stage scripts (which assume a fresh distro + wsl.conf).

def _docker_ready(brain):
    rc, _, _ = brain_sh(brain, "docker info >/dev/null 2>&1 && echo OK")
    return rc == 0

def provision_runtime(args):
    brain = args.brain
    home = brain_home(brain)
    if not home:
        die(f"cannot resolve home for {brain}")
    _, brain_dir = brain_paths(args)

    # 1. Rootless Docker for the brain (idempotent — skip if its daemon already answers).
    if _docker_ready(brain):
        ok("rootless Docker already running as the brain")
    else:
        setup = shutil.which("dockerd-rootless-setuptool.sh") or "dockerd-rootless-setuptool.sh"
        info("installing rootless Docker for the brain (dockerd-rootless-setuptool.sh install)")
        rc, out, e = brain_sh(brain,
            f"export XDG_RUNTIME_DIR=/run/user/$(id -u); {setup} install")
        if rc != 0:
            die("rootless Docker setup FAILED for the brain. Common causes: kernel unprivileged\n"
                "    userns disabled, or /run/user/<uid> not present (linger must be on — it is set\n"
                f"    in create-brain). Output:\n{out}{e}")
        # Enable + start the rootless daemon as a user service, holding it across boots.
        brain_sh(brain, "systemctl --user enable --now docker")
        if not _docker_ready(brain):
            die("rootless Docker installed but its daemon is not answering — check "
                "`systemctl --user status docker` as the brain.")
        ok("rootless Docker installed + running as the brain")

    # 2. Seed DOCKER_HOST for non-interactive login shells (so `docker` resolves under
    #    `bash -lc`), mirroring the Windows /etc/profile.d seam. Idempotent write.
    uid_rc, uid_out, _ = brain_sh(brain, "id -u")
    uid = (uid_out or "").strip()
    profile = Path(home) / ".bashrc"
    marker = "# brain rootless docker env"
    try:
        body = profile.read_text() if profile.is_file() else ""
    except Exception:
        body = ""
    if marker not in body:
        line = (f"\n{marker}\nexport XDG_RUNTIME_DIR=/run/user/{uid}\n"
                f"export DOCKER_HOST=unix:///run/user/{uid}/docker.sock\n")
        # Write as the brain so ownership stays correct.
        brain_sh(brain, f"printf '%s' {shell_quote(line)} >> ~/.bashrc")
        ok("DOCKER_HOST seeded into the brain's login environment")

    # 3. Lay the gateway stack into ~/docker from the staged canon + gen certs.
    stack_dir = f"{home}/docker"
    canon_gateway = brain_dir / "system" / "brain_bin" / "gateway"
    if not (canon_gateway / "gen-cert.sh").is_file():
        die(f"staged gateway canon missing at {canon_gateway} — did stage_package run?")
    # compose.yaml has ONE source: the ADR-0015 seam template (brain_etc.example/docker/, or the
    # seeded brain_etc/docker/ once it exists). gateway/ used to carry a second copy and the two
    # drifted — gateway/ kept a 79-line pre-ADR-0013 prototype (base `ports:`, no ollama) while
    # the template moved to the 403-line overlay model. Mirrors stage4_brain.sh; keep them agreed.
    compose_src = next((p for p in (brain_dir / "brain_etc" / "docker" / "compose.yaml",
                                    brain_dir / "brain_etc.example" / "docker" / "compose.yaml")
                        if p.is_file()), None)
    if compose_src is None:
        die(f"compose template missing — looked for brain_etc/docker/compose.yaml and "
            f"brain_etc.example/docker/compose.yaml under {brain_dir}. Did stage_package run?")
    info(f"laying the gateway stack into {stack_dir} (compose from {compose_src.parent})")
    # Copy as the brain so the tree is brain-owned (rootless Docker reads it as the brain).
    # Data-in seam zones: brain_rw/chroma holds the vector store (pre-created brain-owned so
    # docker doesn't root-create the bind target); brain_ro holds read-only source content
    # (its no-write posture is tightened to root-owned by the harden stage). See knowledge/README.md.
    brain_sh(brain, f"mkdir -p ~/docker ~/gateway/gateway_out ~/knowledge/brain_rw/chroma ~/knowledge/brain_ro")
    for rel in ("nginx", ".env.example"):
        src = canon_gateway / rel
        if src.exists():
            # cp -r via the brain to preserve ownership; -n never clobbers an admin-edited file.
            brain_sh(brain, f"cp -rn {shell_quote(str(src))} ~/docker/ 2>/dev/null || true")
    brain_sh(brain, f"cp -n {shell_quote(str(compose_src))} ~/docker/ 2>/dev/null || true")
    # Materialize .env from the example if absent, with a generated CHROMA_MASTER_TOKEN_FOR_GW.
    brain_sh(brain,
        "test -f ~/docker/.env || { "
        "cp ~/docker/.env.example ~/docker/.env 2>/dev/null || : ; "
        "grep -q '^CHROMA_MASTER_TOKEN_FOR_GW=' ~/docker/.env 2>/dev/null || "
        "echo CHROMA_MASTER_TOKEN_FOR_GW=$(openssl rand -hex 32) >> ~/docker/.env ; }")
    # Generate the TLS cert at the gateway home (posture-aware).
    posture = args.posture
    gencert = "system/brain_bin/gateway/gen-cert.sh"
    if (canon_gateway / "gen-cert.sh").is_file():
        brain_sh(brain, f"cd {shell_quote(str(brain_dir))} && bash {gencert} {posture} || "
                        f"bash {shell_quote(str(canon_gateway / 'gen-cert.sh'))} {posture}")
    ok("gateway stack laid + TLS cert generated (~/gateway/gateway_out/cert.pem)")

    # 4. Bring the stack up (rootless compose as the brain).
    rc, out, e = brain_sh(brain, "cd ~/docker && docker compose up -d")
    if rc != 0:
        die(f"docker compose up FAILED:\n{out}{e}")
    ok("Chroma + gateway stack up (rootless, as the brain)")


def shell_quote(s):
    """Minimal POSIX single-quote for embedding a literal in a bash -lc string."""
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


# ---------------------------------------------------------------------------
# Stage: config-exposure seam (bind mount RO + POSIX perms — the Linux realization)
# ---------------------------------------------------------------------------
# Windows uses a drvfs 9p mount + icacls deny-ACE gymnastics (and the v0.8.0 fix that
# a deny-ACE breaks drvfs reads). On Linux none of that applies: the seam is an ordinary
# read-only BIND mount, and read-only-to-the-brain is enforced by OWNERSHIP — the host
# brain_etc/ tree is root-owned, the brain is not in a writing group, so it can read
# (world-r) but not write. No deny ACE, so the v0.8.0 read-wall class of bug can't occur.

def seam(args):
    brain = args.brain
    _, brain_dir = brain_paths(args)
    home = brain_home(brain)
    etc = brain_dir / "brain_etc"

    # 1. Seed brain_etc/ (host source of truth) from the packaged ADR-0015 TEMPLATE
    #    brain_etc.example/, substituting __BRAIN_NAME__ -> the real brain name. ONLY-IF-ABSENT
    #    per file: idempotent, never clobbers a live/tuned knob or a minted token on a redeploy.
    #    This gives the seam the full path-router config (brain.env + gateway.conf +
    #    token_registry + neuron/{bundles,sources}.yaml + docker/compose*.yaml + chroma/ollama
    #    env + tls), not just the single authz template. Derived config (nginx_auto_gen/,
    #    token maps) is regenerated from these knobs by gateway_config in the gateway stage.
    etc.mkdir(parents=True, exist_ok=True)
    example = brain_dir / "brain_etc.example"
    seeded = 0
    if example.is_dir():
        for src in sorted(example.rglob("*")):
            rel = src.relative_to(example)
            dst = etc / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True); continue
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                dst.write_bytes(src.read_text(encoding="utf-8")
                                .replace("__BRAIN_NAME__", brain).encode("utf-8"))
            except UnicodeDecodeError:
                dst.write_bytes(src.read_bytes())
            seeded += 1
    else:
        warn(f"brain_etc.example not staged ({example}); seeding only the canon authz template")
        (etc / "gateway").mkdir(parents=True, exist_ok=True)
        canon_tmpl = brain_dir / "system" / "brain_bin" / "gateway" / "nginx" / "nginx.conf.template"
        if canon_tmpl.is_file():
            (etc / "gateway" / "nginx.conf.template").write_bytes(canon_tmpl.read_bytes())
    ok(f"brain_etc/ seeded from ADR-0015 template ({seeded} file(s); existing knobs kept)")

    # config-flow Phase 5 / WS3: brain_etc.example/ is a factory TEMPLATE SOURCE only — it must NOT
    # persist as a sibling of the rendered brain_etc/ in a DEPLOYED root. Now that brain_etc/ is
    # populated, remove the template copy (a fresh tarball re-extracts it each deploy).
    if example.is_dir() and etc.is_dir() and any(etc.iterdir()):
        shutil.rmtree(example, ignore_errors=True)
        ok("brain_etc.example/ removed from the deployed root (template source stays in the factory)")

    # 2. POSIX perms: root owns the tree; brain gets read+execute-traverse, never write.
    run(["chown", "-R", "root:root", str(etc)])
    run(["chmod", "-R", "u=rwX,go=rX", str(etc)])   # world-readable, only root writes
    ok(f"brain_etc/ posture applied (root:root, {brain} read-only via POSIX perms — no deny ACE)")

    # 3. Read-only bind mount at /opt/brain_truths via a systemd .mount unit (survives reboot).
    Path(MOUNT_POINT).mkdir(parents=True, exist_ok=True)
    unit_path = Path("/etc/systemd/system") / seam_mount_unit()
    unit = (
        "[Unit]\n"
        "Description=Brain config-exposure seam (read-only bind mount of brain_etc)\n"
        "# no After=local-fs.target: mount units are implicitly Before=local-fs.target\n"
        "# (DefaultDependencies); an explicit After= creates an ordering cycle -> flapping.\n\n"
        "[Mount]\n"
        f"What={etc}\n"
        f"Where={MOUNT_POINT}\n"
        "Type=none\n"
        "Options=bind,ro\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    unit_path.write_text(unit)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", seam_mount_unit()])
    # Confirm RO.
    rc, out, _ = run_out(["findmnt", "-no", "OPTIONS", MOUNT_POINT])
    if "ro" not in (out or ""):
        warn(f"seam mount at {MOUNT_POINT} is not read-only ({out.strip()}); check the unit")
    else:
        ok(f"seam bind-mounted read-only at {MOUNT_POINT}")

    # 4. Install the apply primitive path expectation: apply_brain_truths.sh (portable bash)
    #    reads from the mount and syncs into the runtime working copy. It rides in at the
    #    mount, exactly like Windows. The gateway stage triggers the first apply.
    ok("brain-truths seam ready: host brain_etc/ exposed read-only at /opt/brain_truths")


# ---------------------------------------------------------------------------
# Bootstrap-token mint (portable — identical format to gateway_token.py / Windows)
# ---------------------------------------------------------------------------

# Tokens live in the unified brain_etc/gateway/token_registry; the nginx
# *_tokens.map / ollama_*.map files are GENERATED from it by gateway_tokens.py.

def _load_gateway_tokens(brain_dir):
    """Load the staged gateway_tokens.py (registry model + generator) as a library."""
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
    gt = _load_gateway_tokens(brain_dir)
    grant = _bootstrap_grant(role)
    for e in gt.read_registry(brain_dir):
        if grant in e.grants:
            return e.token
    return None

def _ensure_bootstrap_token(brain_dir, role, label="bootstrap"):
    import secrets
    from datetime import datetime, timezone
    gt = _load_gateway_tokens(brain_dir)
    entries = gt.read_registry(brain_dir)
    grant = _bootstrap_grant(role)
    for e in entries:
        if grant in e.grants:
            gt.generate(entries, gt.gateway_dir(brain_dir))
            return e.token
    token = secrets.token_hex(32)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries.append(gt.Entry(token, [grant], f"{label}-{role}", created))
    gt.write_registry(entries, brain_dir)
    gt.generate(entries, gt.gateway_dir(brain_dir))
    return token


# ---------------------------------------------------------------------------
# Stage: gateway (port/bind in .env, mint tokens, apply seam + recreate gateway)
# ---------------------------------------------------------------------------

def gateway(args):
    brain = args.brain
    _, brain_dir = brain_paths(args)
    home = brain_home(brain)
    port = args.port
    bind_choice = args.bind or args.posture
    bind = {"personal": "127.0.0.1", "server": "0.0.0.0"}.get(bind_choice, bind_choice)

    # 1. Port + bind into the stack .env (rootless: no privileged-port bind, no Windows firewall).
    info(f"setting gateway port {port} (bind={bind}) in ~/docker/.env")
    brain_sh(brain,
        "cd ~/docker && "
        f"( grep -q '^GATEWAY_PORT=' .env && sed -i 's/^GATEWAY_PORT=.*/GATEWAY_PORT={port}/' .env "
        f"|| echo GATEWAY_PORT={port} >> .env ) && "
        f"( grep -q '^GATEWAY_BIND=' .env && sed -i 's/^GATEWAY_BIND=.*/GATEWAY_BIND={bind}/' .env "
        f"|| echo GATEWAY_BIND={bind} >> .env )")
    ok(f"gateway port {port} set (bind {bind})")

    # 2. Mint the bootstrap reader+writer pair into the seam source (shown once), BEFORE the
    #    regen below so the generated maps carry them.
    reader_tok = _ensure_bootstrap_token(brain_dir, "reader")
    writer_tok = _ensure_bootstrap_token(brain_dir, "writer")

    # 2b. Auto-mint the NAMED neuron tokens the brain.env YAML zone references (config-flow Phase 3):
    #     each neuron's `gateway_token: <name>` must exist in the registry or gateway_config generate
    #     fails CLOSED. seed_neuron_tokens mints any missing name with its type-default grant
    #     (input=chroma:writer, action=chroma:reader, +ollama:use), idempotent, so a fresh install
    #     works without pre-creating the shipped example's tokens. Minted BEFORE the regen below.
    seeder = brain_dir / "system" / "brain_sbin" / "seed_neuron_tokens.py"
    if seeder.is_file():
        rc_s, out_s, e_s = run_out([sys.executable, str(seeder), "--brain-dir", str(brain_dir),
                                    "--action-caller"])
        (ok if rc_s == 0 else warn)(f"neuron token auto-mint {'done' if rc_s == 0 else f'rc={rc_s}'}\n{out_s}{e_s}".rstrip())
    else:
        warn(f"seed_neuron_tokens.py not staged ({seeder}); a neuron naming a token absent from the "
             f"registry will make gateway_config generate fail closed. Rebuild the package + redeploy.")

    # Regenerate the FULL ADR-0015 path-router (nginx_auto_gen/ [chroma/ollama/action/internal
    # + njs] + token_maps_auto_gen/ + fail2ban) from the seeded knobs — replaces the old
    # single-route nginx.conf.template hand-copy. gateway_config is pure host-side generation
    # (portable, no WSL), so it runs the same on native Linux.
    gcfg = brain_dir / "system" / "brain_sbin" / "gateway_config.py"
    if gcfg.is_file():
        rc_g, out_g, e_g = run_out([sys.executable, str(gcfg), "--brain-dir", str(brain_dir)])
        if rc_g != 0:
            warn(f"gateway_config generate returned {rc_g}; the path-router may be incomplete.\n{out_g}{e_g}")
        else:
            ok("path-router config regenerated from knobs (chroma/ollama/action/internal routes)")
    else:
        warn(f"gateway_config.py not staged ({gcfg}); gateway will use whatever config is in the seam")
    # The registry + generated maps are admin-owned config → root-owned, brain-readable.
    # The token_registry holds raw secrets; keep it root-only (600). The maps are 644.
    gwdir = brain_dir / "brain_etc" / "gateway"
    reg = gwdir / "token_registry"
    if reg.is_file():
        run(["chown", "root:root", str(reg)]); run(["chmod", "600", str(reg)])
    for name in ("reader_tokens.map", "writer_tokens.map", "ollama_use.map", "ollama_admin.map"):
        p = gwdir / name
        if p.is_file():
            run(["chown", "root:root", str(p)]); run(["chmod", "644", str(p)])
    ok("bootstrap tokens provisioned in brain_etc/gateway (SHOWN ONCE — save them):")
    print(f"    reader (read-only): Bearer {reader_tok}")
    print(f"    writer (read+write): Bearer {writer_tok}")

    # 3. Apply the seam (sync mode-C template + token maps from the RO mount into the runtime)
    #    and force-recreate the gateway so envsubst re-renders. Run as the brain (rootless
    #    Docker socket lives in the brain's XDG_RUNTIME_DIR; ~ = the brain home).
    compose = f"{home}/docker/compose.yaml"
    apply = (f"bash {MOUNT_POINT}/scripts/apply_brain_truths.sh -- "
             f"docker compose -f {compose} up -d --force-recreate gateway")
    rc, out, e = brain_sh(brain, apply)
    if rc != 0:
        # Fall back to a plain recreate if the apply primitive isn't present in the seam yet.
        warn(f"seam apply returned {rc}; attempting a plain gateway recreate.\n{out}{e}")
        brain_sh(brain, f"cd ~/docker && docker compose up -d --force-recreate gateway")
    else:
        ok("seam applied + gateway recreated (mode C admission + bootstrap tokens live)")


# ---------------------------------------------------------------------------
# Stage: residency (systemd --user unit + linger — the Linux keepalive)
# ---------------------------------------------------------------------------
# No idle-VM to hold open, so residency is just "a user unit brings the stack up at boot,
# and linger keeps the user manager alive without a login". Much simpler than the Windows
# Task Scheduler boot task + SeBatchLogonRight. `restart: unless-stopped` on the compose
# services does the rest once the daemon is up.

def residency(args):
    brain = args.brain
    home = brain_home(brain)
    if not linger_enabled(brain):
        run(["loginctl", "enable-linger", brain])
    unit_dir = Path(home) / ".config" / "systemd" / "user"
    unit_file = unit_dir / f"{stack_service(brain)}.service"
    unit = (
        "[Unit]\n"
        "Description=Brain Chroma+gateway stack (bring up at boot)\n"
        "After=docker.service\n"
        "Wants=docker.service\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        f"WorkingDirectory={home}/docker\n"
        "ExecStart=/usr/bin/docker compose up -d\n"
        "ExecStop=/usr/bin/docker compose down\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    # Write + enable as the brain (user unit).
    brain_sh(brain, f"mkdir -p {shell_quote(str(unit_dir))}")
    brain_sh(brain, f"cat > {shell_quote(str(unit_file))} <<'EOF'\n{unit}EOF")
    rc, out, e = brain_sh(brain,
        f"systemctl --user daemon-reload && systemctl --user enable --now {stack_service(brain)}.service")
    if rc != 0:
        die(f"failed to enable the residency user unit:\n{out}{e}")
    ok(f"residency wired (systemd --user {stack_service(brain)} enabled + linger on)")


# ---------------------------------------------------------------------------
# Stage: verify (TLS gate + persistence, through the brain)
# ---------------------------------------------------------------------------

def verify(args):
    brain = args.brain
    _, brain_dir = brain_paths(args)
    port = args.port

    # Mode C: no-token → 403.
    hb_notoken = (f"curl -s -o /dev/null -w '%{{http_code}}' --cacert ~/gateway/gateway_out/cert.pem "
                  f"https://127.0.0.1:{port}/api/v2/heartbeat")
    rc, out, e = brain_sh(brain, hb_notoken)
    code = _http_code(out)
    if rc != 0 or code != "403":
        die(f"VERIFY FAILED — no-token heartbeat expected 403 (mode C gate closed), got '{code}' "
            f"(rc={rc}).\n  (HTTP 200 = the gateway is running read-open mode B, not mode C — the "
            f"seam apply did not push mode C.)\n{out}{e}")
    ok(f"no-token heartbeat 403 on :{port} (mode C — admission gate closed)")

    # Reader token → 200.
    reader = _read_seam_token(brain_dir, "reader")
    if reader:
        hb_reader = (f"curl -s -o /dev/null -w '%{{http_code}}' --cacert ~/gateway/gateway_out/cert.pem "
                     f"-H 'Authorization: Bearer {reader}' https://127.0.0.1:{port}/api/v2/heartbeat")
        rc, out, e = brain_sh(brain, hb_reader)
        code = _http_code(out)
        if rc != 0 or code != "200":
            die(f"VERIFY FAILED — reader-token heartbeat expected 200, got '{code}' (rc={rc}).\n{out}{e}")
        ok("reader-token heartbeat 200 — Chroma reachable through the gateway")
    else:
        warn("no reader token in brain_etc/gateway/reader_tokens.map — skipping token'd-read check")

    # Reset → 403 (write-sealed).
    reset = (f"curl -s -o /dev/null -w '%{{http_code}}' --cacert ~/gateway/gateway_out/cert.pem "
             f"-X POST https://127.0.0.1:{port}/api/v2/reset")
    rc, out, e = brain_sh(brain, reset)
    code = _http_code(out)
    if code != "403":
        warn(f"reset endpoint returned '{code}', expected 403 (write-sealed).")
    else:
        ok("reset endpoint 403 (write-sealed) — gateway posture correct")

    # Persistence gate: linger on + the residency user unit enabled + rootless docker active.
    if getattr(args, "skip_residency", False):
        warn("residency skipped (--skip-residency) — persistence across reboot is NOT guaranteed.")
    else:
        if not linger_enabled(brain):
            die("VERIFY FAILED — linger is NOT enabled for the brain, so its user manager (and the\n"
                "    stack) will not come up at boot without a login. Deploy without --skip-residency.")
        rc, out, _ = brain_sh(brain,
            f"systemctl --user is-enabled {stack_service(brain)}.service")
        if "enabled" not in (out or ""):
            die(f"VERIFY FAILED — residency unit {stack_service(brain)} is not enabled "
                f"(got '{out.strip()}'); the stack will not start at boot.")
        ok("residency holding: linger on + stack user unit enabled — persistence wired")

    ok("VERIFY PASSED")


# ---------------------------------------------------------------------------
# Verb handlers
# ---------------------------------------------------------------------------

def cmd_deploy(args):
    validate_brain_name(args.brain)
    banner(f"Deploy brain (Linux): {args.brain}  (posture={args.posture})")
    total = 8 if not args.skip_gateway else 6

    stage(1, total, "Preflight");                 preflight(args)
    stage(2, total, "Create brain");              create_brain(args)
    stage(3, total, "Stage code package");        stage_package(args)
    stage(4, total, "Provision runtime (rootless Docker + stack)"); provision_runtime(args)
    stage(5, total, "Config-exposure seam");      seam(args)
    if not args.skip_gateway:
        stage(6, total, "Gateway (port + token)"); gateway(args)
        stage(7, total, "Residency (systemd + linger)");
        if not args.skip_residency:
            residency(args)
        else:
            info("--skip-residency: stack up; boot persistence NOT wired")
        stage(8, total, "Verify");                verify(args)
    else:
        info("--skip-gateway: runtime provisioned; gateway + residency + verify skipped")

    banner(f"DEPLOY COMPLETE: {args.brain}")


def cmd_teardown(args):
    validate_brain_name(args.brain)
    destructive = args.purge
    banner(f"Teardown brain (Linux): {args.brain}  ({'PURGE' if destructive else 'stop/reset'})")
    if destructive and not args.yes:
        die("--purge is destructive (deletes the brain user + home + all stack data).\n"
            "    Re-run with --yes to confirm.")
    brain = args.brain
    _, brain_dir = brain_paths(args)

    # 1. Stop + disable the residency user unit; bring the stack down.
    if user_exists(brain):
        brain_sh(brain, f"systemctl --user disable --now {stack_service(brain)}.service 2>/dev/null || true")
        brain_sh(brain, "cd ~/docker && docker compose down 2>/dev/null || true")
        ok("stack stopped + residency unit disabled")

    # 2. Unmount + remove the seam mount unit.
    run(["systemctl", "disable", "--now", seam_mount_unit()], check=False)
    unit_path = Path("/etc/systemd/system") / seam_mount_unit()
    if unit_path.is_file():
        unit_path.unlink()
        run(["systemctl", "daemon-reload"], check=False)
    ok("seam mount removed")

    # 3. Firewall (server posture) — best-effort ufw close.
    if shutil.which("ufw"):
        run(["ufw", "delete", "allow", f"{args.port}/tcp"], check=False)

    # 4. Purge: linger off, delete the user + home, remove the brain folder.
    if destructive:
        run(["loginctl", "disable-linger", brain], check=False)
        run(["userdel", "--remove", brain], check=False)
        if brain_dir.is_dir():
            shutil.rmtree(brain_dir, ignore_errors=True)
        ok(f"account {brain} + home + brain folder removed (data deleted)")
    else:
        info("non-destructive: user + data preserved (re-deploy to bring the stack back up)")

    banner(f"TEARDOWN COMPLETE: {args.brain}")


def cmd_verify(args):
    validate_brain_name(args.brain)
    banner(f"Verify brain (Linux): {args.brain}")
    verify(args)


def cmd_status(args):
    validate_brain_name(args.brain)
    banner(f"Status (Linux): {args.brain}")
    _, brain_dir = brain_paths(args)
    brain = args.brain
    print(f"  account exists   : {user_exists(brain)}")
    print(f"  brain folder     : {brain_dir}  ({'present' if brain_dir.is_dir() else 'MISSING'})")
    staged = (brain_dir / 'system' / 'brain_bin' / 'gateway' / 'gen-cert.sh').is_file()
    print(f"  code staged      : {staged}")
    print(f"  linger enabled   : {linger_enabled(brain) if user_exists(brain) else False}")
    docker_ok = _docker_ready(brain) if user_exists(brain) else False
    print(f"  rootless docker  : {docker_ok}")
    rc, out, _ = run_out(["findmnt", "-no", "OPTIONS", MOUNT_POINT])
    print(f"  seam mounted     : {bool(out.strip())} ({out.strip() or 'not mounted'})")
    if user_exists(brain):
        _, s_out, _ = brain_sh(brain, f"systemctl --user is-enabled {stack_service(brain)}.service 2>/dev/null")
        print(f"  residency unit   : {(s_out or 'absent').strip()}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args():
    ap = argparse.ArgumentParser(
        description="Brain deploy orchestrator (native Linux) — systemd + rootless Docker, no VM.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deploy", help="stand up a brain end-to-end on this Linux host")
    d.add_argument("--brain", required=True)
    d.add_argument("--posture", choices=("personal", "server"), default="personal")
    d.add_argument("--port", type=int, default=8443, help="gateway host port (default 8443)")
    d.add_argument("--bind", choices=("personal", "server", "127.0.0.1", "0.0.0.0"),
                   default=None, help="gateway bind (default: follow --posture)")
    d.add_argument("--package", default=None, help="code package tar (default: newest in dist/)")
    d.add_argument("--install-root", default=None,
                   help="dir containing brains/<brain>/ (default: $AIOS_INSTALL_ROOT / autodetect)")
    d.add_argument("--skip-residency", action="store_true",
                   help="deploy the stack but do not enable the boot residency unit")
    d.add_argument("--skip-gateway", action="store_true",
                   help="stop after runtime provision (no gateway/token/residency/verify)")
    d.set_defaults(func=cmd_deploy)

    t = sub.add_parser("teardown", help="stop/reset (default) or purge a brain")
    t.add_argument("--brain", required=True)
    t.add_argument("--purge", action="store_true",
                   help="destructive: delete the brain user + home + brain folder (all data)")
    t.add_argument("--yes", action="store_true", help="confirm a --purge")
    t.add_argument("--port", type=int, default=8443, help="gateway port (for firewall cleanup)")
    t.add_argument("--install-root", default=None)
    t.set_defaults(func=cmd_teardown)

    v = sub.add_parser("verify", help="TLS heartbeat + reset=403 + residency-enabled through the gateway")
    v.add_argument("--brain", required=True)
    v.add_argument("--port", type=int, default=8443)
    v.add_argument("--install-root", default=None)
    v.add_argument("--skip-residency", action="store_true",
                   help="do not assert the residency unit is enabled")
    v.set_defaults(func=cmd_verify)

    s = sub.add_parser("status", help="show what exists for a brain")
    s.add_argument("--brain", required=True)
    s.add_argument("--install-root", default=None)
    s.set_defaults(func=cmd_status)

    return ap.parse_args()


def main():
    # parse_args first so `--help` works for inspection on any OS (argparse exits there).
    args = parse_args()
    # But the verbs actually touch a Linux host — refuse to RUN them off Linux.
    if not sys.platform.startswith("linux"):
        die("linux_deploy_brain.py targets native Linux (systemd + rootless Docker). On "
            "Windows use windows_deploy_brain.py (WSL2); macOS support is objective 009.")
    # Every mutating verb needs root up front (status is read-only).
    if args.cmd in ("deploy", "teardown"):
        require_root()
    args.func(args)


if __name__ == "__main__":
    main()
