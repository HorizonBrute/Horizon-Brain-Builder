#!/usr/bin/env bash
# Stage: BAKE the ollama roster models into the engine at BUILD time.
# ------------------------------------------------------------------------------
# Runs as the BRAIN user in the networked scratch build distro, AFTER prefetch_images.sh
# (which baked ollama/ollama), and BEFORE the export. Pulls the roster models the RUNTIME
# needs (brain_etc/ollama/models — passed as args by the deploy orchestrator) into the SAME
# named docker volume the runtime ollama container mounts, so they survive `wsl --export`.
#
# WHY: the brain's per-user WSL2 VM comes up with NO network interface under mirrored
# networking, so a runtime `ollama pull` cannot reach registry.ollama.ai (root-caused on the
# first clean from-scratch deploy, 2026-07-13 — [8/10] model sync EOF'd on 127.0.0.11:53).
# Container IMAGES are baked by prefetch_images.sh; MODELS live in the ollama data volume, so
# they need their OWN build-time seed: a throwaway ollama server pulls them into the volume.
#
# The volume name MUST match compose.yaml's `${BRAIN_NAME}_ollama_models` and the mount path
# MUST match its `ollama_models:/root/.ollama`, or the runtime ollama won't see the models.
#
# FAIL LOUD: if any roster model can't be pulled, exit nonzero so the FROM-SCRATCH BUILD fails
# here — never as a silently model-less runtime whose /ask 404s.
set -uo pipefail

: "${BRAIN:?prefetch_models: BRAIN env required (the brain name → volume name)}"
models=("$@")
if [ "${#models[@]}" -eq 0 ]; then
  echo "prefetch_models: no roster models supplied — nothing to bake"
  echo MODELS_PREFETCH_DONE
  exit 0
fi

VOL="${BRAIN}_ollama_models"   # MUST equal compose.yaml volumes.ollama_models.name

# Rootless docker was brought up in stage4 and persists; give it a moment to settle.
for _ in $(seq 1 15); do docker info >/dev/null 2>&1 && break; sleep 2; done
if ! docker info >/dev/null 2>&1; then
  echo "prefetch_models: rootless docker daemon not reachable — cannot bake models" >&2
  exit 1
fi

# Use the EXACT ollama image prefetch_images.sh already baked (honors a pinned OLLAMA_VERSION),
# so build and runtime run the same server.
IMG="$(docker images --format '{{.Repository}}:{{.Tag}}' | grep '^ollama/ollama:' | head -1)"
if [ -z "$IMG" ]; then
  echo "prefetch_models: no ollama/ollama image present — prefetch_images.sh must run first" >&2
  exit 1
fi

# The runtime mounts this named volume at /root/.ollama; pull INTO it so the models ride the
# exported engine and the NIC-less runtime never pulls. Reuse an existing volume idempotently.
docker volume create "$VOL" >/dev/null

SEED="brain-build-ollama-seed-${BRAIN}"
docker rm -f "$SEED" >/dev/null 2>&1 || true
cleanup() { docker rm -f "$SEED" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== prefetch: baking ${#models[@]} ollama model(s) into volume ${VOL} (server ${IMG}) =="
docker run -d --name "$SEED" -v "${VOL}:/root/.ollama" "$IMG" >/dev/null

# Wait for the ollama server inside the seed container to answer.
ready=0
for _ in $(seq 1 30); do
  if docker exec "$SEED" ollama list >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done
if [ "$ready" -ne 1 ]; then
  echo "prefetch_models: ollama server not ready in the seed container" >&2
  exit 1
fi

fail=0
for m in "${models[@]}"; do
  pulled=0
  for attempt in 1 2 3; do
    echo "  ollama pull ${m} (attempt ${attempt}/3)"
    if docker exec "$SEED" ollama pull "$m"; then pulled=1; break; fi
    sleep 5
  done
  if [ "$pulled" -ne 1 ]; then
    echo "  [ERROR] could not pull model ${m} from the registry" >&2
    fail=1
  fi
done

echo "== models baked into ${VOL} =="
docker exec "$SEED" ollama list || true

if [ "$fail" -ne 0 ]; then
  echo "prefetch_models: FAILED — one or more roster models could not be baked." >&2
  echo "  The runtime brain VM has no network under mirrored, so these MUST be baked in now." >&2
  exit 1
fi
echo MODELS_PREFETCH_DONE
