#!/usr/bin/env python3
"""
wsl_scripts.py — mount and run in-distro admin scripts.

The folder system/brain_sbin/wsl_in_distro_scripts/ is mounted read-only into the brain's
WSL distro at /opt/brain_wsl_in_distro_scripts. Scripts are authored on the host
(admin read-write) and run in-distro by path (brain read-only).

    wsl_scripts.py [--brain NAME] [--brain-dir DIR] install     enable the read-only mount
    wsl_scripts.py [--brain NAME] [--brain-dir DIR] list        list available scripts
    wsl_scripts.py [--brain NAME] [--brain-dir DIR] run NAME [-- ARGS...]   run one in-distro

run picks the interpreter by extension (.py -> python3, .sh -> bash) and executes
through run_as_brain (brain identity; --as-root for root in the distro).
"""
import argparse
import base64
import shlex
import subprocess
import sys
from pathlib import Path

SEAM_SUBDIR = "wsl_in_distro_scripts"                   # host folder under system/brain_sbin/
MOUNT_POINT = "/opt/brain_wsl_in_distro_scripts"        # RO view inside the distro (mirrors the host name)


def _self_dir() -> Path:
    return Path(__file__).resolve().parent

def _default_brain_dir() -> Path:
    # system/brain_sbin/wsl_scripts.py -> brain root is three parents up.
    return _self_dir().parent.parent

def _default_brain(brain_dir: Path) -> str:
    return brain_dir.name

def _host_seam(brain_dir: Path) -> Path:
    return Path(brain_dir) / "system" / "brain_sbin" / SEAM_SUBDIR

def _rab(brain_dir: Path) -> Path:
    return Path(brain_dir) / "system" / "brain_sbin" / "run_as_brain.py"

def _mount_unit_name() -> str:
    # /opt/brain_scripts -> opt-brain_scripts.mount  (systemd escaping rule)
    return MOUNT_POINT.strip("/").replace("/", "-") + ".mount"

def _mount_unit_text(host_seam: Path) -> str:
    return (
        "[Unit]\n"
        "Description=Brain scripts - brain_sbin in-distro tooling, read-only\n"
        "# no After=local-fs.target: mount units are implicitly Before=local-fs.target\n"
        "# (DefaultDependencies); an explicit After= creates an ordering cycle -> flapping.\n\n"
        "[Mount]\n"
        f"What={host_seam}\n"          # Windows path; drvfs accepts it as-is
        f"Where={MOUNT_POINT}\n"
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


def cmd_install(args) -> int:
    brain, brain_dir = args.brain, Path(args.brain_dir)
    host_seam = _host_seam(brain_dir)
    if not host_seam.is_dir():
        print(f"[ERROR] seam folder does not exist: {host_seam}")
        return 1
    unit_b64 = base64.b64encode(_mount_unit_text(host_seam).encode("utf-8")).decode("ascii")
    unit_path = f"/etc/systemd/system/{_mount_unit_name()}"
    # Ensure the mountpoint, write the unit, reload, enable. mkdir -p is idempotent even
    # when the mountpoint is already a live read-only mount.
    cmd = (f"mkdir -p {MOUNT_POINT} && "
           f"echo {unit_b64} | base64 -d > {unit_path} && "
           f"systemctl daemon-reload && "
           f"systemctl enable --now {_mount_unit_name()}")
    if len(cmd) > 1000:
        print(f"[ERROR] install command {len(cmd)} chars - exceeds the safe bridge cap")
        return 1
    print(f"enabling brain-scripts RO mount ({host_seam} -> {MOUNT_POINT}) in brain-{brain}")
    rc = _rab_run(brain, brain_dir, True, [cmd])
    if rc != 0:
        print("  [WARN] mount enable returned nonzero - is the distro up?")
        return rc
    print(f"  {MOUNT_POINT} mounted read-only (run scripts by path from there)")
    return 0


def cmd_list(args) -> int:
    seam = _host_seam(Path(args.brain_dir))
    if not seam.is_dir():
        print(f"(no seam folder yet: {seam})")
        return 0
    print(f"scripts in {seam}  (run in-distro at {MOUNT_POINT}/):")
    for p in sorted(seam.iterdir()):
        if p.is_file() and p.suffix in (".py", ".sh"):
            print(f"  {p.name}")
    return 0


def cmd_run(args) -> int:
    brain, brain_dir = args.brain, Path(args.brain_dir)
    name = args.name
    seam = _host_seam(brain_dir)
    if not (seam / name).is_file():
        print(f"[ERROR] no such script: {seam / name}")
        return 1
    suffix = Path(name).suffix
    interp = {".py": "python3", ".sh": "bash"}.get(suffix)
    if interp is None:
        print(f"[ERROR] unsupported script type '{suffix}' (want .py or .sh)")
        return 1
    remote = f"{MOUNT_POINT}/{name}"
    # Build one shell string; shlex.quote guards spaces in the script's own args.
    cmd = " ".join([interp, shlex.quote(remote)] + [shlex.quote(a) for a in args.script_args])
    print(f"running {remote} in brain-{brain} ({'root' if args.as_root else 'brain'})")
    return _rab_run(brain, brain_dir, args.as_root, [cmd])


def main() -> int:
    ap = argparse.ArgumentParser(description="In-distro admin-script seam (mount + run).")
    ap.add_argument("--brain-dir", default=str(_default_brain_dir()))
    ap.add_argument("--brain")
    ap.add_argument("--as-root", action="store_true",
                    help="run inside the distro as root (default: the brain identity)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("install")
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("name")
    r.add_argument("script_args", nargs=argparse.REMAINDER,
                   help="args after the script name (a leading -- is stripped)")

    args = ap.parse_args()
    if not args.brain:
        args.brain = _default_brain(Path(args.brain_dir))
    # Strip a leading '--' separator from REMAINDER so `run foo -- a b` passes [a, b].
    if getattr(args, "script_args", None) and args.script_args and args.script_args[0] == "--":
        args.script_args = args.script_args[1:]

    return {"install": cmd_install, "list": cmd_list, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
