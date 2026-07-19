#!/usr/bin/env python3
"""symlink_into_brain — expose an external folder/file inside a brain's knowledge seam.

The admin-friendly knob for ON-DISK source delivery (Way 1): link an external source into
a brain's `knowledge/<zone>/` and set the correct brain permission, without hand-running
`mklink` / `icacls`. Layout IS the documentation — the link appears beside the brain's other
sources; the zone decides the brain's read/write posture.

    add   SOURCE [DEST]   link SOURCE in as knowledge/<zone>/<DEST>   (DEST defaults to SOURCE's name)
    list                  show the links present in the zone
    remove DEST           remove the link (never touches the link's target)

Zones (per knowledge/README.md):
    brain_ro  (default)  brain READ-ONLY — unprocessed source content. The brain reads it,
                         never writes it. `add` grants the brain Read+Execute on the target.
    brain_rw             brain READ-WRITE — brain-produced data a service writes.

Defaults resolve from this tool's own location: it lives in <brain_dir>/system/brain_sbin/, so
--brain / --brain-dir default to that brain. Override for another brain.

    python system/brain_sbin/symlink_into_brain.py add C:\\data\\specs
    python system/brain_sbin/symlink_into_brain.py add C:\\data\\specs product_specs --zone brain_ro
    python system/brain_sbin/symlink_into_brain.py list
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

ZONES = ("brain_ro", "brain_rw")


def default_brain_dir() -> Path:
    # <brain_dir>/system/brain_sbin/symlink_into_brain.py -> <brain_dir>
    return Path(__file__).resolve().parent.parent.parent


def zone_dir(brain_dir: Path, zone: str) -> Path:
    return brain_dir / "knowledge" / zone


def _icacls(path: Path, *rules) -> bool:
    p = subprocess.run(["icacls", str(path), *rules, "/C"], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(f"  [WARN] icacls {rules}: {(p.stderr or p.stdout).strip()}\n")
    return p.returncode == 0


def _is_link(p: Path) -> bool:
    """True for a symlink OR a Windows junction. os.readlink succeeds on both (py3.8+);
    os.path.islink returns False for junctions, so it can't be used alone."""
    try:
        os.readlink(p)
        return True
    except OSError:
        return False


def _make_link(link: Path, target: Path) -> str:
    """Create link -> target and return the kind made. For a DIRECTORY on Windows, prefer a
    JUNCTION (`mklink /J`): transparent for reading and — unlike a symlink — needs NO elevation,
    which is the whole point of a friendly admin tool. Fall back to a symlink (needs privilege /
    Developer Mode) or, for a file, a hard link (`/H`, same-volume, no elevation)."""
    is_dir = target.is_dir()
    if os.name == "nt" and is_dir:
        p = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True, text=True)
        if p.returncode == 0:
            return "junction"
    try:
        os.symlink(target, link, target_is_directory=is_dir)
        return "symlink"
    except (OSError, NotImplementedError) as e:
        if os.name != "nt":
            raise
        flag = "/D" if is_dir else "/H"      # /D symlinkd (needs priv); /H hardlink (file, no priv)
        p = subprocess.run(["cmd", "/c", "mklink", flag, str(link), str(target)],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"could not link ({(p.stderr or p.stdout).strip()}); "
                               f"os.symlink said: {e}")
        return "symlink" if is_dir else "hardlink"


def _remove_link(link: Path) -> None:
    """Remove the LINK only, never its target. A dir link (junction/symlinkd) is removed with
    rmdir (removes the reparse point, not the contents); a file link with unlink."""
    if link.is_dir() and not os.path.islink(link):   # junction or symlinkd
        os.rmdir(link)
    else:
        link.unlink()


def _set_zone_acl(brain: str, target: Path, zone: str) -> None:
    """Set the brain's posture on the LINK TARGET (the real bytes): read-only for brain_ro,
    read-write for brain_rw. Uses the drvfs-safe pattern (RX/RW allow, no deny ACE) so a
    read-only mount can't be broken by a write-implying open. Best-effort (warns, never dies)."""
    group = f"{brain}_group"
    perm = "RX" if zone == "brain_ro" else "M"           # brain_ro: read+execute; brain_rw: modify
    ci = "(OI)(CI)" if target.is_dir() else ""           # container/object inherit for a dir target
    recurse = ["/T"] if target.is_dir() else []
    # Grant the brain user AND its per-brain group the zone posture. No deny ACE (drvfs read-safe).
    _icacls(target, "/grant", f"{brain}:{ci}{perm}",
                    "/grant", f"{group}:{ci}{perm}", *recurse)


def cmd_add(args) -> int:
    brain_dir = Path(args.brain_dir)
    brain = args.brain
    source = Path(args.source).resolve()
    if not source.exists():
        sys.stderr.write(f"[ERROR] source does not exist: {source}\n"); return 1
    zdir = zone_dir(brain_dir, args.zone)
    if not zdir.parent.is_dir():
        sys.stderr.write(f"[ERROR] {zdir.parent} not found — is --brain-dir a brain?\n"); return 1
    dest = args.dest or source.name
    link = zdir / dest
    zdir.mkdir(parents=True, exist_ok=True)
    if _is_link(link):
        if not args.force:
            sys.stderr.write(f"[ERROR] link already exists: {link} (use --force to replace)\n"); return 1
        _remove_link(link)                       # removes the link only, never its target
    elif link.exists():
        sys.stderr.write(f"[ERROR] {link} exists and is NOT a link — refusing to touch real data\n"); return 1
    try:
        kind = _make_link(link, source)
    except Exception as e:
        sys.stderr.write(f"[ERROR] could not create link: {e}\n"); return 1
    _set_zone_acl(brain, source, args.zone)
    posture = "READ-ONLY" if args.zone == "brain_ro" else "READ-WRITE"
    print(f"  linked  knowledge/{args.zone}/{dest}  ->  {source}   ({kind})")
    print(f"  posture {posture} to the brain (target ACL granted to {brain} / {brain}_group)")
    return 0


def cmd_list(args) -> int:
    brain_dir = Path(args.brain_dir)
    any_found = False
    for zone in ZONES:
        zdir = zone_dir(brain_dir, zone)
        if not zdir.is_dir():
            continue
        links = [p for p in sorted(zdir.iterdir()) if _is_link(p)]
        if links:
            any_found = True
            print(f"{zone}/:")
            for p in links:
                target = os.readlink(p)
                print(f"  {p.name}  ->  {target}")
    if not any_found:
        print("  (no links in any zone)")
    return 0


def cmd_remove(args) -> int:
    brain_dir = Path(args.brain_dir)
    link = zone_dir(brain_dir, args.zone) / args.dest
    if not _is_link(link):
        sys.stderr.write(f"[ERROR] not a link: {link}\n"); return 1
    _remove_link(link)
    print(f"  removed  knowledge/{args.zone}/{args.dest}  (target untouched)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    bd = default_brain_dir()
    ap = argparse.ArgumentParser(prog="symlink_into_brain",
                                 description="Link an external source into a brain's knowledge zone.")
    ap.add_argument("--brain", default=bd.name, help="brain name (default: this brain)")
    ap.add_argument("--brain-dir", default=str(bd), help="brain root dir (default: this brain)")
    sub = ap.add_subparsers(dest="verb", required=True)
    a = sub.add_parser("add", help="link SOURCE in as knowledge/<zone>/<DEST>")
    a.add_argument("source"); a.add_argument("dest", nargs="?")
    a.add_argument("--zone", choices=ZONES, default="brain_ro", help="knowledge zone (default: brain_ro)")
    a.add_argument("--force", action="store_true", help="replace an existing link")
    a.set_defaults(func=cmd_add)
    sub.add_parser("list", help="show links in each zone").set_defaults(func=cmd_list)
    r = sub.add_parser("remove", help="remove a link (target untouched)")
    r.add_argument("dest")
    r.add_argument("--zone", choices=ZONES, default="brain_ro", help="knowledge zone (default: brain_ro)")
    r.set_defaults(func=cmd_remove)
    return ap


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
