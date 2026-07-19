#!/usr/bin/env python3
"""harden_nginx.py — idempotently add gateway hardening to an nginx.conf.template.

Applies (all mode-agnostic — works on the mode-B canon and the mode-C live template):
  * server_tokens off            — strip version from Server header + ALL error pages
  * limit_req_zone/limit_req     — per-IP + per-token rate buckets, 429 on breach
  * proxy_intercept_errors + JSON error_pages — no nginx/backend identity or detail in bodies
  * proxy_hide_header Server      — strip Chroma/uvicorn Server header on proxied 200s

Idempotent: re-running is a no-op. Anchors are asserted so it fails loud if the
template shape ever changes. Usage: harden_nginx.py <path-to-nginx.conf.template>
"""
import sys

A_HTTP = "    default_type  application/octet-stream;\n"
A_SRV  = "        server_name _;\n"
A_LOC  = "        location / {\n"

INS_HTTP = A_HTTP + """
    # -- Hardening: never advertise server name/version on ANY response --------
    # Strips the version from the Server header AND every generated error page
    # (403/400/405/501/weird-method probes) — closes version/method recon.
    server_tokens off;

    # -- Rate limiting (defense-in-depth) -------------------------------------
    # Per-client-IP and per-bearer-token leaky buckets; breach -> 429 (JSON below).
    limit_req_zone $binary_remote_addr  zone=perip:10m    rate=30r/s;
    limit_req_zone $http_authorization  zone=pertoken:10m rate=60r/s;
    limit_req_status 429;
"""

INS_SRV = A_SRV + """
        # -- Uniform JSON errors — no nginx/backend identity or detail leaks ----
        # Catches gateway-generated errors (403 authz gate, 400/405/413/429) AND
        # intercepted upstream errors. NOTE: 422 (Chroma/FastAPI validation) is
        # deliberately NOT intercepted — legit clients need that field detail.
        proxy_intercept_errors on;
        error_page 400 @e400;
        error_page 401 @e401;
        error_page 403 @e403;
        error_page 404 @e404;
        error_page 405 @e405;
        error_page 413 @e413;
        error_page 429 @e429;
        error_page 500 501 502 503 504 @e5xx;
        location @e400 { default_type application/json; return 400 '{"status":400,"message":"bad request"}'; }
        location @e401 { default_type application/json; return 401 '{"status":401,"message":"unauthorized"}'; }
        location @e403 { default_type application/json; return 403 '{"status":403,"message":"forbidden"}'; }
        location @e404 { default_type application/json; return 404 '{"status":404,"message":"not found"}'; }
        location @e405 { default_type application/json; return 405 '{"status":405,"message":"method not allowed"}'; }
        location @e413 { default_type application/json; return 413 '{"status":413,"message":"payload too large"}'; }
        location @e429 { default_type application/json; return 429 '{"status":429,"message":"too many requests"}'; }
        location @e5xx { default_type application/json; return 502 '{"status":502,"message":"upstream error"}'; }
"""

INS_LOC = A_LOC + """            # -- Rate limits + strip upstream identity --------------------------
            limit_req zone=perip    burst=60  nodelay;
            limit_req zone=pertoken burst=120 nodelay;
            proxy_hide_header Server;    # drop Chroma/uvicorn Server header on proxied 200s
"""


def main():
    path = sys.argv[1]
    s = open(path, encoding="utf-8").read()
    for anchor in (A_HTTP, A_SRV, A_LOC):
        assert anchor in s, f"anchor not found (template shape changed?): {anchor!r}"
    done = []
    if "server_tokens" not in s:
        s = s.replace(A_HTTP, INS_HTTP, 1); done.append("http:server_tokens+ratezones")
    if "@e403" not in s:
        s = s.replace(A_SRV, INS_SRV, 1); done.append("server:json_errors+intercept")
    if "proxy_hide_header" not in s:
        s = s.replace(A_LOC, INS_LOC, 1); done.append("location:limit_req+hide_header")
    if done:
        open(path, "w", encoding="utf-8", newline="\n").write(s)
        print(f"HARDENED {path}: {', '.join(done)}")
    else:
        print(f"NOOP (already hardened) {path}")


if __name__ == "__main__":
    main()
