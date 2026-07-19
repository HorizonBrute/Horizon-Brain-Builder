#!/usr/bin/env python3
"""
brain_installer_2_brain.py - BRAIN-side deploy: file structure + instantiate the engine.

Runs AS the brain user (launched by phase 1, or manually via
`runas /user:<brain> "python brain_installer_2_brain.py --brain <brain>"`).

What it does:
  1. Create the Windows-side brain file structure (knowledge/{brain_ro,brain_rw/chroma}).
  2. Import the preconfigured engine image under THIS (brain) Windows account:
       wsl --import brain-<brain> <brain>/system/wsl_engine/disk
                                 <brain>/system/wsl_engine/<brain>_engine.tar --version 2
     (skipped if already registered).
  3. First-boot restart (wsl --terminate) so wsl.conf systemd=true + default user apply.
  4. Bring the stack up inside the distro and verify TLS heartbeat + data ownership.

Registering the WSL distro needs NO admin - it is per-user, which is the point: it lands
under the brain's account and is invisible to the owner via \\wsl$. The boot RESIDENCY task
is NOT registered here: it is boot-triggered + run-whether-logged-on, which needs admin to
create, so installer_1 (admin, holds the brain password) owns it via deploy/residency.py.

Usage:
  python brain_installer_2_brain.py [--brain NAME]
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRAIN_DIR = HERE.parent.parent.parent
PROVISION = BRAIN_DIR / ".brain_provision.json"

# MUST match windows_deploy_brain.py / brain_installer_1_admin.py (ADR-0019 amended).
WSL_RUNTIME_REL = ("system", "wsl_engine")

# residency.py (same dir) owns the boot-keepalive script content + path. Phase 2 is the ONLY
# deploy stage that runs AS THE BRAIN and can therefore SEE the brain's per-user WSL distro,
# so it ships the script IN here; installer_1 (admin) later registers the boot TASK that runs
# it. That namespace split is deliberate — an elevated admin session cannot reach the distro.
sys.path.insert(0, str(HERE))
try:
    import residency
except Exception:
    residency = None


def info(m): print(f"  {m}")
def step(n, m): print(f"\n[{n}] {m}")
def die(m): print(f"  [ERROR] {m}", file=sys.stderr); sys.exit(1)


def brain_name(args):
    if args.brain:
        return args.brain
    if PROVISION.is_file():
        try:
            return json.loads(PROVISION.read_text(encoding="utf-8"))["brain_name"]
        except Exception:
            pass
    return BRAIN_DIR.name


def wsl(*a, **kw):
    return subprocess.run(["wsl", *a], capture_output=True, text=True, **kw)


def make_structure():
    # config-flow Phase 5 removed knowledge/inbox/; the real data-in layout (brain_ro / brain_rw/
    # chroma) is created in-distro by mk_knowledge_layout.sh as the brain.
    for sub in ("knowledge", "knowledge/brain_ro", "knowledge/brain_rw/chroma"):
        (BRAIN_DIR / sub).mkdir(parents=True, exist_ok=True)
    info("knowledge/ layout ready (brain_ro, brain_rw/chroma)")


def distro_registered(distro):
    # wsl --list outputs UTF-16; decode leniently and look for the name.
    p = subprocess.run(["wsl", "--list", "--quiet"], capture_output=True)
    text = p.stdout.decode("utf-16", errors="ignore") + p.stdout.decode("utf-8", errors="ignore")
    return distro in text


def import_engine(brain):
    distro = f"brain-{brain}"
    wsl_dir = BRAIN_DIR.joinpath(*WSL_RUNTIME_REL)
    disk = wsl_dir / "disk"
    tar = wsl_dir / f"{brain}_engine.tar"
    if distro_registered(distro):
        info(f"{distro} already registered - skipping import")
        return distro
    if not tar.is_file():
        die(f"engine image not found: {tar}")
    disk.mkdir(parents=True, exist_ok=True)
    info(f"importing {distro} (VHDX -> {disk})...")
    p = wsl("--import", distro, str(disk), str(tar), "--version", "2")
    if p.returncode != 0:
        die(f"import failed: {p.stderr.strip() or p.stdout.strip()}")
    info(f"{distro} imported under this (brain) account")
    return distro


def bring_up_and_verify(distro):
    info("starting Chroma (first boot may take a few seconds for rootless docker)...")
    for attempt in range(1, 5):
        up = wsl("-d", distro, "--", "bash", "-lc", "cd ~/docker && docker compose up -d")
        if up.returncode == 0:
            break
        info(f"  docker not ready yet (attempt {attempt}); waiting...")
        time.sleep(8)
    time.sleep(6)
    # Chroma is sealed behind the TLS gateway (stage4) — verify THROUGH the gateway over
    # TLS, not a plaintext direct port (which no longer exists).
    hb = wsl("-d", distro, "--", "bash", "-lc",
             "curl -s --cacert ~/gateway/gateway_out/cert.pem https://127.0.0.1:8000/api/v2/heartbeat")
    own = wsl("-d", distro, "--", "bash", "-lc", "ls -ln ~/chroma_store | tail -n +2")
    print("\n  heartbeat:", (hb.stdout or hb.stderr).strip())
    print("  data ownership (expect uid 1000):")
    for line in (own.stdout or "").splitlines():
        print("   ", line)
    ok = "nanosecond heartbeat" in (hb.stdout or "")
    print(f"\n  RESULT: {'OK - engine live and brain-owned' if ok else 'CHECK - heartbeat not seen'}")


def first_boot_restart(distro):
    """A freshly-imported distro must be terminated once so wsl.conf (systemd=true +
    default user) is applied on the next start — mirrors the build-time restart. Cheap
    and idempotent (no-op if not running)."""
    info(f"terminating {distro} so wsl.conf (systemd + default user) applies on next start...")
    wsl("--terminate", distro)


# NOTE: the boot RESIDENCY task is NOT registered here. Phase 2 runs as the NON-elevated
# brain and cannot create a BootTrigger task; registration is an admin concern owned by
# brain_installer_1_admin.py via the shared deploy/residency.py module. See that module's
# header for the Password-vs-S4U rationale.


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain")
    args = ap.parse_args()
    brain = brain_name(args)

    print("=" * 60)
    print(f"Brain engine deploy (brain user) - {brain}")
    print("=" * 60)
    step(1, "file structure"); make_structure()
    step(2, "instantiate preconfigured engine"); distro = import_engine(brain)
    step(3, "first-boot restart (apply systemd + default user)"); first_boot_restart(distro)
    step(4, "bring up + verify"); bring_up_and_verify(distro)
    step(5, "install boot keepalive script (~/keepalive.sh)")
    if residency is not None:
        residency.write_keepalive(distro, info)
    else:
        info("[WARN] residency.py not importable — keepalive.sh NOT written; the boot task "
             "installer_1 registers will fail to launch until it exists.")
    info("residency (boot keepalive TASK) is registered by installer_1 (admin), not here.")
    print("\n[done] brain phase complete. See system/brain_bin/DEPLOYMENT.md §Residency for operations.")


if __name__ == "__main__":
    main()
