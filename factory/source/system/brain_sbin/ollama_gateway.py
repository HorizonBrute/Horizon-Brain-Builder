#!/usr/bin/env python3
"""
ollama_gateway.py - render the gateway's Ollama exposure config (brain_sbin).
=============================================================================

ADR projects/2ndbraindevelopment/decisions/0009-ollama-sealed-model-serving-and-gateway-exposure.md
(exposure) + 0011-compose-profile-brain-composition.md (the whole thing is opt-in).

Ollama is a SEALED brain_net container with ZERO native auth. When -
and ONLY when - it is exposed off-box, the ONE nginx gateway must supply the entire
auth + role layer by an endpoint allow-list. This tool renders that config, `ollama.conf`,
from three knobs:

    OLLAMA_EXPOSE   off (default) | on      - is a :11434 listener rendered at all?
    OLLAMA_GW_TLS   off | enforced (default) - plain http, or https reusing the gateway cert
    OLLAMA_GW_AUTHZ open | token | token-role (default) - the admission model

SEALED (OLLAMA_EXPOSE=off) -> ollama.conf is EMPTY (a comment). The base nginx template
includes it via a GLOB (`include /etc/nginx/ollama.d/*.conf;`), which is a no-op when
nothing is mounted there - so the default brain is byte-identical to a chroma-only gateway
and an ollama-off brain can never fail nginx startup on an unresolvable `ollama` upstream.

EXPOSED -> ollama.conf carries: the `ollama_backend` upstream, PATH-based inference (use)
and management (admin) allow-lists (default-deny), the `$is_ollama_use`/`$is_ollama_admin`
token gates (from the generated maps), a default-deny admission map, and the
`server { listen 11434 }` block. Management endpoints (pull/create/copy/push/delete/blobs)
are NEVER reachable by a use token.

This file is rendered host-side into brain_etc/gateway/ollama.conf and synced to
~/docker/nginx/ollama.conf; the exposure compose override mounts it into
/etc/nginx/ollama.d/ and publishes 11434. Everything ASCII (cp1252-safe console).
"""
import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRAIN_DIR = HERE.parent.parent

# Inference (use-role) routes - anchored, PATH-only (method-agnostic: inference and
# management both POST). Default-deny; a route not listed fails closed.
INFERENCE_ROUTES = [
    "/api/embeddings", "/api/embed", "/api/generate", "/api/chat",
    "/api/tags", "/api/show", "/api/ps", "/api/version",
]
# Management (admin-role) routes. A use token is NEVER admitted here.
MGMT_ROUTES = ["/api/pull", "/api/create", "/api/copy", "/api/push", "/api/delete"]


def die(m): print(f"[ollama_gateway] ERROR: {m}", file=sys.stderr); sys.exit(1)
def info(m): print(f"[ollama_gateway] {m}")


def gateway_dir(brain_dir=BRAIN_DIR):
    return Path(brain_dir) / "brain_etc" / "gateway"


def _route_map(var, routes, extra=()):
    lines = [f"    map $uri ${var} {{", "        default 0;"]
    for r in routes:
        lines.append(f'        "~^{r}/?$" 1;')
    for pat in extra:
        lines.append(f'        "{pat}" 1;')
    lines.append("    }")
    return "\n".join(lines)


def render_sealed():
    return ("# ollama.conf - SEALED (OLLAMA_EXPOSE=off). Intentionally empty: no :11434\n"
            "# listener, no ollama upstream. Rendered by ollama_gateway.py. When exposed,\n"
            "# this file carries the full role-gated 11434 server.\n")


def render_exposed(tls, authz, burst_perip=30, burst_pertoken=60,
                   inspect_block="", inspect_filter=""):
    ssl = (tls == "enforced")
    listen = "    listen 11434 ssl;\n    http2 on;" if ssl else "    listen 11434;"
    ssl_block = ("""
    ssl_certificate     /etc/nginx/certs/cert.pem;
    ssl_certificate_key /etc/nginx/certs/cert.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;""" if ssl else "")

    # Admission by authz mode. Key = inference|mgmt|use|admin (4 digits).
    #   token-role (default): inference needs use|admin; mgmt needs admin (never use).
    #   token: any valid token (use OR admin) may do inference; mgmt still admin-only.
    #   open: no token required for inference; mgmt STILL admin-only (destructive).
    if authz == "open":
        admission = (
            '    map "$is_ollama_inference$is_ollama_mgmt$is_ollama_use$is_ollama_admin" $ollama_allowed {\n'
            "        default 0;\n"
            '        "~^10..$" 1;   # inference: open to anyone\n'
            '        "~^01.1$" 1;   # management: admin token only\n'
            "    }")
    elif authz == "token":
        admission = (
            '    map "$is_ollama_inference$is_ollama_mgmt$is_ollama_use$is_ollama_admin" $ollama_allowed {\n'
            "        default 0;\n"
            '        "~^10(10|01|11)$" 1;   # inference: any valid token (use or admin)\n'
            '        "~^01.1$" 1;           # management: admin token only\n'
            "    }")
    else:  # token-role (default)
        admission = (
            '    map "$is_ollama_inference$is_ollama_mgmt$is_ollama_use$is_ollama_admin" $ollama_allowed {\n'
            "        default 0;\n"
            '        "1010" 1;   # inference + use\n'
            '        "1001" 1;   # inference + admin\n'
            '        "1011" 1;   # inference + use&admin\n'
            '        "0101" 1;   # management + admin\n'
            '        "0111" 1;   # management + use&admin\n'
            "        # 0110 (management + use only) intentionally absent -> DENY\n"
            "    }")

    v1_extra = ('~^/v1/.*$',)   # OpenAI-compatible scaffold routes are inference
    return f"""# ollama.conf - EXPOSED (OLLAMA_EXPOSE=on, tls={tls}, authz={authz}).
# GENERATED by ollama_gateway.py. The ONE gateway supplies the auth/roles Ollama lacks.
# Included by the base nginx.conf via `include /etc/nginx/ollama.d/*.conf;`.

    upstream ollama_backend {{
        server ollama-svc:11434;
        keepalive 8;
    }}

    # -- inference (use) allow-list - default-deny, PATH-matched --------
{_route_map("is_ollama_inference", INFERENCE_ROUTES, v1_extra)}

    # -- management (admin) allow-list - a use token is NEVER admitted here -----------
{_route_map("is_ollama_mgmt", MGMT_ROUTES, ('~^/api/blobs/.*$',))}

    # -- token gates (generated from token_registry by gateway_tokens.py) ---
    map $http_authorization $is_ollama_use {{
        default 0;
        include /etc/nginx/ollama_use.map;
    }}
    map $http_authorization $is_ollama_admin {{
        default 0;
        include /etc/nginx/ollama_admin.map;
    }}

    # -- admission (authz={authz}) - default-deny -------------------------------------
{admission}

    server {{
{listen}
        server_name _;{ssl_block}
{inspect_block}
        # Uniform JSON errors - no server identity leak (mirrors the chroma server).
        proxy_intercept_errors on;
        error_page 400 @oe400; error_page 403 @oe403; error_page 429 @oe429;
        error_page 500 501 502 503 504 @oe5xx;
        location @oe400 {{ default_type application/json; return 400 '{{"status":400,"message":"bad request"}}'; }}
        location @oe403 {{ default_type application/json; return 403 '{{"status":403,"message":"forbidden"}}'; }}
        location @oe429 {{ default_type application/json; return 429 '{{"status":429,"message":"too many requests"}}'; }}
        location @oe5xx {{ default_type application/json; return 502 '{{"status":502,"message":"upstream error"}}'; }}

        location / {{
            # Ollama generation is compute-DoS-prone - rate-limit hard.
            # Burst sizes come from gateway.conf [ratelimit] ollama_burst_* knobs.
            limit_req zone=perip    burst={burst_perip} nodelay;
            limit_req zone=pertoken burst={burst_pertoken} nodelay;
            proxy_hide_header Server;
            if ($ollama_allowed = 0) {{ return 403; }}
{inspect_filter}
            # Ollama has NO auth: nothing to inject upstream. Strip the client's gateway
            # token so it never reaches Ollama (which would ignore it anyway).
            proxy_set_header Authorization "";
            proxy_http_version 1.1;
            proxy_set_header Host       $host;
            proxy_set_header Connection "";
            proxy_pass http://ollama_backend;
        }}
    }}
"""


def render(expose, tls, authz, burst_perip=30, burst_pertoken=60,
           inspect_block="", inspect_filter=""):
    """Render ollama.conf. inspect_block/inspect_filter (ADR 0015 Phase 2) stamp the
    ollama-EXTERNAL surface's access_log + optional njs body filter; gateway_config.py
    computes and passes them. Rendered standalone (this module's CLI), they default empty -
    regenerate via gateway_config.py for the full inspection wiring."""
    if expose != "on":
        return render_sealed()
    if tls not in ("off", "enforced"):
        die(f"OLLAMA_GW_TLS must be off|enforced, got {tls!r}")
    if authz not in ("open", "token", "token-role"):
        die(f"OLLAMA_GW_AUTHZ must be open|token|token-role, got {authz!r}")
    return render_exposed(tls, authz, burst_perip, burst_pertoken,
                          inspect_block, inspect_filter)


def _env_default(brain_dir, key, fallback):
    """Read KEY from the stack posture env if present, else fallback. The seam source is
    brain_etc/brain.env (synced to ~/docker/.env). See brain_truths.py SEAM."""
    envp = Path(brain_dir) / "brain_etc" / "brain.env"
    if envp.is_file():
        for line in envp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return os.environ.get(key, fallback)


def cmd_render(args):
    bd = Path(args.brain_dir) if args.brain_dir else BRAIN_DIR
    expose = args.expose or _env_default(bd, "OLLAMA_EXPOSE", "off")
    tls = args.tls or _env_default(bd, "OLLAMA_GW_TLS", "enforced")
    authz = args.authz or _env_default(bd, "OLLAMA_GW_AUTHZ", "token-role")
    text = render(expose, tls, authz)
    out = gateway_dir(bd) / "nginx_auto_gen" / "ollama.conf"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".conf.tmp")
    tmp.write_bytes(text.encode("utf-8"))          # LF bytes, never CRLF
    os.replace(tmp, out)
    state = "SEALED (empty)" if expose != "on" else f"EXPOSED tls={tls} authz={authz}"
    info(f"rendered {out}  ->  {state}")
    if args.stdout:
        print(text)
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Render the gateway's Ollama exposure config (ollama.conf).")
    ap.add_argument("--brain-dir", help="brain root (default: this tool's brain)")
    ap.add_argument("--expose", choices=("off", "on"), help="override OLLAMA_EXPOSE")
    ap.add_argument("--tls", choices=("off", "enforced"), help="override OLLAMA_GW_TLS")
    ap.add_argument("--authz", choices=("open", "token", "token-role"), help="override OLLAMA_GW_AUTHZ")
    ap.add_argument("--stdout", action="store_true", help="also print the rendered config")
    ap.set_defaults(func=cmd_render)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
