#!/usr/bin/env bash
# Stage: PRE-BUILD the neuron bundle images into the engine at BUILD time.
# ------------------------------------------------------------------------------
# Runs as the BRAIN user in the networked scratch build distro, BEFORE the export. Builds the
# shared per-role neuron images with the SAME tags the runtime compose references
# (${BRAIN}-input_neurons / ${BRAIN}-action_neurons) so the exported engine already carries
# them and the runtime never builds.
#
# WHY: the neuron Dockerfile does `FROM python:3.12-slim` + `RUN pip install -r requirements.txt`
# — BOTH need network. The brain's per-user WSL2 VM has NO network interface under mirrored, so
# a runtime `docker compose up --build` CANNOT build them (root-caused on the first clean
# from-scratch deploy, 2026-07-13 — [9/10] died resolving python:3.12-slim on [::1]:53). Baking
# a base image alone is NOT enough (pip still needs PyPI); the whole image must be pre-built
# here, where the scratch distro DOES network. Runtime `up` then uses these baked images
# (--pull never, no --build). Nothing large is committed to git — the images ride the transient
# engine tar (gitignored under brains/*).
#
# FAIL LOUD: if an image can't be built, exit nonzero so the FROM-SCRATCH BUILD fails here.
set -uo pipefail

: "${BRAIN:?prefetch_neurons: BRAIN env required (the brain name → image tags)}"
INPUT_CTX="${1:?prefetch_neurons: input neuron context path required}"
ACTION_CTX="${2:?prefetch_neurons: action neuron context path required}"

# Rootless docker was brought up in stage4 and persists; give it a moment to settle.
for _ in $(seq 1 15); do docker info >/dev/null 2>&1 && break; sleep 2; done
if ! docker info >/dev/null 2>&1; then
  echo "prefetch_neurons: rootless docker daemon not reachable — cannot pre-build images" >&2
  exit 1
fi

build_one() {
  local tag="$1" ctx="$2"
  if [ ! -f "${ctx}/Dockerfile" ]; then
    echo "  [skip] no Dockerfile at ${ctx} — bare factory (no neuron source); image ${tag} not baked"
    return 0
  fi
  local ok=0
  for attempt in 1 2 3; do
    echo "  docker build -t ${tag} ${ctx} (attempt ${attempt}/3)"
    # --pull: fetch a fresh python:3.12-slim base (networked build); the pip layer resolves
    # PyPI here so the runtime never has to.
    if docker build --pull -t "${tag}" "${ctx}"; then ok=1; break; fi
    sleep 5
  done
  if [ "$ok" -ne 1 ]; then
    echo "  [ERROR] could not build neuron image ${tag} from ${ctx}" >&2
    return 1
  fi
}

echo "== prefetch: pre-building neuron images for ${BRAIN} =="
fail=0
build_one "${BRAIN}-input_neurons"  "${INPUT_CTX}"  || fail=1
build_one "${BRAIN}-action_neurons" "${ACTION_CTX}" || fail=1

echo "== neuron images baked =="
docker images --format '  {{.Repository}}:{{.Tag}} ({{.Size}})' \
  | grep -E -- "-(input|action)_neurons" || true

if [ "$fail" -ne 0 ]; then
  echo "prefetch_neurons: FAILED — a neuron image could not be pre-built." >&2
  echo "  The runtime brain VM has no network under mirrored, so these MUST be baked in now." >&2
  exit 1
fi
echo NEURONS_PREFETCH_DONE
