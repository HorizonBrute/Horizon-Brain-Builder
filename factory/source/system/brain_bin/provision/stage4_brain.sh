#!/usr/bin/env bash
# Stage 4 (run as the BRAIN user in the distro). LAY the Chroma stack for the
# READ-ACCESS GATEWAY model: Chroma sealed on brain_net + token-required, the
# nginx gateway as the one published surface on :8000 (TLS), data in-distro.
#
# It LAYS the stack (compose + nginx + token maps + .env + cert); it does NOT run it.
# Bringing the stack up is reapply_brain_configs.py's job on the real runtime VM — see
# the note above the ownership check at the bottom for why the old build-time `up` +
# self-checks were removed. Vector data in ~/chroma_store persists (same bind mount).
#
# Usage:  stage4_brain.sh [personal|server] [gateway_src_dir]
#   posture       default 'personal' (loopback bind, localhost cert). 'server' binds
#                 0.0.0.0 — pair with a host firewall rule + LAN SAN at onboard.
#   gateway_src   where the authored gateway/ artifacts are staged; default is the
#                 sibling ../gateway relative to this script (system/brain_bin/gateway).
set -uo pipefail

POSTURE="${1:-personal}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_SRC="${2:-$(cd "${SCRIPT_DIR}/../gateway" 2>/dev/null && pwd)}"

# compose.yaml has ONE source: the ADR-0015 config-seam template brain_etc.example/docker/.
# It used to live in gateway/ as well, and the two drifted badly — gateway/ kept a 79-line
# pre-ADR-0013 prototype (base `ports:`, no ollama service) while the template moved on to the
# 403-line ADR-0013 model (no base `ports:`; exposure comes from the compose.*-gateway.yaml
# overlays). Staging the prototype here put a stale base under current overlays: both published
# :8000, the gateway collided with itself, and ollama had no container. gateway/ still owns the
# things that are genuinely its own (gen-cert.sh, nginx/). Do NOT reintroduce a compose.yaml
# there — if you need to change the stack, change the template.
COMPOSE_SRC="${COMPOSE_SRC:-$(cd "${SCRIPT_DIR}/../../../brain_etc.example/docker" 2>/dev/null && pwd)}"

if [ -z "${GATEWAY_SRC:-}" ] || [ ! -f "${GATEWAY_SRC}/gen-cert.sh" ]; then
    echo "ERROR: gateway source not found (looked for gen-cert.sh under '${GATEWAY_SRC:-<unset>}')." >&2
    echo "Pass the path to system/brain_bin/gateway as arg 2." >&2
    exit 1
fi
if [ -z "${COMPOSE_SRC:-}" ] || [ ! -f "${COMPOSE_SRC}/compose.yaml" ]; then
    echo "ERROR: compose template not found (looked for compose.yaml under '${COMPOSE_SRC:-<unset>}')." >&2
    echo "Expected the brain_etc.example/docker/ seam template. Override with COMPOSE_SRC=." >&2
    exit 1
fi

case "${POSTURE}" in
    personal) GATEWAY_BIND="127.0.0.1" ;;
    server)   GATEWAY_BIND="0.0.0.0"  ;;
    *) echo "ERROR: posture must be 'personal' or 'server' (got '${POSTURE}')." >&2; exit 1 ;;
esac

# Runs as the brain user, so default the name to the current Linux user; override
# with `BRAIN_NAME=` or `BRAIN=` in the env.
BRAIN_NAME="${BRAIN_NAME:-${BRAIN:-$(id -un)}}"
cd ~
mkdir -p docker chroma_store logs/gateway gateway/gateway_out

echo "== lay gateway stack from ${GATEWAY_SRC} (compose from ${COMPOSE_SRC}) =="
cp    "${COMPOSE_SRC}/compose.yaml"               ~/docker/compose.yaml
cp    "${GATEWAY_SRC}/gen-cert.sh"                 ~/docker/gen-cert.sh
chmod +x                                          ~/docker/gen-cert.sh
mkdir -p                                          ~/docker/nginx
cp    "${GATEWAY_SRC}/nginx/nginx.conf.template"  ~/docker/nginx/nginx.conf.template
# ratelimit.conf is a REQUIRED include in nginx.conf.template (nginx aborts if missing),
# and the base compose mounts it — so stage it alongside the template.
cp    "${GATEWAY_SRC}/nginx/ratelimit.conf"       ~/docker/nginx/ratelimit.conf

# writer_tokens.map: seed EMPTY (comment only) → no active writer → gateway is
# READ-ONLY until onboarding provisions a writer token via the brain_sbin tooling.
# (The neuron container writes via the internal brain_net path, not the gateway, so
# a write-disabled gateway is the correct secure default.)
if [ ! -f ~/docker/nginx/writer_tokens.map ]; then
    printf '# No writer tokens provisioned — gateway is read-only.\n# Add via the brain_sbin token tooling.\n' \
        > ~/docker/nginx/writer_tokens.map
fi
# reader_tokens.map: empty include target (only consulted by authz mode C).
[ -f ~/docker/nginx/reader_tokens.map ] || printf '# No reader tokens (authz mode C only).\n' > ~/docker/nginx/reader_tokens.map

echo "== render .env (generate CHROMA_MASTER_TOKEN_FOR_GW if placeholder) =="
# NOTE: for a SHIPPED image, token generation moves to onboard (per-deployment).
# For this live brain we generate here and bake it into the running stack.
if [ ! -f ~/docker/.env ] || grep -q '__GENERATED' ~/docker/.env 2>/dev/null; then
    TOKEN="$(openssl rand -hex 32)"
    cat > ~/docker/.env <<ENV
COMPOSE_PROJECT_NAME=${BRAIN_NAME}
BRAIN_NAME=${BRAIN_NAME}
CHROMA_VERSION=1.5.0
CHROMA_MASTER_TOKEN_FOR_GW=${TOKEN}
GATEWAY_BIND=${GATEWAY_BIND}
GATEWAY_PORT=8000
ENV
    echo "   generated a new CHROMA_MASTER_TOKEN_FOR_GW"
else
    # keep existing token; just update the posture bind
    sed -i "s/^GATEWAY_BIND=.*/GATEWAY_BIND=${GATEWAY_BIND}/" ~/docker/.env
    echo "   kept existing CHROMA_MASTER_TOKEN_FOR_GW; set GATEWAY_BIND=${GATEWAY_BIND}"
fi

echo "== self-signed cert (posture: ${POSTURE}) =="
cd ~/docker
if [ ! -f ~/gateway/gateway_out/cert.pem ]; then
    ./gen-cert.sh                       # personal SAN (localhost, 127.0.0.1)
else
    echo "   ~/gateway/gateway_out/cert.pem exists — keeping (re-run ./gen-cert.sh to rotate)"
fi

# NO STACK BRING-UP HERE — this stage LAYS the stack, it does not run it.
#
# There used to be a `docker compose down`/`up -d` + a status table + three self-checks
# (TLS heartbeat, POST /reset -> 403, plaintext-:8000 sealed probe). They were REMOVED because
# they could not work and were not needed:
#
#   * They CANNOT work. Compose interpolates every service in the file at PARSE time, before
#     profile selection. The neuron services reference ${NEURON_TOKEN__<bundle>__<neuron>:?...},
#     which fails CLOSED by design (gateway_config generate mints them host-side into brain_etc
#     at the gateway stage). At BUILD time those tokens do not exist yet and cannot: the whole
#     `up` aborted with "required variable NEURON_TOKEN__... is missing a value". This script is
#     `set -uo pipefail` (no -e), so the failure was SWALLOWED — the status table printed empty,
#     the three checks silently probed a stack that was never up, and STAGE4_DONE printed anyway.
#     A textbook false green. (It only ever "worked" when the staged compose was the 79-line
#     prototype with no neuron services to interpolate.)
#   * They are NOT needed. Nothing here survives into the engine: cleanup_brain.sh brings the
#     containers down before the export, and the runtime images are baked separately and later
#     by prefetch_images.sh / prefetch_models.sh / prefetch_neurons.sh (direct `docker pull` /
#     `docker build`, which never touch compose). The stack is brought up on the REAL runtime VM
#     by the deploy's gateway stage (reapply_brain_configs.py — ADR-0013 §4's one tool to make
#     the running stack match disk), and the same three assertions are made there, for real, by
#     the deploy's verify stage against a live brain.
#
# NOTE: this narrows the script's old secondary role. Its header once called it "the DEPLOY step
# for an already-live brain" (down old, lay new, up). It now only LAYS the stack + generates the
# cert; use reapply_brain_configs.py to make a live brain match what was laid.

echo "== data dir ownership (must be brain uid 1000) =="
ls -ln /home/${BRAIN_NAME}/chroma_store | head
echo "STAGE4_DONE"
