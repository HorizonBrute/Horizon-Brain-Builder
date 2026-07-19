#!/usr/bin/env python3
r"""
neurons_mount.py — mount the neuron CODE-IN seams read-only into the distro.

The neuron programs live on the host (admin read-write) and are exposed INSIDE the brain's
WSL distro as read-only drvfs mounts — the code-in seams. There are TWO, one per neuron ROLE:

    input neurons   host `input_neurons/`   -> /opt/input_neurons    (the WRITE side)
    action neurons  host `action_neurons/`  -> /opt/action_neurons   (the READ side)

Both are the twin of the config seam (`/opt/brain_truths`, brain_truths.py) and the
admin-script seam (`/opt/brain_wsl_in_distro_scripts`, wsl_scripts.py): the brain EXECUTES
its neuron code but cannot modify it, and host edits reflect live (no image rebuild — the
Dockerfiles bake only deps; code is baked/bind-mounted from these seams).

    neurons_mount.py [--brain NAME] [--brain-dir DIR] install     enable the RO mounts
    neurons_mount.py [--brain NAME] [--brain-dir DIR] status      show the in-distro mounts

Each compose neuron service reads its seam two ways: `build: context: /opt/<seam>` (bakes
deps from the Dockerfile) and the code at runtime. A seam whose host folder is absent is
skipped (a brain with only input neurons still installs cleanly).

The unit body is written base64-encoded (NOT printf'd) so a Windows drvfs `What=` path like
`C:\...\brains\...\action_neurons` survives verbatim — printf would eat `\a`/`\b` escapes.
"""
import argparse
import base64
import subprocess
import sys
from pathlib import Path

# The code-in seams, one per neuron role. (host subdir, in-distro mountpoint, human label).
# Add a row here to expose a new role's code seam; install/status iterate this table.
# host subdir is the SHARED per-role platform image SOURCE (config-flow Phase 5: relocated from
# the retired root input_neurons/ + action_neurons/ to system/common_neuron_platform/{input,action}/,
# built ONCE per role). The in-distro mount TARGET + image build-context path stays /opt/<role>_neurons
# so the Dockerfile build contexts + query.py's /opt/action_neurons lib imports never move.
SEAMS = [
    ("system/common_neuron_platform/input",  "/opt/input_neurons",  "input neuron code-in seam (WRITE side)"),
    ("system/common_neuron_platform/action", "/opt/action_neurons", "action neuron code-in seam (READ side)"),
]


def _self_dir() -> Path:
    return Path(__file__).resolve().parent

def _default_brain_dir() -> Path:
    # system/brain_sbin/neurons_mount.py -> brain root is three parents up.
    return _self_dir().parent.parent

def _default_brain(brain_dir: Path) -> str:
    return brain_dir.name

def _rab(brain_dir: Path) -> Path:
    return Path(brain_dir) / "system" / "brain_sbin" / "run_as_brain.py"

def _mount_unit_name(mount_point: str) -> str:
    # /opt/action_neurons -> opt-action_neurons.mount  (systemd escaping rule)
    return mount_point.strip("/").replace("/", "-") + ".mount"

def _mount_unit_text(host_seam: Path, mount_point: str, label: str) -> str:
    return (
        "[Unit]\n"
        f"Description={label} - read-only\n"
        "# no After=local-fs.target: mount units are implicitly Before=local-fs.target\n"
        "# (DefaultDependencies); an explicit After= creates an ordering cycle -> flapping.\n\n"
        "[Mount]\n"
        f"What={host_seam}\n"           # Windows path; drvfs accepts it as-is
        f"Where={mount_point}\n"
        "Type=drvfs\n"
        "Options=ro\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _rab_run(brain: str, brain_dir: Path, as_root: bool, argv: list[str]) -> int:
    cmd = [sys.executable, str(_rab(brain_dir)), "--brain", brain]
    cmd += ["--root"] if as_root else ["--wsl"]
    cmd += ["--", *argv]
    return subprocess.run(cmd, text=True).returncode


def _install_seam(brain: str, brain_dir: Path, subdir: str, mount_point: str, label: str) -> int:
    host_seam = Path(brain_dir) / subdir
    if not host_seam.is_dir():
        print(f"  [skip] {label}: host seam absent ({host_seam})")
        return 0
    unit_name = _mount_unit_name(mount_point)
    unit_b64 = base64.b64encode(
        _mount_unit_text(host_seam, mount_point, label).encode("utf-8")).decode("ascii")
    unit_path = f"/etc/systemd/system/{unit_name}"
    # Ensure the mountpoint, write the unit, reload, enable --now. mkdir -p is idempotent even
    # when the mountpoint is already a live read-only mount (never chmods it -> no EROFS).
    cmd = (f"mkdir -p {mount_point} && "
           f"echo {unit_b64} | base64 -d > {unit_path} && "
           f"systemctl daemon-reload && "
           f"systemctl enable --now {unit_name}")
    if len(cmd) > 1000:
        print(f"  [ERROR] {label}: install command {len(cmd)} chars - exceeds the safe bridge cap")
        return 1
    print(f"  enabling {label} ({host_seam} -> {mount_point})")
    rc = _rab_run(brain, brain_dir, True, [cmd])
    if rc != 0:
        print(f"  [WARN] {label}: mount enable returned nonzero - is the distro up?")
        return rc
    print(f"    {mount_point} mounted read-only")
    return 0


def cmd_install(args) -> int:
    brain, brain_dir = args.brain, Path(args.brain_dir)
    print(f"enabling neuron code-in RO mounts in brain-{brain}:")
    rc = 0
    for subdir, mount_point, label in SEAMS:
        rc |= _install_seam(brain, brain_dir, subdir, mount_point, label)
    return rc


def cmd_status(args) -> int:
    brain, brain_dir = args.brain, Path(args.brain_dir)
    print(f"neuron code seam mount status in brain-{brain}:")
    checks = " ; ".join(
        f"echo '== {mp} =='; findmnt {mp} || echo '(not mounted)'"
        for _, mp, _ in SEAMS)
    return _rab_run(brain, brain_dir, False, [checks])


def main() -> int:
    ap = argparse.ArgumentParser(description="Neuron code-in seams (RO drvfs mounts).")
    ap.add_argument("--brain-dir", default=str(_default_brain_dir()))
    ap.add_argument("--brain")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("install")
    sub.add_parser("status")

    args = ap.parse_args()
    if not args.brain:
        args.brain = _default_brain(Path(args.brain_dir))
    return {"install": cmd_install, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
