#!/usr/bin/env python3
"""
test_gateway_inside.py — probe the sealed INTERIOR network paths of the brain.

The twin of test_gateway_outside.py. That one asks "does an external consumer get served
through the published gateway?"; this one asks "inside the box, who can reach whom?" — the
interior blind spot ADR 0015 cares about. It reports **reachability as fact** (no policy
baked in): a response = REACHABLE (PASS); a transport/timeout = UNREACHABLE (FAIL). You read
the matrix and decide what SHOULD be open.

Run it in-distro via the seam:
    wsl_scripts.py run test_gateway_inside.py -- --direction:brain-chroma
    wsl_scripts.py run test_gateway_inside.py -- --direction:ollama-world -d https://registry.ollama.ai

  --direction:X   one of:
     brain-chroma   a container on brain_net  ->  chroma:8000     (default /api/v2/heartbeat, +token)
     brain-ollama   a container on brain_net  ->  ollama:11434    (default /api/version)
     brain-world    a container on brain_net  ->  the world       (default https://github.com)
     chroma-world   FROM chroma's netns       ->  the world       (egress as chroma)
     ollama-world   FROM ollama's netns       ->  the world       (egress as ollama)
  -d PATH_OR_URL  the endpoint to test. A bare path ("/api/tags") is appended to the service
                  base; a full http(s):// URL is used as-is. Default: the service heartbeat
                  (service directions) / https://github.com (world directions).
  -h -? --h --? --help   this help

Mechanism: a throwaway `python:3.12-slim` probe container attached to the right network —
`--network <brain>_net` for brain-*, `--network container:<brain>-<svc>` to test egress
*as* that service. No tools need exist inside the chroma/ollama images. HTTPS is probed with
verification OFF on purpose: this measures reachability, not certificate trust.
"""
import os
import subprocess
import sys
from pathlib import Path

HELP_FLAGS = {"-h", "--help", "-?", "--?", "--h", "help", "/?"}
PROBE_IMAGE = "python:3.12-slim"

# The probe: GET the URL (optional bearer), print one line, exit 0 on any HTTP response.
PROBE_SRC = (
    "import os,ssl,urllib.request,urllib.error,sys\n"
    "u=os.environ['PROBE_URL']; b=os.environ.get('PROBE_BEARER','')\n"
    "h={'Authorization':'Bearer '+b} if b else {}\n"
    "ctx=ssl._create_unverified_context()\n"
    "req=urllib.request.Request(u,headers=h)\n"
    "try:\n"
    "    r=urllib.request.urlopen(req,timeout=8,context=ctx)\n"
    "    print('HTTP %d %s' % (r.status, r.read(160).decode('utf-8','replace').replace(chr(10),' ')))\n"
    "except urllib.error.HTTPError as e:\n"
    "    print('HTTP %d %s' % (e.code, e.read(160).decode('utf-8','replace').replace(chr(10),' ')))\n"
    "except Exception as e:\n"
    "    print('ERR %s: %s' % (type(e).__name__, e)); sys.exit(1)\n"
)


def _brain() -> str:
    return Path.home().name


def _chroma_token() -> str:
    env = Path.home() / "docker" / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("CHROMA_MASTER_TOKEN_FOR_GW="):
                return line.split("=", 1)[1].strip()
    return ""


def directions(b: str):
    net = f"{b}_net"
    return {
        "brain-chroma": dict(net=f"--network={net}", base="http://chroma:8000",
                             path="/api/v2/heartbeat", bearer=_chroma_token()),
        "brain-ollama": dict(net=f"--network={net}", base="http://ollama:11434",
                             path="/api/version", bearer=""),
        "brain-world":  dict(net=f"--network={net}", base=None,
                             path="https://github.com", bearer=""),
        "chroma-world": dict(net=f"--network=container:{b}-chroma", base=None,
                             path="https://github.com", bearer=""),
        "ollama-world": dict(net=f"--network=container:{b}-ollama", base=None,
                             path="https://github.com", bearer=""),
    }


def _get(argv, name, default=None):
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    norm = []
    for a in raw:  # fold `--opt:value` -> `--opt=value`
        if a.startswith("--") and ":" in a and "=" not in a:
            head, _, tail = a.partition(":")
            norm.append(f"{head}={tail}")
        else:
            norm.append(a)
    argv = norm

    if not argv or any(a in HELP_FLAGS for a in argv):
        print(__doc__)
        return 0

    b = _brain()
    dirs = directions(b)
    d = _get(argv, "--direction")
    if d not in dirs:
        print(f"[usage] --direction must be one of: {', '.join(dirs)}", file=sys.stderr)
        return 2
    spec = dirs[d]
    endpoint = _get(argv, "-d") or _get(argv, "--dir")

    if spec["base"] is None:                        # world direction
        url = endpoint or spec["path"]
    elif endpoint and endpoint.startswith("http"):   # explicit full URL
        url = endpoint
    else:                                            # path on the service base
        url = spec["base"] + (endpoint or spec["path"])

    print(f"[inside] {d}  ->  GET {url}")
    print(f"         probe: {PROBE_IMAGE} on {spec['net']}"
          + ("  (+bearer)" if spec["bearer"] else ""))

    cmd = ["docker", "run", "--rm", spec["net"],
           "-e", f"PROBE_URL={url}", "-e", f"PROBE_BEARER={spec['bearer']}",
           PROBE_IMAGE, "python", "-c", PROBE_SRC]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).strip()
    reachable = out.startswith("HTTP")
    print(f"  {'PASS (REACHABLE)' if reachable else 'FAIL (UNREACHABLE)'}  {out[:240]}")
    return 0 if reachable else 1


if __name__ == "__main__":
    raise SystemExit(main())
