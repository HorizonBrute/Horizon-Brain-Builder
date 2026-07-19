#!/usr/bin/env python3
"""
test_gateway_outside.py — prove the gateway from OUTSIDE (an external consumer).

A legit HTTPS call through the brain's published gateway, from off-box (the host /
workstation perspective), with a real bearer token — pass/fail on the response. This is
the "does a consumer out in the world actually get served?" check, the twin of
test_gateway_inside.py (which tests the sealed interior network paths).

    test_gateway_outside.py --chroma -t env:SORCERYPUNK_DEV_CHROMA_R
    test_gateway_outside.py --ollama -t @/path/to/token --host 192.168.1.10
    test_gateway_outside.py --chroma -t <raw-token> -k

  SERVICE (pick one; extensible — add a row to SERVICES):
    --chroma            chroma gateway  (default :8000, GET /api/v2/heartbeat)
    --ollama            ollama gateway  (default :11434, GET /api/version)

  TOKEN  -t / --token VALUE   resolved three ways:
    @FILE  or an existing path   -> read the token from that file (first line)
    env:NAME  or  $NAME          -> read it from environment variable NAME
    anything else                -> the raw token itself

  Other:
    --host H        gateway host (default 127.0.0.1)
    --port P        override the service's default port
    --path P        override the endpoint path
    --cacert FILE   CA bundle to verify TLS (default: this brain's gateway cert if found)
    -k/--insecure   skip TLS verification (self-signed without the CA on hand)
    --timeout N     seconds (default 10)
    -h -? --h --? --help   this help

Exit code: 0 = PASS (2xx), 1 = FAIL (non-2xx or transport error), 2 = usage error.
Stdlib only — no third-party deps, so it runs anywhere python does.
"""
import os
import ssl
import sys
import urllib.request
import urllib.error
from pathlib import Path

# The service registry: default port + probe endpoint per published service. Add a row
# to extend ("whatever drops in") — nothing else in the tool is service-specific.
SERVICES = {
    "chroma": {"port": 8000,  "path": "/api/v2/heartbeat"},
    "ollama": {"port": 11434, "path": "/api/version"},
}

HELP_FLAGS = {"-h", "--help", "-?", "--?", "--h", "help", "/?"}


def _brain_dir() -> Path:
    # system/brain_sbin/test_gateway_outside.py -> brain root is three parents up.
    return Path(__file__).resolve().parent.parent.parent


def _default_cacert() -> str | None:
    # The gateway's self-signed cert lives on the config seam at brain_etc/tls/cert.pem.
    c = _brain_dir() / "brain_etc" / "tls" / "cert.pem"
    return str(c) if c.is_file() else None


def resolve_token(spec: str) -> str:
    """@file / existing-path -> file contents; env:NAME / $NAME -> env; else raw."""
    if spec.startswith("@"):
        return Path(spec[1:]).read_text(encoding="utf-8").strip()
    if spec.startswith("env:"):
        name = spec[4:]
        val = os.environ.get(name)
        if val is None:
            sys.exit(f"[usage] env var not set: {name}")
        return val.strip()
    if spec.startswith("$"):
        val = os.environ.get(spec[1:])
        if val is None:
            sys.exit(f"[usage] env var not set: {spec[1:]}")
        return val.strip()
    p = Path(spec)
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return spec.strip()


def _get(argv, name, default=None):
    """Pull `--name value` (or `--name=value`) out of a normalized argv list."""
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # Normalize the user's `--opt:value` colon form into `--opt=value` for uniform parsing
    # (fold only the FIRST colon after a long-opt name, so a value like env:NAME survives).
    norm = []
    for a in raw:
        if a.startswith("--") and ":" in a and "=" not in a:
            head, _, tail = a.partition(":")
            norm.append(f"{head}={tail}")
        else:
            norm.append(a)
    argv = norm

    if not argv or any(a in HELP_FLAGS for a in argv):
        print(__doc__)
        return 0

    # Which service?
    chosen = [s for s in SERVICES if f"--{s}" in argv]
    if len(chosen) != 1:
        print("[usage] pick exactly one service flag, e.g. --chroma or --ollama "
              f"(known: {', '.join('--' + s for s in SERVICES)})", file=sys.stderr)
        return 2
    svc = chosen[0]
    reg = SERVICES[svc]

    host = _get(argv, "--host", "127.0.0.1")
    port = _get(argv, "--port", str(reg["port"]))
    path = _get(argv, "--path", reg["path"])
    timeout = float(_get(argv, "--timeout", "10"))
    insecure = ("-k" in argv) or ("--insecure" in argv)
    cacert = _get(argv, "--cacert", _default_cacert())

    token_spec = _get(argv, "--token") or _get(argv, "--t") or _get(argv, "-t")
    if not token_spec:
        print("[usage] a token is required: -t @file | -t env:NAME | -t <raw>", file=sys.stderr)
        return 2
    token = resolve_token(token_spec)

    url = f"https://{host}:{port}{path}"
    if insecure:
        ctx = ssl._create_unverified_context()
        tls_note = "TLS verify OFF (-k)"
    else:
        ctx = ssl.create_default_context(cafile=cacert) if cacert else ssl.create_default_context()
        tls_note = f"TLS verify via {cacert}" if cacert else "TLS verify via system CAs"

    print(f"[outside] {svc} gateway  ->  GET {url}")
    print(f"          {tls_note}; bearer len={len(token)}")

    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            code, body = r.status, r.read(400).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read(400).decode("utf-8", "replace")
    except Exception as e:  # transport / TLS / DNS
        print(f"  FAIL  transport error: {type(e).__name__}: {e}")
        return 1

    ok = 200 <= code < 300
    print(f"  {'PASS' if ok else 'FAIL'}  http={code}  body={body.strip()[:200]!r}")
    if code == 403:
        print("        (403 = gateway up + auth enforced, but this token was rejected for this path)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
