#!/usr/bin/env python3
"""
brain_python_resolver.py — Brain-Runnable Python Resolver
=========================================================

Resolve a Python interpreter that a *brain service account* can actually
execute, and provide an installer preflight around it.

Brain-owned, stdlib-only, and deliberately free of any dependency on the
host platform environment — this module travels with the brain when the brain
is dropped onto another machine.

WHY THIS EXISTS
---------------
A brain runs as its own least-privileged OS account. The two-phase brain
installer hands off to phase 2 with `runas /user:<brain> "<python> ..."`.
If that `<python>` is the admin's **per-user Microsoft Store** interpreter
(under `C:\\Users\\<admin>\\...\\WindowsApps\\...python.exe`), the emitted
command is un-runnable as the brain for two reasons:

  1. It lives under another user's profile — the brain has no read access there.
  2. Store Python is an MSIX per-user app aliased to the installing user only.

So an interpreter is "brain-runnable" only if it is installed **machine-wide /
all-users**, outside any user profile. This module resolves such an interpreter
(or reports that none exists, loudly), so callers never bake `sys.executable`
into a cross-account handoff.

We cannot *prove* runnability without actually launching as the brain (that is
run_as_brain's job); machine-wide location is the correct, admin-checkable proxy.

CLI Usage
---------
    python brain_python_resolver.py resolve            # print best interpreter path
    python brain_python_resolver.py resolve --json     # machine-readable
    python brain_python_resolver.py preflight          # exit 0 if a brain-runnable Python exists, else 2 + guidance
    python brain_python_resolver.py list               # show every candidate and why it was kept/rejected
    [--min X.Y]  minimum acceptable version (default 3.9)

Exit codes: 0 = resolved / OK, 2 = no brain-runnable Python found, 1 = usage/other error.

Importable interface (used by the brain installer's launch_phase2 and run_as_brain)
    from brain_python_resolver import resolve_brain_python, is_brain_runnable
    rp = resolve_brain_python(min_version=(3, 9))   # -> ResolvedPython | None
    if rp: print(rp.path, rp.version)
"""

import argparse
import glob
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

MIN_VERSION_DEFAULT: Tuple[int, int] = (3, 9)


class ResolvedPython(NamedTuple):
    path: str                     # absolute path to the interpreter
    version: Tuple[int, int, int] # (major, minor, micro) as reported by the interpreter itself
    source: str                   # where we discovered it: 'registry' | 'py-launcher' | 'program-files' | 'path'


class Candidate(NamedTuple):
    path: str
    source: str


# ---------------------------------------------------------------------------
# Runnability test (the core policy)
# ---------------------------------------------------------------------------

def is_brain_runnable(interpreter_path: str) -> bool:
    """True if a brain service account could plausibly execute this interpreter.

    Policy: the interpreter must live in a **machine-wide** location, i.e. NOT
    under any user profile and NOT the Store/WindowsApps MSIX alias. This is the
    admin-checkable proxy for "the brain can run it" — see module docstring.
    """
    try:
        p = Path(interpreter_path).resolve()
    except Exception:
        return False

    low = str(p).lower()
    parts_low = [seg.lower() for seg in p.parts]

    if platform.system() == 'Windows':
        # Store Python / execution aliases live under ...\WindowsApps\...
        if 'windowsapps' in parts_low:
            return False
        # Any interpreter under a user profile is per-user; another account
        # (the brain) has no read access there.
        users_root = (os.environ.get('SystemDrive', 'C:') + os.sep + 'Users' + os.sep).lower()
        if low.startswith(users_root):
            return False
        return True

    # Unix: reject interpreters under a home directory (/home/*, /Users/* on macOS,
    # or the current user's ~). Accept system locations (/usr, /opt, /Library).
    for home_root in ('/home/', '/users/'):
        if low.startswith(home_root):
            return False
    home = os.path.expanduser('~').lower()
    if home and low.startswith(home + os.sep):
        return False
    return True


# ---------------------------------------------------------------------------
# Candidate discovery (Windows)
# ---------------------------------------------------------------------------

def _win_candidates_from_registry() -> List[Candidate]:
    """Machine-wide interpreters registered under HKLM\\SOFTWARE\\Python\\PythonCore.

    HKLM = all-users (authoritative machine-wide). We deliberately do NOT read
    HKCU (that is the per-user trap this whole module exists to avoid).
    """
    found: List[Candidate] = []
    try:
        import winreg
    except ImportError:
        return found

    # Read both registry views so a 64- and 32-bit Python are both seen.
    for view in (getattr(winreg, 'KEY_WOW64_64KEY', 0), getattr(winreg, 'KEY_WOW64_32KEY', 0)):
        try:
            core = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Python\PythonCore',
                0, winreg.KEY_READ | view,
            )
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    tag = winreg.EnumKey(core, i)
                except OSError:
                    break
                i += 1
                try:
                    ip = winreg.OpenKey(core, tag + r'\InstallPath', 0, winreg.KEY_READ | view)
                    try:
                        try:
                            exe, _ = winreg.QueryValueEx(ip, 'ExecutablePath')
                        except OSError:
                            install_dir, _ = winreg.QueryValueEx(ip, None)
                            exe = os.path.join(install_dir, 'python.exe')
                        if exe:
                            found.append(Candidate(exe, 'registry'))
                    finally:
                        winreg.CloseKey(ip)
                except OSError:
                    continue
        finally:
            winreg.CloseKey(core)
    return found


def _win_candidates_from_py_launcher() -> List[Candidate]:
    """Interpreters reported by the `py -0p` launcher.

    We take only the interpreter paths it lists and filter each with
    is_brain_runnable() below — we do not trust the launcher's own location.
    """
    found: List[Candidate] = []
    try:
        proc = subprocess.run(['py', '-0p'], capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.SubprocessError):
        return found
    if proc.returncode != 0:
        return found
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Lines look like: "-V:3.12 *        C:\Program Files\Python312\python.exe"
        idx = line.lower().rfind('python.exe')
        if idx == -1:
            continue
        # Take the last whitespace-split token that ends in python.exe / an abs path.
        token = line.split()[-1]
        if token.lower().endswith('python.exe') and os.path.isabs(token):
            found.append(Candidate(token, 'py-launcher'))
    return found


def _win_candidates_from_program_files() -> List[Candidate]:
    found: List[Candidate] = []
    roots = [
        os.environ.get('ProgramFiles', r'C:\Program Files'),
        os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
        os.environ.get('SystemDrive', 'C:') + os.sep,  # C:\Python3xx installs
    ]
    for root in roots:
        for pat in ('Python3*', 'Python 3*'):
            for d in glob.glob(os.path.join(root, pat)):
                exe = os.path.join(d, 'python.exe')
                if os.path.isfile(exe):
                    found.append(Candidate(exe, 'program-files'))
    return found


def _unix_candidates() -> List[Candidate]:
    found: List[Candidate] = []
    seen = set()
    for name in ('python3', 'python'):
        try:
            proc = subprocess.run(['which', '-a', name], capture_output=True, text=True, timeout=10)
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
        for line in proc.stdout.splitlines():
            path = line.strip()
            if path and path not in seen:
                seen.add(path)
                found.append(Candidate(path, 'path'))
    return found


def _discover_candidates() -> List[Candidate]:
    if platform.system() == 'Windows':
        cands = (
            _win_candidates_from_registry()
            + _win_candidates_from_py_launcher()
            + _win_candidates_from_program_files()
        )
    else:
        cands = _unix_candidates()
    # De-dupe by normalised path, keeping the first (highest-priority) source.
    seen = set()
    unique: List[Candidate] = []
    for c in cands:
        key = os.path.normcase(os.path.normpath(c.path))
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


# ---------------------------------------------------------------------------
# Verification (execute the candidate to learn its real version)
# ---------------------------------------------------------------------------

def _probe_version(interpreter_path: str) -> Optional[Tuple[int, int, int]]:
    """Run the interpreter and return its (major, minor, micro), or None if it won't run."""
    if not os.path.isfile(interpreter_path):
        return None
    try:
        proc = subprocess.run(
            [interpreter_path, '-c',
             "import sys;print('%d %d %d' % sys.version_info[:3])"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        major, minor, micro = (int(x) for x in proc.stdout.split())
        return (major, minor, micro)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Public resolution API
# ---------------------------------------------------------------------------

def _evaluate(min_version: Tuple[int, int]):
    """Return (resolved_best, rejections) for reporting.

    rejections is a list of (path, source, reason) for the `list` command.
    """
    resolved: List[ResolvedPython] = []
    rejections: List[Tuple[str, str, str]] = []

    for cand in _discover_candidates():
        if not is_brain_runnable(cand.path):
            rejections.append((cand.path, cand.source, 'not machine-wide (per-user / Store)'))
            continue
        ver = _probe_version(cand.path)
        if ver is None:
            rejections.append((cand.path, cand.source, 'did not execute'))
            continue
        if ver[:2] < tuple(min_version):
            rejections.append((cand.path, cand.source,
                               f'version {ver[0]}.{ver[1]}.{ver[2]} < min {min_version[0]}.{min_version[1]}'))
            continue
        resolved.append(ResolvedPython(os.path.normpath(cand.path), ver, cand.source))

    # Best = highest version; ties broken by source priority (discovery order).
    resolved.sort(key=lambda rp: rp.version, reverse=True)
    return resolved, rejections


def resolve_brain_python(min_version: Tuple[int, int] = MIN_VERSION_DEFAULT) -> Optional[ResolvedPython]:
    """Resolve the best brain-runnable, machine-wide Python, or None if none exists."""
    resolved, _ = _evaluate(min_version)
    return resolved[0] if resolved else None


# ---------------------------------------------------------------------------
# Guidance shown when nothing brain-runnable is found
# ---------------------------------------------------------------------------

_NO_PYTHON_GUIDANCE = (
    '[ERROR] No brain-runnable (machine-wide) Python found on this host.\n'
    '  A brain service account cannot use a per-user Microsoft Store Python or an\n'
    '  interpreter under another user\'s profile. Install an all-users Python:\n'
    '    winget install Python.Python.3.12 --scope machine\n'
    '  or the python.org "Install for all users" installer, then re-run this preflight.'
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_resolve(min_version: Tuple[int, int], as_json: bool) -> int:
    rp = resolve_brain_python(min_version)
    if rp is None:
        if as_json:
            print(json.dumps({'resolved': None}))
        else:
            print(_NO_PYTHON_GUIDANCE, file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps({
            'resolved': {
                'path': rp.path,
                'version': '%d.%d.%d' % rp.version,
                'source': rp.source,
            }
        }))
    else:
        print(rp.path)
    return 0


def cmd_preflight(min_version: Tuple[int, int]) -> int:
    rp = resolve_brain_python(min_version)
    if rp is None:
        print(_NO_PYTHON_GUIDANCE, file=sys.stderr)
        return 2
    print(f'[OK] Brain-runnable Python found: {rp.path} '
          f'(v{rp.version[0]}.{rp.version[1]}.{rp.version[2]}, via {rp.source})')
    return 0


def cmd_list(min_version: Tuple[int, int]) -> int:
    resolved, rejections = _evaluate(min_version)
    print(f'Brain-runnable Python candidates (min {min_version[0]}.{min_version[1]}, '
          f'platform {platform.system()}):')
    if resolved:
        print('  KEPT:')
        for rp in resolved:
            print(f'    [OK]  {rp.path}  v{rp.version[0]}.{rp.version[1]}.{rp.version[2]}  ({rp.source})')
    else:
        print('  KEPT: (none)')
    if rejections:
        print('  REJECTED:')
        for path, source, reason in rejections:
            print(f'    [--]  {path}  ({source}) — {reason}')
    return 0 if resolved else 2


def _parse_min(value: str) -> Tuple[int, int]:
    try:
        major, minor = value.split('.')
        return (int(major), int(minor))
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError(f'--min must be MAJOR.MINOR (e.g. 3.9), got {value!r}')


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Resolve a brain-runnable (machine-wide) Python interpreter.',
    )
    parser.add_argument('command', choices=('resolve', 'preflight', 'list'),
                        help='resolve: print best path; preflight: OK/fail for installer; '
                             'list: show all candidates and reasons')
    parser.add_argument('--min', type=_parse_min, default=MIN_VERSION_DEFAULT,
                        metavar='X.Y', help='minimum version (default 3.9)')
    parser.add_argument('--json', action='store_true', help='machine-readable output (resolve only)')
    args = parser.parse_args()

    if args.command == 'resolve':
        sys.exit(cmd_resolve(args.min, args.json))
    elif args.command == 'preflight':
        sys.exit(cmd_preflight(args.min))
    elif args.command == 'list':
        sys.exit(cmd_list(args.min))


if __name__ == '__main__':
    main()
