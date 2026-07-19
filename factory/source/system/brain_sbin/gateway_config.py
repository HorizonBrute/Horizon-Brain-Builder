#!/usr/bin/env python3
r"""
gateway_config.py - render ALL gateway BACKEND config from the human knob files.
================================================================================

The gateway has exactly TWO human-facing surfaces an admin edits:

    brain_etc/brain.env            - stack POSTURE  (what runs / what's exposed /
                                     TLS / authz mode / bind / port / CHROMA_MASTER_TOKEN_FOR_GW)
    brain_etc/gateway/gateway.conf - gateway TUNING ([ratelimit] + [fail2ban])
    brain_etc/gateway/token_registry - the bearer tokens

Everything the running stack actually consumes is GENERATED from those three, into
sibling *backend* folders that are never hand-edited:

    gateway/nginx_auto_gen/     nginx.conf.template, ratelimit.conf, ollama.conf
    gateway/token_maps_auto_gen/ reader/writer/ollama_use/ollama_admin .map  (via gateway_tokens)
    gateway/fail2ban_autoconfigs/ jail.d/gateway.conf, filter.d/nginx-gateway.conf,
                                  action.d/seam-banlist.conf

This is the "complexity on the backend" split: the admin turns simple
knobs; this tool renders the raw nginx / fail2ban syntax. Run it whenever brain.env
or gateway.conf changes (the gateway apply calls it), then recreate the gateway.

Everything is written as LF bytes (a stray CR has bitten this seam twice - obj 008)
and ASCII (cp1252-safe console).
"""
import argparse
import configparser
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
BRAIN_DIR = HERE.parent.parent

import gateway_tokens          # reuse the token registry -> maps generator
import ollama_gateway          # reuse the ollama.conf renderer
import neuron_topology         # the per-bundle neuron-net addressing scheme (ADR-0015)
import brain_env               # the two-zone brain.env loader + the flat-zone .env render
import neuron_compose          # renders the DEFAULT bundle's compose blocks from the brain.env zone
import add_neuron_bundle       # renders the ADDITIONAL bundles (shared renderer) — same materialize step


def die(m): print(f"[gateway_config] ERROR: {m}", file=sys.stderr); sys.exit(1)
def info(m): print(f"[gateway_config] {m}")


def gateway_dir(bd=BRAIN_DIR):   return Path(bd) / "brain_etc" / "gateway"
def brain_env_path(bd=BRAIN_DIR): return Path(bd) / "brain_etc" / "brain.env"
def gateway_conf_path(bd=BRAIN_DIR): return gateway_dir(bd) / "gateway.conf"


# --------------------------------------------------------------------------- #
# Exposure model (the new two-zone control panel). A gateway surface's EXTERNAL listener is
# rendered when the gateway publishes host ports (EXTERNAL_GATEWAY_ENABLE=on) AND the surface
# exists (a built-in backend is enabled, or >=1 action neuron serves). Whether that surface
# then reaches the LAN vs loopback is a SEPARATE, firewall/bind concern (BRAIN_POSTURE +
# <SVC>_PUBLISH_TO_LAN), owned by gateway_port.py — NOT by the nginx render here.
# --------------------------------------------------------------------------- #
def gw_publishes(env):
    """The master host-publish switch: does the gateway bind ANY host port? OFF = fully sealed."""
    return env.get("EXTERNAL_GATEWAY_ENABLE", "on").strip().lower() == "on"


def svc_enabled(env, svc):
    """Is a built-in backend (svc = 'CHROMA' | 'OLLAMA') enabled to run at all?"""
    return env.get(f"{svc}_ENABLE", "on").strip().lower() == "on"


def compose_files(env):
    """The compose -f stack for this brain's posture: base + the exposure overlays. THE single
    canonical source of the overlay set (NOTE 001-58): every place that recreates the gateway
    (reapply_brain_configs, gateway_tokens.apply_and_recreate, neuron_schedule, the boot
    keepalive) MUST build its `-f` flags from this list, so no site can recreate the gateway
    base-only and silently unpublish the LAN host ports (chroma 8000 / ollama 11434 / action
    8443). An overlay is layered when the gateway PUBLISHES host ports (EXTERNAL_GATEWAY_ENABLE=on)
    AND the surface exists: a built-in backend that is enabled (CHROMA_ENABLE / OLLAMA_ENABLE), and
    the action overlay whenever the gateway publishes (it publishes any type:action neuron that
    declares ports; a no-op when none do). Returns bare filenames; the caller joins them onto the
    per-brain docker dir. Reuses the gw_publishes / svc_enabled predicates above."""
    files = ["compose.yaml"]
    if not gw_publishes(env):
        return files                                     # fully sealed: base only, no host ports
    if svc_enabled(env, "CHROMA"):
        files.append("compose.chroma-gateway.yaml")
    if svc_enabled(env, "OLLAMA"):
        files.append("compose.ollama-gateway.yaml")
    files.append("compose.action-neuron-gateway.yaml")
    return files


def has_action_neuron(bd):
    """True when the YAML neuron zone declares at least one action neuron (the action surface
    exists). Tolerant: a missing/invalid zone -> no action surface (rendered sealed)."""
    try:
        import brain_env as be
        return any(True for _b, _n in be.iter_neurons(be.load_neurons(brain_env_path(bd)), "action"))
    except Exception:
        return False


def resolve_neuron_tokens(bd=BRAIN_DIR):
    """The NEURON NAMED-TOKEN resolver (config-flow refactor, Phase 3).

    Every neuron in the brain.env YAML zone names its gateway bearer BY NAME (`gateway_token:`);
    nothing is auto-minted. This walks the neuron zone and resolves each name to its Entry in the
    gateway token registry, returning { (bundle, neuron): gateway_tokens.Entry }. It is the single
    place that maps a neuron to its concrete token, so both the generate-time validation here and
    the per-neuron compose injection (Phase 4) go through ONE resolution.

    Fails CLOSED (die) when a neuron names a token that is NOT in the registry - with a copy-paste
    `gateway_tokens.py grant` line to create it. A neuron that names NO token, or names one whose
    grants don't match its type's default scope (input=chroma:writer, action=chroma:reader), is a
    WARNING only: the operator may deliberately break the default posture (unwise, per the spec's
    'you can break this' stance), and a neuron may legitimately reach no token-gated backend."""
    import brain_env as be
    try:
        neurons = be.load_neurons(brain_env_path(bd))
    except be.BrainEnvError as e:
        die(str(e))
    entries = gateway_tokens.read_registry(bd)
    resolved = {}
    for bundle, n in be.iter_neurons(neurons):
        bname, nname, ntype = bundle.get("name"), n.get("name"), n.get("type")
        label = n.get("gateway_token")
        if not label:
            info(f"WARNING: neuron {nname!r} (bundle {bname!r}) names no gateway_token - it will "
                 f"reach no token-gated backend through the gateway. Add `gateway_token: <name>` "
                 f"if it should read/write chroma/ollama.")
            continue
        entry = gateway_tokens.find_by_label(entries, label)
        if entry is None:
            want = gateway_tokens.DEFAULT_NEURON_GRANT.get(ntype, "chroma:reader")
            die(f"neuron {nname!r} (bundle {bname!r}) names gateway_token {label!r}, which does "
                f"not exist in the token registry ({gateway_tokens.registry_path(bd)}).\n"
                f"    Create it with gateway_tokens.py, e.g.:\n"
                f"      python system/brain_sbin/gateway_tokens.py --brain {Path(bd).name} \\\n"
                f"             grant --label {label} --grant {want} --grant ollama:use")
        want = gateway_tokens.DEFAULT_NEURON_GRANT.get(ntype)
        if want and want not in entry.grants:
            info(f"WARNING: {ntype} neuron {nname!r} names token {label!r} whose grants "
                 f"{entry.grants} do not include the default {want!r} - permitted (override), "
                 f"but confirm this is intended.")
        resolved[(bname, nname)] = entry
    return resolved


# --------------------------------------------------------------------------- #
# Input parsing
# --------------------------------------------------------------------------- #
def read_env(path):
    """Parse a KEY=VALUE .env file -> dict (ignore blanks / # comments).

    Marker-aware: brain.env now carries a nested YAML neuron zone BELOW a `===NEURONS===`
    marker line (spec: 001_prototype-brain.env; loader: brain_env.py). This parser reads
    ONLY the flat service panel ABOVE the marker and STOPS there, so every flat-zone consumer
    (this module's generate, reapply, gateway_port) sees the compose environment and never a
    stray YAML line. Plain .env files (chroma.env, ollama.env, the runtime .env) carry no
    marker, so the break never fires for them - harmless."""
    out = {}
    if not Path(path).is_file():
        return out
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.lstrip("#").strip().startswith("===NEURONS==="):
            break                      # flat zone ends here; the rest is the YAML neuron zone
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        # Strip a trailing inline comment (`KEY=val   # note`) - the prototype authors knobs this
        # way. Only when the '#' is preceded by whitespace, so a '#' inside a value (rare here)
        # is preserved. Mirrors brain_env.parse_flat so the two parsers never drift.
        if "#" in v:
            head, _, _tail = v.partition("#")
            if head.rstrip() != head:
                v = head
        out[k.strip()] = v.strip()
    return out


def read_gateway_conf(path):
    """Parse gateway.conf (INI) -> {section: {key: value}}."""
    cp = configparser.ConfigParser()
    if Path(path).is_file():
        cp.read(path, encoding="utf-8")
    return cp


def _write_lf(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(text.encode("ascii"))     # ASCII + LF, never CRLF
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# ratelimit.conf  (from gateway.conf [ratelimit])
# --------------------------------------------------------------------------- #
def render_ratelimit(rl):
    perip = rl.get("perip_rate", "30r/s")
    pertoken = rl.get("pertoken_rate", "60r/s")
    return (
        "# ratelimit.conf - GENERATED by gateway_config.py from gateway.conf [ratelimit].\n"
        "# Do NOT hand-edit. The leaky-bucket zones + sustained rates; a breach -> 429.\n"
        "# Included at http{} by nginx.conf.template. Per-location BURST sizes are rendered\n"
        "# onto the limit_req lines in each server (chroma here, ollama in ollama.conf).\n"
        f"limit_req_zone $binary_remote_addr  zone=perip:10m    rate={perip};\n"
        f"limit_req_zone $http_authorization  zone=pertoken:10m rate={pertoken};\n"
        "limit_req_status 429;\n"
    )


# --------------------------------------------------------------------------- #
# nginx.conf.template  (the chroma core, from brain.env chroma posture + bursts)
# --------------------------------------------------------------------------- #
_NGINX_HEAD = r"""# =============================================================================
# Brain gateway - nginx configuration (BACKEND, GENERATED)
# =============================================================================
# GENERATED by system/brain_sbin/gateway_config.py from brain.env (posture) + gateway.conf
# (tuning). Do NOT hand-edit - change those and re-run the gateway apply. This is the
# SKELETON: shared http{} bits (rate-limit include, JSON log, chroma authz/token maps)
# plus two glob includes for the service listeners - chroma.d/ (chroma.conf) and ollama.d/
# (ollama.conf), each mounted by its exposure overlay. ${CHROMA_MASTER_TOKEN_FOR_GW} is substituted at
# container start by nginx envsubst (NGINX_ENVSUBST_FILTER=CHROMA).
# =============================================================================
@NJS_LOAD@
user  nginx;
worker_processes  auto;
error_log  /var/log/nginx/error.log  warn;
pid        /var/run/nginx.pid;

events {
    worker_connections  1024;
}

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    # Never advertise server name/version on ANY response (version/method recon).
    server_tokens off;

    # Rate-limit zones + sustained rates (gateway.conf [ratelimit]).
    include /etc/nginx/ratelimit.conf;

@LOG_FORMATS@
    sendfile        on;
    keepalive_timeout  65;

    # Chroma payloads (add/query with embeddings) can be large.
    client_max_body_size 100m;

    # ADR 0015 Phase 2 (content capture): buffer the WHOLE request body in memory so
    # $request_body is populated for the inspection log. Sized to client_max_body_size -
    # a body larger than this spills to a temp file and $request_body is then EMPTY (the
    # documented cost of in-flight content capture; raise both in lockstep if payloads grow).
    client_body_buffer_size 100m;
    client_body_in_single_buffer on;
"""

# The chroma authz maps: read allow-list + token gates + $allowed (mode-dependent) +
# $role. Always emitted (keeps the log_format $role/$allowed valid even if chroma is
# not exposed); @ALLOWED_MAP@ is substituted per CHROMA_GW_AUTHZ.
_NGINX_CHROMA_MAPS = r"""
    # -- READ allow-list (METHOD:URI). Default deny; anchored, [^/]+ dynamic segments.
    map $request_method:$request_uri $is_read {
        default 0;
        "~^GET:/api/v2/heartbeat/?(\?.*)?$"                                                          1;
        "~^GET:/api/v2/version/?(\?.*)?$"                                                            1;
        "~^GET:/api/v2/pre-flight-checks/?(\?.*)?$"                                                  1;
        "~^GET:/api/v2/tenants/[^/]+/?(\?.*)?$"                                                      1;
        "~^GET:/api/v2/tenants/[^/]+/databases/?(\?.*)?$"                                            1;
        "~^GET:/api/v2/tenants/[^/]+/databases/[^/]+/?(\?.*)?$"                                      1;
        "~^GET:/api/v2/tenants/[^/]+/databases/[^/]+/collections/?(\?.*)?$"                          1;
        "~^GET:/api/v2/tenants/[^/]+/databases/[^/]+/collections/[^/]+/?(\?.*)?$"                    1;
        "~^GET:/api/v2/tenants/[^/]+/databases/[^/]+/collections/[^/]+/count/?(\?.*)?$"              1;
        "~^POST:/api/v2/tenants/[^/]+/databases/[^/]+/collections/[^/]+/query/?(\?.*)?$"             1;
        "~^POST:/api/v2/tenants/[^/]+/databases/[^/]+/collections/[^/]+/get/?(\?.*)?$"               1;
        "~^GET:/api/v2/auth/identity/?(\?.*)?$"  1;   # chromadb client init (get_user_identity)
    }

    # -- Writer + reader bearer-token gates (generated maps).
    map $http_authorization $is_writer {
        default 0;
        include /etc/nginx/writer_tokens.map;
    }
    map $http_authorization $is_reader {
        default 0;
        include /etc/nginx/reader_tokens.map;
    }

@ALLOWED_MAP@

    # role label for the access log
    map $is_writer $role {
        default "reader";
        "1"     "writer";
    }
"""

# The chroma upstream + TLS server. @LISTEN@ / @SSL_BLOCK@ / @BURST_*@ substituted.
_NGINX_CHROMA_SERVER = r"""
    # -- Upstream: internal Chroma, plain HTTP inside brain_net (TLS is edge-only). Targets
    # the DISTINCT `chroma-svc` alias (not bare `chroma`, which is aliased to the gateway
    # itself on neuron_net -> a proxy loop; see internal.conf / compose chroma-svc).
    upstream chroma_backend {
        server chroma-svc:8000;
        keepalive 16;
    }

    server {
@LISTEN@
        server_name _;
@INSPECT@
        # Uniform JSON errors - no nginx/backend identity or detail leaks. 422
        # (Chroma/FastAPI validation) is deliberately NOT intercepted.
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
@SSL_BLOCK@
        location / {
            limit_req zone=perip    burst=@BURST_PERIP@  nodelay;
            limit_req zone=pertoken burst=@BURST_PERTOKEN@ nodelay;
            proxy_hide_header Server;
            if ($allowed = 0) {
                return 403;
            }
@INSPECT_FILTER@
            # Inject Chroma's service token; strip any client-supplied token.
            proxy_set_header Authorization  "Bearer ${CHROMA_MASTER_TOKEN_FOR_GW}";
            proxy_set_header X-Chroma-Token "";

            proxy_http_version 1.1;
            proxy_set_header Host              $host;
            proxy_set_header X-Real-IP         $remote_addr;
            proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header Connection        "";

            proxy_pass http://chroma_backend;
        }
    }
"""

_NGINX_TAIL = r"""
    # -- Service listeners (symmetric). Each is a glob include that
    # is a no-op when its exposure overlay is not layered (a sealed/off service mounts
    # nothing). chroma.conf -> chroma.d (compose.chroma-gateway.yaml, CHROMA_EXPOSE=on);
    # ollama.conf -> ollama.d (compose.ollama-gateway.yaml, OLLAMA_EXPOSE=on). Both are
    # rendered from the knob files into nginx_auto_gen/. The $role/$allowed maps above stay
    # in this skeleton (nginx validates log_format variables at load, so they must exist
    # even when chroma's server block is not mounted).
    include /etc/nginx/chroma.d/*.conf;
    include /etc/nginx/ollama.d/*.conf;
    # -- Action query-API listener (ADR 0017 sec Next). action.conf -> action.d
    # (compose.action-neuron-gateway.yaml, ACTION_EXPOSE=on): the TLS :8443 server that proxies to the
    # action neuron's HTTP query API on neuron_net. A no-op glob when the API is not exposed.
    include /etc/nginx/action.d/*.conf;

    # -- INTERNAL listeners (ADR 0015 sec 1). ALWAYS rendered + ALWAYS mounted (base gateway,
    # not an exposure overlay): internal traffic is unconditional. internal.conf carries the
    # plain chroma :8000 + ollama :11434 servers the neuron reaches over neuron_net (the
    # backends are unreachable to it except through here). Mounted as a .template so
    # ${CHROMA_MASTER_TOKEN_FOR_GW} is injected upstream by the chroma half; the maps it uses ($allowed,
    # $is_writer above; $is_oint_* self-contained inside internal.conf) are in scope here.
    include /etc/nginx/internal.d/*.conf;
}
"""

_SSL_BLOCK = r"""
        ssl_certificate     /etc/nginx/certs/cert.pem;
        ssl_certificate_key /etc/nginx/certs/cert.key;
        ssl_protocols       TLSv1.2 TLSv1.3;
        ssl_ciphers         HIGH:!aNULL:!MD5;
        ssl_prefer_server_ciphers on;
        ssl_session_cache   shared:SSL:10m;
        ssl_session_timeout 1h;
"""


# --------------------------------------------------------------------------- #
# Inspection / content capture  (ADR 0015 Phase 2)
# --------------------------------------------------------------------------- #
# Four SURFACES (service x direction), each a per-service/per-direction verbosity knob in
# brain.env. Each maps to exactly one generated server block, stamped with an "inspect block"
# (the access_log for its baked per-surface format). The log SCHEMA is uniform across surfaces
# (same JSON keys always present; fields empty below their level), so the blue-team parses ONE
# shape. Level ladder: off (no record) -> basic -> basic+headers -> request (DEFAULT, + request
# body) -> request+response (+ upstream body via njs; the ONLY level that loads njs).
_INSPECT_LEVELS = ("off", "basic", "basic+headers", "request", "request+response")

# surface label  ->  brain.env knob (the *_INSPECT enums). Order is stable for logging.
# `action-external` (ADR 0017 sec Next) is the external QUERY-API surface: client -> gateway :8443
# -> the action neuron's HTTP server. It has no `-internal` twin — the action neuron's OWN calls
# out to chroma/ollama are already captured on the chroma-internal / ollama-internal surfaces.
_SURFACES = {
    "chroma-external": "CHROMA_EXTERNAL_INSPECT",
    "chroma-internal": "CHROMA_INTERNAL_INSPECT",
    "ollama-external": "OLLAMA_EXTERNAL_INSPECT",
    "ollama-internal": "OLLAMA_INTERNAL_INSPECT",
    "action-external": "ACTION_EXTERNAL_INSPECT",
}


def inspect_level(env, key):
    """Read + validate a *_INSPECT knob (default `request`, the high-value/low-cost body)."""
    v = env.get(key, "request").strip()
    if v not in _INSPECT_LEVELS:
        die(f"{key} must be one of {'|'.join(_INSPECT_LEVELS)}, got {v!r}")
    return v


def _surface_levels(env):
    """{surface: level} for all four surfaces."""
    return {s: inspect_level(env, k) for s, k in _SURFACES.items()}


def need_response_capture(env):
    """True iff ANY surface is request+response - the ONLY thing that pulls in njs."""
    return any(lv == "request+response" for lv in _surface_levels(env).values())


# The shared BASIC fields (identical across every format, so fail2ban's remote_addr/status
# anchors never move). $role/$allowed are chroma-derived maps but exist globally (skeleton),
# so they resolve for ollama surfaces too (as reader/deny - pre-existing behavior, kept).
_LOG_BASIC = (
    '"time":"$time_iso8601",'
    '"remote_addr":"$remote_addr",'
    '"host":"$host",'
    '"method":"$request_method",'
    '"uri":"$uri",'
    '"status":$status,'
    '"body_bytes_sent":$body_bytes_sent,'
    '"request_time":$request_time,'
    '"role":"$role",'
    '"allowed":"$allowed",'
)
# Curated request headers (NEVER Authorization - tokens must never land in the log; that is
# the standing rule the metadata log already follows). Present as a nested object; `{}` at
# levels below basic+headers so the KEY is always present (uniform schema).
_LOG_HEADERS = (
    '"req_headers":{'
      # X-Neuron-* attribution, stamped by input AND action neurons on every internal hop
      # (ADR 0015 Phase 2): the owning BUNDLE, the ROLE (input=write | action=read), and the
      # neuron's own NAME. Attributes captured internal traffic per bundle/role/neuron; empty
      # for external (non-neuron) callers. Not secrets (unlike Authorization).
      '"x_neuron_bundle":"$http_x_neuron_bundle",'
      '"x_neuron_role":"$http_x_neuron_role",'
      '"x_neuron_name":"$http_x_neuron_name",'
      '"content_type":"$content_type",'
      '"content_length":"$content_length",'
      '"user_agent":"$http_user_agent",'
      '"accept":"$http_accept",'
      '"x_forwarded_for":"$http_x_forwarded_for"'
    '},'
)


def _one_format(name, surface, level):
    """Build a single named log_format for a surface at a level. surface + level are baked as
    LITERAL strings (each surface has exactly one level), so NO per-request $insp_surface var
    is needed - the schema stays uniform (same keys) and there is no map/set variable to fight
    nginx over. req_body via $request_body DIRECTLY (log-time eval; an early `set` would capture
    it before the body is read). resp_body via $insp_respbody (njs) only at request+response."""
    headers = _LOG_HEADERS if level in ("basic+headers", "request", "request+response") else '"req_headers":{},'
    reqbody = "$request_body" if level in ("request", "request+response") else ""
    respbody = "$insp_respbody" if level == "request+response" else ""
    body = ("{" + _LOG_BASIC
            + f'"surface":"{surface}","level":"{level}",'
            + headers
            + f'"req_body":"{reqbody}","resp_body":"{respbody}"' + "}")
    return f"    log_format {name} escape=json\n        '{body}';"


def inspect_format_name(surface):
    return "insp_" + surface.replace("-", "_")


def render_inspect_formats(env):
    """Render the per-surface log_format set (uniform schema across all of them). One format per
    surface that is not `off`, plus `insp_meta` (basic-only, surface '-') used as the fail2ban
    feed under the split topology. $insp_respbody (njs) is referenced ONLY by a surface at
    request+response, and js_set provides it then - so it is never referenced without njs."""
    out = ["    # Uniform JSON inspection log (ADR 0015 Phase 2). One schema (same keys); per",
           "    # surface, populated to its level. remote_addr + status keep their keys/positions",
           "    # so fail2ban still matches. surface/level are baked literals (no runtime var)."]
    for surface, level in _surface_levels(env).items():
        if level == "off":
            continue
        out.append(_one_format(inspect_format_name(surface), surface, level))
    # Metadata-only feed kept for fail2ban when the topology is `split` (content goes to the
    # per-surface files; access.log then carries only this basic record). Cheap if unused.
    out.append(_one_format("insp_meta", "-", "basic"))
    return "\n".join(out) + "\n"


# The njs response-body filter. ALWAYS written + mounted (harmless when not imported) so
# flipping a surface to request+response is a pure regen+recreate - the module is only
# LOADED (load_module/js_import) when some surface needs it. Accumulates the upstream body
# per-request onto `r`, capped to protect memory; js_set $insp_respbody reads it at log time.
# NB (VALIDATE LIVE before first production use of request+response): the js_body_filter
# forwarding contract (r.sendBuffer) and the njs module's presence in the nginx image must be
# confirmed against the running gateway - no surface uses this yet, so it is unproven live.
_INSPECT_JS = r"""// inspect.js - GENERATED by gateway_config.py (ADR 0015 sec 3). DO NOT hand-edit.
// Response-body capture for surfaces at inspect level `request+response`. Only imported
// when nginx.conf.template carries `js_import` (i.e. some surface needs it); otherwise this
// file sits mounted-but-unused. See gateway_config.py render_njs_* for the wiring.

// Cap accumulated bytes so a large model response can't balloon worker memory. Beyond the
// cap the body is truncated with a marker (the blue-team still sees the head + that it ran over).
var MAX_BYTES = 262144;  // 256 KiB

// js_body_filter: called per response chunk. Accumulate onto the request object, then
// forward the chunk UNCHANGED downstream via r.sendBuffer (the filter must forward or the
// response is withheld).
function filter(r, data, flags) {
    if (r.variables.insp_capping !== '1') {
        var cur = r.buf_resp || '';
        if (cur.length < MAX_BYTES) {
            r.buf_resp = cur + data;
            if (r.buf_resp.length >= MAX_BYTES) {
                r.buf_resp = r.buf_resp.substring(0, MAX_BYTES) + '...[truncated]';
            }
        }
    }
    r.sendBuffer(data, flags);
}

// js_set target: return the accumulated body at log time.
function respBody(r) {
    return r.buf_resp || '';
}

export default { filter, respBody };
"""


def render_njs_load(env):
    """Main-context `load_module` for njs - emitted ONLY when some surface is request+response
    (the response-body filter is the only thing needing njs). Empty otherwise (njs never pays)."""
    if not need_response_capture(env):
        return ("# (njs not loaded: no surface is request+response - response-body capture off.)")
    return ("# njs (ngx_http_js_module) - loaded because a surface is set to request+response\n"
            "# (ADR 0015 sec 3: response-body capture is the ONLY thing that pulls in njs).\n"
            "load_module modules/ngx_http_js_module.so;")


def render_njs_http(env):
    """http{}-context njs wiring (import + js_set for $insp_respbody). Emitted with the module."""
    if not need_response_capture(env):
        return ""
    return ("\n    # njs response-body capture (ADR 0015 sec 3). inspect.js accumulates the\n"
            "    # upstream body per-request; $insp_respbody exposes it to insp_reqresp.\n"
            "    js_path \"/etc/nginx/njs/\";\n"
            "    js_import inspect from inspect.js;\n"
            "    js_set $insp_respbody inspect.respBody;\n")


def inspect_block(env, surface, topology):
    """The per-server access_log stamp for this surface (its baked per-surface format carries
    surface/level, so no `set` is needed). topology `unified` -> one access.log (also fail2ban's
    source). `split` -> a per-surface content file PLUS a metadata-only access.log kept so
    fail2ban never goes blind. Level `off` -> no record (access_log off); fail2ban is then blind
    to that surface by the operator's choice."""
    level = inspect_level(env, _SURFACES[surface])
    if level == "off":
        return "        access_log off;\n"
    fmt = inspect_format_name(surface)
    fname = surface.replace("-", "_")
    if topology == "split":
        return (f"        access_log /var/log/nginx/inspect_{fname}.log {fmt};\n"
                f"        access_log /var/log/nginx/access.log insp_meta;  # kept for fail2ban\n")
    return f"        access_log /var/log/nginx/access.log {fmt};\n"  # unified (default)


def inspect_body_filter(env, surface):
    """The location-level `js_body_filter` line - ONLY for a surface at request+response (the
    filter accumulates the upstream body into $insp_respbody). Empty otherwise, so a stock
    (non-njs) gateway never carries the directive. buffer_type=string: bodies are text (JSON)."""
    if inspect_level(env, _SURFACES[surface]) == "request+response":
        return ("            # ADR 0015 sec 3: capture the upstream response body (njs).\n"
                "            js_body_filter inspect.filter buffer_type=string;\n")
    return ""


def inspect_topology(env):
    v = env.get("GATEWAY_INSPECT_LOG", "unified").strip()
    if v not in ("unified", "split"):
        die(f"GATEWAY_INSPECT_LOG must be unified|split, got {v!r}")
    return v


def internal_ratelimit(rl):
    """The limit_req lines for the INTERNAL listeners (ADR 0015 sec 5). Default `exempt` -> NO
    limit_req (a high-volume ingest is never throttled by the external DoS defense). `on` ->
    share the perip/pertoken zones with the relaxed internal_burst_* from gateway.conf."""
    mode = rl.get("internal_rate_limit", "exempt").strip()
    if mode == "exempt":
        return ("            # internal_rate_limit=exempt (ADR 0015 sec 5): no throttle on the\n"
                "            # internal ingest path. Set internal_rate_limit=on in gateway.conf to enable.")
    if mode == "on":
        bp = rl.get("internal_burst_perip", "240")
        bt = rl.get("internal_burst_pertoken", "480")
        return (f"            limit_req zone=perip    burst={bp} nodelay;\n"
                f"            limit_req zone=pertoken burst={bt} nodelay;")
    die(f"internal_rate_limit must be exempt|on, got {mode!r}")


def _chroma_allowed_map(authz):
    """The $allowed admission map for the chosen chroma authz MODE (brain.env)."""
    if authz == "open":                       # mode A: open read/write pass-through
        return ('    # admission: MODE open (A) - any client gets full RW (gateway still\n'
                '    # injects the token + terminates TLS).\n'
                '    map $is_read $allowed { default 1; }')
    if authz == "read-open":                  # mode B: reads open, writes need a writer token
        return ('    # admission: MODE read-open (B) - reads pass for anyone; writes need a\n'
                '    # writer token.\n'
                '    map "$is_read$is_writer" $allowed {\n'
                '        default 1;   # 10 reader-hits-read, 01 writer-any, 11 writer-hits-read\n'
                '        "00"    0;   # unauthenticated write / unknown route -> denied\n'
                '    }')
    if authz == "token-role":                 # mode C (default): everything needs a token
        return ('    # admission: MODE token-role (C, DEFAULT) - every request needs a token; a\n'
                '    # reader token is READ-ONLY, a writer token may read or write.\n'
                '    map "$is_read$is_writer$is_reader" $allowed {\n'
                '        default 0;\n'
                '        "~^1.1$" 1;   # reader token on a READ path only (is_read=1 AND is_reader=1)\n'
                '        "~^.1.$" 1;   # writer token - any method\n'
                '    }')
    die(f"CHROMA_GW_AUTHZ must be open|read-open|token-role, got {authz!r}")


def render_skeleton(env, rl):
    """Render nginx.conf.template - the http{} SKELETON: shared bits (ratelimit include,
    the inspection log_format ladder + njs wiring) + the chroma authz/token maps + the
    chroma.d/ollama.d glob includes. NO server block; the chroma and ollama listeners are
    separate .conf mounted by overlays."""
    authz = env.get("CHROMA_GW_AUTHZ", "token-role")
    head = (_NGINX_HEAD
            .replace("@NJS_LOAD@", render_njs_load(env))
            .replace("@LOG_FORMATS@", render_inspect_formats(env) + render_njs_http(env)))
    maps = _NGINX_CHROMA_MAPS.replace("@ALLOWED_MAP@", _chroma_allowed_map(authz))
    return head + maps + _NGINX_TAIL


def render_chroma_conf(env, rl):
    """Render chroma.conf - the chroma upstream + :8000 server, mounted into chroma.d/ by
    the CHROMA_EXPOSE overlay (compose.chroma-gateway.yaml). Sealed (empty comment) when
    CHROMA_EXPOSE=off, exactly mirroring ollama.conf. TLS from brain.env, bursts from
    gateway.conf. The $is_*/$allowed/$role maps it uses live in the skeleton (above)."""
    expose = "on" if (gw_publishes(env) and svc_enabled(env, "CHROMA")) else "off"
    tls = env.get("CHROMA_GW_TLS", "enforced")
    if tls not in ("off", "enforced"):
        die(f"CHROMA_GW_TLS must be off|enforced, got {tls!r}")
    if expose != "on":
        return ("# chroma.conf - SEALED (EXTERNAL_GATEWAY_ENABLE=off or CHROMA_ENABLE=off). Empty: no :8000\n"
                "# listener. Rendered by gateway_config.py. When exposed, this file carries the\n"
                "# chroma upstream + role-gated 8000 server (included via chroma.d/*.conf).\n")
    if tls == "enforced":
        listen = "        listen 8000 ssl;\n        http2  on;"
        ssl_block = _SSL_BLOCK
    else:
        listen = "        listen 8000;"
        ssl_block = ""
    header = (f"# chroma.conf - EXPOSED (gateway publishes + CHROMA_ENABLE=on, tls={tls}). GENERATED by gateway_config.py.\n"
              "# The chroma upstream + role-gated :8000 server, included via chroma.d/*.conf. The\n"
              "# $is_read/$is_writer/$is_reader/$allowed/$role maps it uses are in the skeleton\n"
              "# nginx.conf.template. ${CHROMA_MASTER_TOKEN_FOR_GW} is substituted by nginx envsubst at start.\n")
    topology = inspect_topology(env)
    server = (_NGINX_CHROMA_SERVER
              .replace("@LISTEN@", listen)
              .replace("@SSL_BLOCK@", ssl_block)
              .replace("@INSPECT@", inspect_block(env, "chroma-external", topology).rstrip("\n"))
              .replace("@INSPECT_FILTER@", inspect_body_filter(env, "chroma-external").rstrip("\n"))
              .replace("@BURST_PERIP@", rl.get("chroma_burst_perip", "60"))
              .replace("@BURST_PERTOKEN@", rl.get("chroma_burst_pertoken", "120")))
    return header + server


# --------------------------------------------------------------------------- #
# action.conf  (ADR 0017 sec Next - the external QUERY-API listener). The action neuron's
# HTTP query API runs long-lived on neuron_net (<brain>_action_api:8080, plain); this is the
# gateway's TLS :8443 server that fronts it, so an application calls the read side over the
# network AND the traffic is captured on the `action-external` inspection surface. Mounted into
# action.d/ by the ACTION_EXPOSE overlay (compose.action-neuron-gateway.yaml); sealed (empty) when off,
# exactly mirroring chroma.conf / ollama.conf.
#
# AUTHZ-IN (world -> API): ACTION_GW_AUTHZ knob.
#   token (DEFAULT) - a caller MUST present a bearer with the `action:call` grant (token_registry
#                     -> action_tokens.map); no/unknown token -> 403. This gates ADMISSION only
#                     (there are no read/write tiers here - the app behind it holds its own scoped
#                     reader token, so it cannot write regardless of the caller).
#   open            - no gate (any caller reaches the API). The earlier POC posture.
# The upstream is a plain proxy_pass - unlike chroma, no ${CHROMA_MASTER_TOKEN_FOR_GW} is injected (the API is
# the app, not a token-gated backend), so action.conf is mounted directly (no envsubst), like
# ollama.conf. Rate limiting is always on (cheap DoS defense).
# --------------------------------------------------------------------------- #
_NGINX_ACTION_SERVER = r"""
@AUTHZ_MAP@
@ROUTE_MAPS@
    server {
@LISTEN@
        server_name _;
@INSPECT@
        # Uniform JSON errors - no nginx/backend identity or detail leaks (own @-locations).
        proxy_intercept_errors on;
        error_page 400 @a400;
        error_page 401 @a401;
        error_page 403 @a403;
        error_page 404 @a404;
        error_page 405 @a405;
        error_page 413 @a413;
        error_page 429 @a429;
        error_page 500 501 502 503 504 @a5xx;
        location @a400 { default_type application/json; return 400 '{"status":400,"message":"bad request"}'; }
        location @a401 { default_type application/json; return 401 '{"status":401,"message":"unauthorized"}'; }
        location @a403 { default_type application/json; return 403 '{"status":403,"message":"forbidden"}'; }
        location @a404 { default_type application/json; return 404 '{"status":404,"message":"not found"}'; }
        location @a405 { default_type application/json; return 405 '{"status":405,"message":"method not allowed"}'; }
        location @a413 { default_type application/json; return 413 '{"status":413,"message":"payload too large"}'; }
        location @a429 { default_type application/json; return 429 '{"status":429,"message":"too many requests"}'; }
        location @a5xx { default_type application/json; return 502 '{"status":502,"message":"upstream error"}'; }
@SSL_BLOCK@
        # Anything that is NOT a /{bundle}/{neuron}/<suffix> triple (bare :8443, a two-segment
        # path, etc.) -> uniform JSON 404. This is the router's "no such route" surface.
        location / {
            limit_req zone=perip    burst=@BURST_PERIP@  nodelay;
            limit_req zone=pertoken burst=@BURST_PERTOKEN@ nodelay;
            default_type application/json;
            return 404 '{"status":404,"message":"not found - route is /{bundle}/{neuron}/<endpoint>"}';
        }

        # === PATH-ROUTER (ADR 0017 sec Next) ===
        # Match /{bundle}/{neuron}/<suffix>. The gateway owns ONLY the prefix: it resolves the
        # neuron's service on neuron_net at REQUEST time and forwards the deployer's own suffix
        # (path + method + body + query) UNTOUCHED. `{neuron}` is the neuron's CONFIG name = its
        # compose service name = its neuron_net DNS name.
        location ~ ^/(?<rt_bundle>[a-z0-9_]+)/(?<rt_neuron>[a-z0-9_]+)/(?<rt_suffix>.*)$ {
            limit_req zone=perip    burst=@BURST_PERIP@  nodelay;
            limit_req zone=pertoken burst=@BURST_PERTOKEN@ nodelay;
            proxy_hide_header Server;
@AUTHZ_GATE@
            # Target admission (ACTION_ROUTE_ALLOW): permit-any by default; a denied target ->
            # uniform JSON 404 (never a leaky 502 or an open proxy to an undeclared name).
            if ($route_allowed = 0) { return 404; }
@INSPECT_FILTER@
            # Strip any client Authorization so it never reaches the app (the neuron uses its
            # OWN scoped reader token internally; the world token, if any, is only for admission
            # here and must not leak upstream).
            proxy_set_header Authorization "";

            proxy_http_version 1.1;
            proxy_set_header Host              $host;
            proxy_set_header X-Real-IP         $remote_addr;
            proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header Connection        "";
            # LLM synthesis can take a while - don't cut the answer off.
            proxy_read_timeout 600s;

            # Resolve the neuron's service at REQUEST time via Docker's embedded DNS (a variable
            # in proxy_pass forces runtime resolution), NOT at nginx startup. This is deliberate:
            # this gateway ALSO fronts chroma/ollama, so it must boot even if a target neuron is
            # absent - a static `upstream {}` would make nginx refuse to start, taking the whole
            # brain offline. Down/unknown neuron -> 502. The variable-in-URI form ALSO strips the
            # /{bundle}/{neuron}/ prefix, forwarding only <suffix> (the neuron's own endpoint).
            resolver 127.0.0.11 valid=10s ipv6=off;
            set $rt_upstream "$rt_neuron:$rt_port";
            proxy_pass http://$rt_upstream/$rt_suffix$is_args$args;
        }
    }
"""


def read_route_registry(bd=BRAIN_DIR):
    """Parse brain_etc/gateway/route_registry -> [(bundle, neuron, port)]. Lines are
    `{bundle}/{neuron} = <port>`; blanks / #-comments ignored. Missing file -> []."""
    path = gateway_dir(bd) / "route_registry"
    out = []
    if not Path(path).is_file():
        return out
    for ln, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        target, _, port = s.partition("=")
        target, port = target.strip(), port.strip()
        if "/" not in target:
            die(f"route_registry line {ln}: target must be '{{bundle}}/{{neuron}}', got {target!r}")
        bundle, neuron = (p.strip() for p in target.split("/", 1))
        if not (bundle and neuron):
            die(f"route_registry line {ln}: empty bundle or neuron in {target!r}")
        if not port.isdigit():
            die(f"route_registry line {ln}: port must be an integer, got {port!r}")
        out.append((bundle, neuron, int(port)))
    return out


def _route_maps(env, entries):
    """The two http-level maps the router uses: $route_allowed (target admission, keyed on
    "$rt_bundle/$rt_neuron") and $rt_port (per-neuron internal serve port, keyed on $rt_neuron).
    ACTION_ROUTE_ALLOW: `any` (DEFAULT) = permit-any (default 1); `registry` = default-deny +
    only the route_registry targets. Port defaults to 8080; a registry line overrides per neuron."""
    allow = env.get("ACTION_ROUTE_ALLOW", "any").strip()
    if allow not in ("any", "registry"):
        die(f"ACTION_ROUTE_ALLOW must be any|registry, got {allow!r}")
    if allow == "any":
        allow_body = ("        # ACTION_ROUTE_ALLOW=any (DEFAULT, firewall permit-any posture): every\n"
                      "        # /{bundle}/{neuron}/ target is admitted. Set ACTION_ROUTE_ALLOW=registry\n"
                      "        # to flip to default-deny + the explicit allow-list from route_registry.\n"
                      "        default 1;")
    else:
        lines = ["        # ACTION_ROUTE_ALLOW=registry: default-deny; ONLY route_registry targets route.",
                 "        default 0;"]
        for bundle, neuron, _port in entries:
            lines.append(f'        "{bundle}/{neuron}" 1;')
        if not entries:
            lines.append("        # (route_registry is empty -> NOTHING routes. Add targets or use ACTION_ROUTE_ALLOW=any.)")
        allow_body = "\n".join(lines)
    port_lines = ["        default 8080;"]
    for _bundle, neuron, port in entries:
        port_lines.append(f'        "{neuron}" {port};')
    return (
        "    # -- Path-routing target admission (ADR 0017 sec Next). Keyed on the router's named\n"
        "    # captures; only set inside the router location, so other requests get the default.\n"
        '    map "$rt_bundle/$rt_neuron" $route_allowed {\n'
        f"{allow_body}\n"
        "    }\n"
        "    # -- Per-target internal serve port (default 8080 = action.py --serve default; a\n"
        "    # route_registry line pins a non-default port for that neuron).\n"
        "    map $rt_neuron $rt_port {\n"
        f"{chr(10).join(port_lines)}\n"
        "    }\n"
    )


def _action_authz(authz):
    """(map, gate) for the ACTION_GW_AUTHZ mode. `token` (default) gates world->API admission on a
    bearer with the action:call grant (action_tokens.map); `open` leaves it ungated."""
    if authz == "open":
        return ("", "            # ACTION_GW_AUTHZ=open - no admission gate on this surface.")
    if authz == "token":
        amap = (
            "    # -- Action query-API admission (ADR 0017 sec Next; ACTION_GW_AUTHZ=token). A\n"
            "    # bearer with the `action:call` grant (token_registry -> action_tokens.map) is\n"
            "    # admitted; anything else -> $is_action_caller=0 -> 403. Admission only (no tiers).\n"
            "    map $http_authorization $is_action_caller {\n"
            "        default 0;\n"
            "        include /etc/nginx/action_tokens.map;\n"
            "    }\n")
        gate = ("            # world -> API authorization-in: require an action:call bearer.\n"
                "            if ($is_action_caller = 0) { return 403; }")
        return (amap, gate)
    die(f"ACTION_GW_AUTHZ must be open|token, got {authz!r}")


def render_action_conf(env, rl, bd=BRAIN_DIR):
    """Render action.conf - the :8443 PATH-ROUTER + TLS server, mounted into action.d/ by the
    ACTION_EXPOSE overlay. Sealed (empty comment) when ACTION_EXPOSE=off, mirroring chroma.conf /
    ollama.conf. TLS + authz-in + route-admission from brain.env; bursts from gateway.conf; routable
    targets/ports from brain_etc/gateway/route_registry. The $role/$allowed maps its log_format
    references live in the skeleton (always emitted)."""
    expose = "on" if (gw_publishes(env) and has_action_neuron(bd)) else "off"
    tls = env.get("ACTION_GW_TLS", "enforced")
    authz = env.get("ACTION_GW_AUTHZ", "token")
    if tls not in ("off", "enforced"):
        die(f"ACTION_GW_TLS must be off|enforced, got {tls!r}")
    if expose != "on":
        return ("# action.conf - SEALED (EXTERNAL_GATEWAY_ENABLE=off or no action neuron). Empty: no :8443\n"
                "# listener. Rendered by gateway_config.py. When exposed, this carries the action\n"
                "# query-API upstream + :8443 server (included via action.d/*.conf).\n")
    if tls == "enforced":
        listen = "        listen 8443 ssl;\n        http2  on;"
        ssl_block = _SSL_BLOCK
    else:
        listen = "        listen 8443;"
        ssl_block = ""
    entries = read_route_registry(bd)
    route_maps = _route_maps(env, entries)
    authz_map, authz_gate = _action_authz(authz)
    allow = env.get("ACTION_ROUTE_ALLOW", "any").strip()
    header = (f"# action.conf - EXPOSED (gateway publishes + action neuron present, tls={tls}, authz={authz}, route_allow={allow}).\n"
              "# GENERATED by gateway_config.py. The :8443 PATH-ROUTER (ADR 0017 sec Next): matches\n"
              "# /{bundle}/{neuron}/<suffix>, resolves the neuron on neuron_net at request time, and\n"
              "# forwards the suffix untouched. Included via action.d/*.conf; inspected via\n"
              "# ACTION_EXTERNAL_INSPECT. Routable targets/ports: brain_etc/gateway/route_registry.\n")
    topology = inspect_topology(env)
    server = (_NGINX_ACTION_SERVER
              .replace("@AUTHZ_MAP@", authz_map.rstrip("\n"))
              .replace("@ROUTE_MAPS@", route_maps.rstrip("\n"))
              .replace("@AUTHZ_GATE@", authz_gate.rstrip("\n"))
              .replace("@LISTEN@", listen)
              .replace("@SSL_BLOCK@", ssl_block)
              .replace("@INSPECT@", inspect_block(env, "action-external", topology).rstrip("\n"))
              .replace("@INSPECT_FILTER@", inspect_body_filter(env, "action-external").rstrip("\n"))
              .replace("@BURST_PERIP@", rl.get("action_burst_perip", "60"))
              .replace("@BURST_PERTOKEN@", rl.get("action_burst_pertoken", "120")))
    return header + server


# --------------------------------------------------------------------------- #
# internal.conf  (ADR 0015 sec 1 - the internal listeners the neuron reaches over
# neuron_net). ALWAYS rendered (internal traffic is unconditional); mounted by the
# BASE gateway (not an exposure overlay) as a .template so ${CHROMA_MASTER_TOKEN_FOR_GW} is injected
# upstream. FULL PARITY WITH THE SERVICES: the two servers listen on the SAME ports as
# the real backends (chroma 8000, ollama 11434), bound to the gateway's STATIC neuron_net
# IP (@GW_NEURON_IP@) so they sit on the neuron-facing interface only and never collide
# with the wildcard external TLS listeners on those same ports. On neuron_net the gateway
# is aliased `chroma`/`ollama`, so the neuron's http://chroma:8000 / http://ollama:11434 hit
# these - a transparent, unbypassable inspection proxy. The upstreams target the DISTINCT
# brain_net aliases chroma-svc / ollama-svc (the bare names would resolve to the gateway
# itself on neuron_net -> a proxy loop). @GW_NEURON_IP@ is substituted at generation time.
# --------------------------------------------------------------------------- #
_NGINX_INTERNAL = r"""# internal.conf - GENERATED by gateway_config.py (ADR 0015 sec 1). DO NOT hand-edit.
# The INTERNAL listeners: the neuron sits on neuron_net and can resolve NEITHER chroma
# nor ollama directly - only the gateway (aliased `chroma`/`ollama` there, bridging
# neuron_net + brain_net). These two plain servers - on the SAME ports as the real services
# (8000, 11434), bound to the gateway's static neuron_net IP - are how its writes/reads
# transit nginx transparently. Included at http{} by the skeleton's
# `include /etc/nginx/internal.d/*.conf;`. ${CHROMA_MASTER_TOKEN_FOR_GW} is injected by nginx envsubst.

    # ===================== internal CHROMA (:8000, plain) =======================
    # Reuses the skeleton's $allowed / $is_read / $is_writer / $is_reader maps (always
    # present). The neuron carries a scoped chroma:writer token (NOT the master); nginx
    # validates it, strips it, and injects the master ${CHROMA_MASTER_TOKEN_FOR_GW} upstream. No
    # limit_req: internal ingest is high-volume by design (ADR 0015 sec 5, relaxed).
    upstream chroma_int_backend {
        server chroma-svc:8000;
        keepalive 16;
    }

    server {
        # One listen per bundle neuron-net: the gateway is multi-homed onto every bundle's net
        # and binds its static .2 on each (ADR-0015 per-bundle isolation). Generated below.
@GW_CHROMA_LISTENS@
        server_name _;
@INSPECT_CHROMA_INT@
        # Uniform JSON errors (own @-locations; named locations are per-server).
        proxy_intercept_errors on;
        error_page 400 @ie400;
        error_page 401 @ie401;
        error_page 403 @ie403;
        error_page 404 @ie404;
        error_page 405 @ie405;
        error_page 413 @ie413;
        error_page 500 501 502 503 504 @ie5xx;
        location @ie400 { default_type application/json; return 400 '{"status":400,"message":"bad request"}'; }
        location @ie401 { default_type application/json; return 401 '{"status":401,"message":"unauthorized"}'; }
        location @ie403 { default_type application/json; return 403 '{"status":403,"message":"forbidden"}'; }
        location @ie404 { default_type application/json; return 404 '{"status":404,"message":"not found"}'; }
        location @ie405 { default_type application/json; return 405 '{"status":405,"message":"method not allowed"}'; }
        location @ie413 { default_type application/json; return 413 '{"status":413,"message":"payload too large"}'; }
        location @ie5xx { default_type application/json; return 502 '{"status":502,"message":"upstream error"}'; }

        location / {
@INT_RL@
            proxy_hide_header Server;
            if ($allowed = 0) {
                return 403;
            }
@INSPECT_FILTER_CHROMA_INT@
            # Inject Chroma's MASTER service token (lives only in the gateway now);
            # strip the neuron's scoped bearer + any client X-Chroma-Token.
            proxy_set_header Authorization  "Bearer ${CHROMA_MASTER_TOKEN_FOR_GW}";
            proxy_set_header X-Chroma-Token "";

            proxy_http_version 1.1;
            proxy_set_header Host              $host;
            proxy_set_header X-Real-IP         $remote_addr;
            proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header Connection        "";

            proxy_pass http://chroma_int_backend;
        }
    }

    # ===================== internal OLLAMA (:11434, plain) ======================
    # Self-contained + UNIQUELY-named maps ($is_oint_*) so they never collide with
    # ollama.conf's $is_ollama_* maps when OLLAMA_EXPOSE=on. Inference-only (embeddings, the
    # read-only model metadata the neuron needs, AND generative inference: /api/generate +
    # /api/chat, which the images `caption` strategy uses to have a vision model describe an
    # image). Management (/api/pull etc.) is NOT listed -> fails closed (admin-only,
    # out-of-band). Ollama has no auth: nothing to inject; strip the client's bearer so it
    # never reaches ollama.
    map $uri $is_oint_inference {
        default 0;
        "~^/api/embeddings/?$" 1;
        "~^/api/embed/?$"      1;
        "~^/api/generate/?$"   1;
        "~^/api/chat/?$"       1;
        "~^/api/tags/?$"       1;
        "~^/api/version/?$"    1;
        "~^/api/show/?$"       1;
        "~^/api/ps/?$"         1;
    }
    map $http_authorization $is_oint_use {
        default 0;
        include /etc/nginx/ollama_use.map;
    }
    map "$is_oint_inference$is_oint_use" $oint_allowed {
        default 0;
        "11" 1;   # an inference route AND a valid use token
    }

    upstream ollama_int_backend {
        server ollama-svc:11434;
        keepalive 8;
    }

    server {
        # One listen per bundle neuron-net (see the chroma-internal server above).
@GW_OLLAMA_LISTENS@
        server_name _;
@INSPECT_OLLAMA_INT@
        proxy_intercept_errors on;
        error_page 400 @oie400; error_page 403 @oie403; error_page 429 @oie429;
        error_page 500 501 502 503 504 @oie5xx;
        location @oie400 { default_type application/json; return 400 '{"status":400,"message":"bad request"}'; }
        location @oie403 { default_type application/json; return 403 '{"status":403,"message":"forbidden"}'; }
        location @oie429 { default_type application/json; return 429 '{"status":429,"message":"too many requests"}'; }
        location @oie5xx { default_type application/json; return 502 '{"status":502,"message":"upstream error"}'; }

        location / {
@INT_RL@
            proxy_hide_header Server;
            if ($oint_allowed = 0) { return 403; }
@INSPECT_FILTER_OLLAMA_INT@
            proxy_set_header Authorization "";
            proxy_http_version 1.1;
            proxy_set_header Host       $host;
            proxy_set_header Connection "";
            # Generative inference (/api/generate, /api/chat) on CPU can take MINUTES, especially
            # on a cold model load - the default 60s read timeout would cut a slow answer to a
            # 504 (seen live: the action query API's first llama3.2:1b synthesis). Embeddings are
            # fast; generation is not. Give the internal model path a generous ceiling.
            proxy_read_timeout 600s;
            proxy_send_timeout 600s;
            proxy_pass http://ollama_int_backend;
        }
    }
"""


def render_internal_conf(env, rl, bd=BRAIN_DIR):
    """Render internal.conf - the ADR 0015 sec 1 internal listeners (chroma :8000, ollama
    :11434) PLUS the ADR-0020 Q2 external-egress listeners. Always rendered (no knob gates
    internal traffic on/off); the $allowed/$is_* maps its chroma half reuses live in the skeleton,
    its ollama maps are self-contained and uniquely named. Mounted as a .template so
    ${CHROMA_MASTER_TOKEN_FOR_GW} is injected upstream. PER-BUNDLE ISOLATION (ADR-0015): the gateway
    is multi-homed onto EVERY bundle's neuron net, so each server emits one `listen <gw_ip>:PORT`
    per net (the gateway's STATIC .2 on that net). The IPs are substituted HERE (generation time),
    not via nginx envsubst (NGINX_ENVSUBST_FILTER=CHROMA would not touch them). The bundle->net map
    is neuron_topology (the single source of truth).

    Q2 (per-neuron/bundle backends + external egress): a net whose EFFECTIVE chroma/inference
    endpoint is an off-box URL is EXCLUDED from the built-in server's listen list and instead gets
    an ADDITIONAL upstream + server block on the SAME <gw_ip>:PORT that proxies OFF-BOX over TLS,
    inspected on the SAME internal surface. The neuron is unchanged (still http://chroma:8000). When
    NO net is external the appended text is empty -> byte-identical to the internal-only output."""
    nets = _net_backends(bd)
    # Q2 STUB: the OpenAI inference provider is a reserved seam (api_key_gpg_id -> GPG key), NOT
    # built. Fail closed rather than half-render a provider whose auth/shape does not exist yet.
    for n in nets:
        if n["inference_provider"] == "openai":
            die(f"bundle {n['name']!r}: inference provider 'openai' is a Q2 STUB (api_key_gpg_id "
                f"seam reserved) - not built. Use provider: ollama, or await the OpenAI provider.")
    chroma_int = [n for n in nets if n["chroma_endpoint"] == "internal"]
    chroma_ext = [n for n in nets if n["chroma_endpoint"] != "internal"]
    ollama_int = [n for n in nets if n["inference_endpoint"] == "internal"]
    ollama_ext = [n for n in nets if n["inference_endpoint"] != "internal"]
    # An off-box alias still binds the SAME built-in server block scaffold; a brain whose every net
    # points an alias off-box (no internal listen left) is a Q2 gap - fail closed with a clear msg.
    if chroma_ext and not chroma_int:
        die("every neuron-net points chroma OFF-BOX: the built-in chroma listener would then have "
            "no address to bind. Keep >=1 bundle on internal chroma (Q2 limitation; Phase 5).")
    if ollama_ext and not ollama_int:
        die("every neuron-net points inference OFF-BOX: the built-in ollama listener would then "
            "have no address to bind. Keep >=1 bundle on internal ollama (Q2 limitation; Phase 5).")
    chroma_listens = "\n".join(f"        listen {n['gw_ip']}:8000;" for n in chroma_int)
    ollama_listens = "\n".join(f"        listen {n['gw_ip']}:11434;" for n in ollama_int)
    topology = inspect_topology(env)
    base = (_NGINX_INTERNAL
            .replace("@GW_CHROMA_LISTENS@", chroma_listens)
            .replace("@GW_OLLAMA_LISTENS@", ollama_listens)
            .replace("@INT_RL@", internal_ratelimit(rl))
            .replace("@INSPECT_CHROMA_INT@", inspect_block(env, "chroma-internal", topology).rstrip("\n"))
            .replace("@INSPECT_FILTER_CHROMA_INT@", inspect_body_filter(env, "chroma-internal").rstrip("\n"))
            .replace("@INSPECT_OLLAMA_INT@", inspect_block(env, "ollama-internal", topology).rstrip("\n"))
            .replace("@INSPECT_FILTER_OLLAMA_INT@", inspect_body_filter(env, "ollama-internal").rstrip("\n")))
    return base + render_external_egress(env, rl, chroma_ext, ollama_ext, topology)


# --------------------------------------------------------------------------- #
# External backend egress THROUGH the gateway (ADR-0020 Q2). The invariant (LOCKED): ALL neuron
# egress routes through the gateway - including off-box endpoints - so inspection is never bypassed.
# The neuron still talks to http://chroma:8000 / http://ollama:11434 on its own neuron_net (the
# gateway is aliased there and binds its static .2); we only change what the gateway's upstream for
# that alias/address is: an off-box URL over TLS instead of the built-in chroma-svc/ollama-svc. The
# egress is stamped with the SAME internal inspection surface (chroma-internal / ollama-internal),
# so the blue-team parses one shape whether the backend is internal or off-box.
# --------------------------------------------------------------------------- #
def _net_backends(bd=BRAIN_DIR):
    """Per neuron-net (= per bundle; neuron_topology) EFFECTIVE backend endpoints. Returns a list
    mirroring neuron_topology.topology(bd), each entry the topology dict PLUS:
        chroma_endpoint     'internal' | '<https URL>'
        inference_endpoint  'internal' | '<https URL>'
        inference_provider  'ollama' | 'openai'
    A net = one bundle = ONE address (.2) for the chroma/ollama alias, so a net carries ONE upstream
    per alias. Effective endpoint = the bundle default overlaid by per-neuron overrides, requiring
    the neurons ON a net to AGREE (a per-neuron divergence within one bundle fails closed here)."""
    import brain_env as be
    nets = neuron_topology.topology(bd)
    try:
        neurons = be.load_neurons(brain_env_path(bd))
    except be.BrainEnvError as e:
        die(str(e))
    by_name = {b.get("name"): b for b in be.iter_bundles(neurons)}
    out = []
    for net in nets:
        bundle = by_name.get(net["name"])
        ch_ep, _ = _effective_net_endpoint(bundle, "chroma", net["name"])
        inf_ep, inf_prov = _effective_net_endpoint(bundle, "inference", net["name"])
        e = dict(net)
        e["chroma_endpoint"] = ch_ep
        e["inference_endpoint"] = inf_ep
        e["inference_provider"] = inf_prov or "ollama"
        out.append(e)
    return out


def _effective_net_endpoint(bundle, kind, net_name):
    """(endpoint, provider) for a whole net. A topology net with no zone bundle -> internal. The
    net's neurons must resolve to ONE endpoint (bundle default + a UNANIMOUS per-neuron override);
    a divergence dies (one net has one alias/address, so its neurons cannot split an upstream)."""
    import brain_env as be
    if bundle is None:
        return ("internal", "ollama" if kind == "inference" else None)
    neurons = bundle.get("neurons") or [{}]   # no neurons -> resolve the bundle default alone
    endpoints, provider = set(), None
    for n in neurons:
        sec = be.resolve_backend(bundle, n, kind)
        endpoints.add(str(sec.get("endpoint", "internal")))
        if kind == "inference":
            p = sec.get("provider", "ollama")
            if provider is None:
                provider = p
            elif provider != p:
                die(f"bundle {net_name!r}: neurons declare conflicting inference providers "
                    f"({provider!r} vs {p!r}); one neuron-net carries one inference alias.")
    if len(endpoints) > 1:
        die(f"bundle {net_name!r}: neurons declare conflicting {kind} endpoints {sorted(endpoints)} "
            f"- one neuron-net has ONE {kind} alias/address, so a bundle's neurons must agree "
            f"(bundle default + a UNANIMOUS per-neuron override). Split them into separate bundles.")
    return (endpoints.pop(), provider)


def parse_external_endpoint(kind, endpoint, where):
    """Parse an OFF-BOX backend endpoint into (host, port). REQUIRES https:// - all off-box egress
    is TLS (the Q2 invariant). A base path is not supported for chroma/ollama egress (the neuron's
    own request URI is forwarded verbatim); flagged if present. 'internal' is handled by the caller."""
    u = urlparse(endpoint)
    if u.scheme != "https":
        die(f"{where}: external {kind} endpoint must be an https:// URL (off-box egress is TLS-only), "
            f"got {endpoint!r}")
    if not u.hostname:
        die(f"{where}: external {kind} endpoint {endpoint!r} has no host")
    if u.path.strip("/"):
        die(f"{where}: external {kind} endpoint {endpoint!r} carries a base PATH; Q2 egress forwards "
            f"the neuron's own request URI verbatim (host:port only). Drop the path (Phase 5 seam).")
    return u.hostname, (u.port or 443)


_EXT_EGRESS_HEADER = (
    "\n    # ===================== EXTERNAL BACKEND EGRESS (ADR-0020 Q2) ================\n"
    "    # Off-box chroma/ollama endpoints, STILL fronted by the gateway on the neuron's own\n"
    "    # neuron_net address (so egress is inspected, never bypassed - the Q2 invariant). Each\n"
    "    # block below listens on a bundle-net's gateway .2 for the SAME alias the neuron uses and\n"
    "    # proxies OFF-BOX over TLS. The neuron is UNCHANGED (http://chroma:8000 / http://ollama).\n")


def _json_error_locations(prefix):
    """The uniform-JSON error @-locations for an egress server (named locations are per-server, so
    the same prefix may repeat across blocks). Mirrors the built-in internal servers."""
    codes = [(400, "bad request"), (401, "unauthorized"), (403, "forbidden"), (404, "not found"),
             (405, "method not allowed"), (413, "payload too large"), (429, "too many requests")]
    lines = [f"        location @{prefix}{c} {{ default_type application/json; "
             f"return {c} '{{\"status\":{c},\"message\":\"{m}\"}}'; }}" for c, m in codes]
    lines.append(f"        location @{prefix}5xx {{ default_type application/json; "
                 f"return 502 '{{\"status\":502,\"message\":\"upstream error\"}}'; }}")
    return "\n".join(lines)


def _render_chroma_ext_block(env, rl, net, topology):
    host, port = parse_external_endpoint("chroma", net["chroma_endpoint"], f"bundle {net['name']!r}")
    up = f"chroma_ext_b{net['index']}"
    inspect = inspect_block(env, "chroma-internal", topology).rstrip("\n")
    ifilter = inspect_body_filter(env, "chroma-internal").rstrip("\n")
    intrl = internal_ratelimit(rl)
    return f"""
    # --- external CHROMA egress for bundle '{net['name']}' -> {host}:{port} (over TLS) ---
    # The bundle's neuron reaches this exactly like the internal chroma (its neuron_net alias
    # `chroma` resolves to the gateway's {net['gw_ip']}); the ONLY difference is this upstream is
    # off-box. Reuses the skeleton $allowed / $is_* maps + the chroma-internal inspect surface.
    upstream {up} {{
        server {host}:{port};
        keepalive 16;
    }}

    server {{
        listen {net['gw_ip']}:8000;
        server_name _;
{inspect}
        proxy_intercept_errors on;
        error_page 400 @xce400; error_page 401 @xce401; error_page 403 @xce403;
        error_page 404 @xce404; error_page 405 @xce405; error_page 413 @xce413;
        error_page 500 501 502 503 504 @xce5xx;
{_json_error_locations("xce")}

        location / {{
{intrl}
            proxy_hide_header Server;
            if ($allowed = 0) {{
                return 403;
            }}
{ifilter}
            # EXTERNAL-ENDPOINT CREDENTIAL SEAM (ADR-0020 Q2): auth to the OFF-BOX service is the
            # gateway's concern (api_key_gpg_id -> a GPG-stored key). STUB - NOT built: the neuron's
            # scoped bearer is stripped here and NO upstream credential is injected yet (Phase 5).
            proxy_set_header Authorization  "";
            proxy_set_header X-Chroma-Token "";

            proxy_http_version 1.1;
            proxy_set_header Host              {host};
            proxy_set_header X-Real-IP         $remote_addr;
            proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header Connection        "";

            # TLS to the off-box endpoint (SNI). proxy_ssl_verify is OFF in the Q2 stub (no CA
            # trust store wired yet); turning it on + pinning a CA is a Phase 5 hardening seam.
            proxy_ssl_server_name on;
            proxy_ssl_name {host};
            proxy_ssl_verify off;
            proxy_pass https://{up};
        }}
    }}
"""


def _render_ollama_ext_block(env, rl, net, topology):
    host, port = parse_external_endpoint("inference", net["inference_endpoint"], f"bundle {net['name']!r}")
    up = f"ollama_ext_b{net['index']}"
    inspect = inspect_block(env, "ollama-internal", topology).rstrip("\n")
    ifilter = inspect_body_filter(env, "ollama-internal").rstrip("\n")
    intrl = internal_ratelimit(rl)
    return f"""
    # --- external OLLAMA/inference egress for bundle '{net['name']}' -> {host}:{port} (over TLS) ---
    # Same alias/address the neuron already uses (`ollama` -> {net['gw_ip']}); the upstream is
    # off-box. Reuses internal.conf's own $is_oint_* / $oint_allowed maps + ollama-internal inspect.
    upstream {up} {{
        server {host}:{port};
        keepalive 8;
    }}

    server {{
        listen {net['gw_ip']}:11434;
        server_name _;
{inspect}
        proxy_intercept_errors on;
        error_page 400 @xoe400; error_page 403 @xoe403; error_page 429 @xoe429;
        error_page 500 501 502 503 504 @xoe5xx;
{_json_error_locations("xoe")}

        location / {{
{intrl}
            proxy_hide_header Server;
            if ($oint_allowed = 0) {{ return 403; }}
{ifilter}
            # EXTERNAL-ENDPOINT CREDENTIAL SEAM (ADR-0020 Q2): a hosted inference endpoint's key is
            # the gateway's concern (api_key_gpg_id -> GPG key). STUB - NOT built: the client bearer
            # is stripped and NO upstream credential is injected yet (Phase 5).
            proxy_set_header Authorization "";
            proxy_http_version 1.1;
            proxy_set_header Host       {host};
            proxy_set_header Connection "";
            proxy_read_timeout 600s;
            proxy_send_timeout 600s;
            proxy_ssl_server_name on;
            proxy_ssl_name {host};
            proxy_ssl_verify off;
            proxy_pass https://{up};
        }}
    }}
"""


def render_external_egress(env, rl, chroma_ext, ollama_ext, topology):
    """Append the Q2 external-egress upstream+server blocks for every net whose effective chroma /
    inference endpoint is off-box. Returns '' when there are none, so the internal-only output stays
    byte-identical to today's."""
    if not chroma_ext and not ollama_ext:
        return ""
    parts = [_EXT_EGRESS_HEADER]
    for net in chroma_ext:
        parts.append(_render_chroma_ext_block(env, rl, net, topology))
    for net in ollama_ext:
        parts.append(_render_ollama_ext_block(env, rl, net, topology))
    return "".join(parts)


# --------------------------------------------------------------------------- #
# fail2ban  (from gateway.conf [fail2ban])
# --------------------------------------------------------------------------- #
def render_jail(f2b, env):
    enabled = "true" if f2b.get("enabled", "on").lower() in ("on", "true", "1", "yes") else "false"
    bantime = f2b.get("bantime", "1h")
    findtime = f2b.get("findtime", "10m")
    maxretry = f2b.get("maxretry", "20")
    ignoreip = f2b.get("ignoreip", "127.0.0.1/8 ::1 10.0.2.2/32")
    cport = env.get("CHROMA_PORT", "8000")
    oport = env.get("OLLAMA_PORT", "11434")
    return f"""# jail.d/gateway.conf - GENERATED by gateway_config.py from gateway.conf [fail2ban].
# Do NOT hand-edit. fail2ban jail for the nginx TLS gateway. Bans a source
# IP that trips too many gateway denials in a window, then SELF-HEALS. Bans are L3
# (iptables in the gateway's shared netns), in front of nginx.

[DEFAULT]
ignoreip = {ignoreip}

[gateway]
enabled  = {enabled}
bantime  = {bantime}
findtime = {findtime}
maxretry = {maxretry}
filter   = nginx-gateway
logpath  = /var/log/nginx/access.log
port     = {cport},{oport}
protocol = tcp
banaction = iptables-multiport
action = %(action_)s
         seam-banlist
"""


def render_filter(f2b):
    statuses = f2b.get("count_statuses", "403")
    alt = "|".join(s.strip() for s in statuses.replace(" ", ",").split(",") if s.strip())
    return f"""# filter.d/nginx-gateway.conf - GENERATED by gateway_config.py from gateway.conf
# [fail2ban] count_statuses. Do NOT hand-edit. Matches a DENIED request in the gateway's
# JSON access log (lwcp_json).
#
# IMPORTANT: `datepattern` makes fail2ban STRIP the matched timestamp from the line BEFORE
# applying failregex, so failregex anchors on "remote_addr" (which survives), NOT on the
# leading "time" field (verified: anchoring on ^{{"time" matches 0 lines).

[Definition]
failregex = "remote_addr":"<HOST>",.*?"status":(?:{alt}),

ignoreregex =

# ISO8601 with offset inside the JSON. `%%` escapes the configparser percent.
datepattern = "time":"%%Y-%%m-%%dT%%H:%%M:%%S
"""


# The ban-list VIEW action is pure plumbing (no knobs) - emitted verbatim.
_SEAM_BANLIST = r"""# action.d/seam-banlist.conf - GENERATED by gateway_config.py (verbatim, no knobs).
# Maintains a READ-ONLY current-ban view at /data/state/current_bans.txt so an operator
# sees live ban state without exec-ing into the container. Appends on ban, deletes the
# line on unban/self-heal. Each action MUST be ONE simple command (multi-statement forms
# got mangled by fail2ban's action runner - verified live).

[Definition]

actionstart = printf '' > '<banfile>'
actionstop  = printf '' > '<banfile>'
actioncheck =
actionban   = printf '%%s\n' '<ip>' >> '<banfile>'
actionunban = sed -i '/^<ip>$/d' '<banfile>'

[Init]

banfile = /data/state/current_bans.txt
"""


# --------------------------------------------------------------------------- #
# Published-port conflict check (the gateway maps host ports 1:1 -> container).
# --------------------------------------------------------------------------- #
def check_gateway_port_conflicts(env, bd=BRAIN_DIR):
    """The gateway publishes each PUBLISHED surface on a distinct HOST port (docker `ports:` is a
    1:1 host->container map), so two published listeners on the same host port would clash at
    `compose up`. Catch it here, at generate time, with a legible error instead of a runtime bind
    failure. Published = the gateway publishes host ports (EXTERNAL_GATEWAY_ENABLE=on) AND the
    surface exists: a built-in backend that is enabled, or an action neuron's declared port(s)."""
    ports = {}  # host_port -> surface label
    if not gw_publishes(env):
        return  # fully sealed: no host ports at all
    # built-in backends (flat zone): published on their host port when enabled
    builtins = [
        ("CHROMA", "CHROMA_PORT", "8000", "chroma"),
        ("OLLAMA", "OLLAMA_PORT", "11434", "ollama"),
    ]
    for svc, port_key, default, label in builtins:
        if not svc_enabled(env, svc):
            continue
        port = env.get(port_key, default).strip()
        if port in ports:
            die(f"host port {port} claimed by BOTH {ports[port]} and {label}. Give each published "
                f"surface a distinct host port in brain.env.")
        ports[port] = label
    # action neurons (YAML neuron zone): each declared publish_to_lan_port is a 1:1 host publish
    import brain_env as be
    try:
        neurons = be.load_neurons(brain_env_path(bd))
    except Exception:
        neurons = {"neuron_bundles": []}
    for _bundle, n in be.iter_neurons(neurons, "action"):
        for p in (n.get("publish_to_lan_ports") or []):
            port, label = str(p), f"action:{n.get('name')}"
            if port in ports:
                die(f"host port {port} claimed by BOTH {ports[port]} and {label}. Give each published "
                    f"surface a distinct host port.")
            ports[port] = label


# --------------------------------------------------------------------------- #
# Generate everything
# --------------------------------------------------------------------------- #
def cmd_generate(args):
    # Anchor the brain root to an ABSOLUTE, existing directory BEFORE any _write_lf mkdir.
    # Root-cause fix for the mangled-path drift (NOTE 001-35): an unanchored / drive-less
    # --brain-dir (e.g. a Windows path mangled to "installrootbrainsmybrain" across a
    # bash boundary, or a relative AIOS_INSTALL_ROOT) would otherwise be materialized by
    # _write_lf(mkdir parents=True) RELATIVE TO CWD, creating a stray brain tree. Fail closed.
    bd = Path(os.path.abspath(args.brain_dir)) if args.brain_dir else BRAIN_DIR
    if not bd.is_dir():
        die(f"--brain-dir does not resolve to an existing directory: {str(bd)!r} "
            f"(cwd={os.getcwd()}). Refusing to autogen a CWD-relative stray brain tree.")
    gdir = gateway_dir(bd)
    env = read_env(brain_env_path(bd))
    cp = read_gateway_conf(gateway_conf_path(bd))
    rl = dict(cp["ratelimit"]) if cp.has_section("ratelimit") else {}
    f2b = dict(cp["fail2ban"]) if cp.has_section("fail2ban") else {}

    # 0) neuron named-token model (Phase 3): resolve every neuron's `gateway_token:` name against
    #    the registry BEFORE emitting any config, so a neuron that names a token nobody created
    #    fails closed with a clear "create it with gateway_tokens.py" message (not a half-render).
    resolve_neuron_tokens(bd)

    # 1) nginx backend: the skeleton + the two service listeners (chroma + ollama)
    _write_lf(gdir / "nginx_auto_gen" / "nginx.conf.template", render_skeleton(env, rl))
    _write_lf(gdir / "nginx_auto_gen" / "ratelimit.conf", render_ratelimit(rl))
    _write_lf(gdir / "nginx_auto_gen" / "chroma.conf", render_chroma_conf(env, rl))
    # action query-API PATH-ROUTER (ADR 0017 sec Next) - mounted by the ACTION_EXPOSE overlay.
    check_gateway_port_conflicts(env, bd)
    _write_lf(gdir / "nginx_auto_gen" / "action.conf", render_action_conf(env, rl, bd))
    # internal listeners (ADR 0015 sec 1) - always rendered; mounted by the base gateway.
    _write_lf(gdir / "nginx_auto_gen" / "internal.conf", render_internal_conf(env, rl, bd))
    # njs response-body filter (ADR 0015 sec 3) - ALWAYS written + mounted (harmless when not
    # imported); load_module/js_import appear only when a surface is request+response.
    _write_lf(gdir / "nginx_auto_gen" / "njs" / "inspect.js", _INSPECT_JS)

    # 2) ollama.conf (reuse the ollama renderer; bursts from gateway.conf). Pass the
    #    ollama-EXTERNAL inspection stamp (ADR 0015 Phase 2) so its server logs like the rest.
    topology = inspect_topology(env)
    ollama_txt = ollama_gateway.render(
        "on" if (gw_publishes(env) and svc_enabled(env, "OLLAMA")) else "off",
        env.get("OLLAMA_GW_TLS", "enforced"),
        env.get("OLLAMA_GW_AUTHZ", "token-role"),
        int(rl.get("ollama_burst_perip", "30")),
        int(rl.get("ollama_burst_pertoken", "60")),
        inspect_block(env, "ollama-external", topology).rstrip("\n"),
        inspect_body_filter(env, "ollama-external").rstrip("\n"))
    _write_lf(gdir / "nginx_auto_gen" / "ollama.conf", ollama_txt)

    # 3) token maps (reuse the registry generator) -> token_maps_auto_gen/
    entries = gateway_tokens.read_registry(bd)
    counts = gateway_tokens.generate(entries, gdir)

    # 4) fail2ban backend
    _write_lf(gdir / "fail2ban_autoconfigs" / "jail.d" / "gateway.conf", render_jail(f2b, env))
    _write_lf(gdir / "fail2ban_autoconfigs" / "filter.d" / "nginx-gateway.conf", render_filter(f2b))
    _write_lf(gdir / "fail2ban_autoconfigs" / "action.d" / "seam-banlist.conf", _SEAM_BANLIST)

    # 5) render brain.env's FLAT zone -> brain_etc/docker/.env.rendered (the file the seam
    #    manifest ships to ~/docker/.env). brain.env is the two-zone source; compose must never
    #    see the YAML neuron zone, so we split here rather than copy brain.env verbatim.
    rendered = brain_env.render_dotenv(bd)
    info(f"rendered flat zone -> {rendered.relative_to(bd)}")

    # 6) render EVERY neuron bundle's compose service blocks from the brain.env neuron zone
    #    (config-flow Phase 4 + P0 unification). Idempotent; keeps compose.yaml in lockstep with the
    #    zone so names/collection/synth-model/embed-model/per-neuron token/service_type never drift
    #    from the neuron object. BOTH regions render through the SAME shared neuron_compose block
    #    renderer — the DEFAULT bundle here, the ADDITIONAL bundles (+ their nets/gateway attachments)
    #    via add_neuron_bundle.render_all. This is the ONE materialize step (there is no longer a
    #    hand-run tool that renders a divergent additional shape). Lenient: a pre-refactor compose
    #    missing a region's markers WARNs + skips, never hard-aborts a keepalive apply.
    compose = neuron_compose.render(bd, strict=False)
    if compose is not None:
        info(f"rendered default-bundle compose region -> {compose.relative_to(bd)}")
    compose_add = add_neuron_bundle.render_all(bd, strict=False)
    if compose_add is not None:
        info(f"rendered additional-bundle compose regions -> {compose_add.relative_to(bd)}")

    # 7) render the runtime source manifest (sources.yaml) from the brain.env neuron zone
    #    (config-flow Phase 5). Retires the hand-authored brain_etc/neuron/sources.yaml as an
    #    input; the seam manifest ships THIS generated file to ~/docker/neuron/sources.yaml, which
    #    the input container reads at /etc/neuron/sources.yaml. Fails closed on a bad zone.
    sources = brain_env.render_sources_yaml(bd)
    info(f"rendered source manifest -> {sources.relative_to(bd)}")

    info(f"generated nginx_auto_gen/ (gateway publishes={gw_publishes(env)}; "
         f"chroma={svc_enabled(env,'CHROMA')} tls={env.get('CHROMA_GW_TLS','enforced')} "
         f"authz={env.get('CHROMA_GW_AUTHZ','token-role')}, "
         f"ollama={svc_enabled(env,'OLLAMA')}, action={has_action_neuron(bd)}), "
         f"maps ({', '.join(f'{k}={v}' for k, v in counts.items())}), fail2ban_autoconfigs/.")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Render all gateway backend config from brain.env + gateway.conf + "
                    "token_registry into the *_auto_gen backend folders.")
    ap.add_argument("--brain-dir", help="brain root (default: this tool's brain)")
    ap.set_defaults(func=cmd_generate)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
