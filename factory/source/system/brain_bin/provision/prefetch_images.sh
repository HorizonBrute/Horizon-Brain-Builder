#!/usr/bin/env bash
# Stage: PREFETCH the runtime container images into the engine at BUILD time.
# ------------------------------------------------------------------------------
# Runs as the BRAIN user in the networked scratch build distro, AFTER cleanup_brain.sh
# (containers already down → clean export). Pulls the exact PUBLIC image tags the RUNTIME
# will use — passed as args, resolved by the deploy orchestrator from brain.env *_VERSION
# knobs (default :latest) so build and runtime agree — and bakes them into the exported
# engine tar.
#
# WHY: the brain's per-user WSL2 VM comes up with NO network interface under mirrored
# networking (a per-user-VM limitation), so a runtime `docker compose pull` cannot reach
# Docker Hub. Seeding the images HERE — where the scratch distro DOES have network — makes
# the runtime pull a no-op (`--pull missing` finds everything present). Nothing large is
# committed to git: the images ride the TRANSIENT engine tar (gitignored under brains/*),
# pulled live from public registries at build time.
#
# FAIL LOUD: if any image cannot be pulled, exit nonzero so the FROM-SCRATCH BUILD fails
# here — network/registry problems surface at build, never as a silently broken runtime.
set -uo pipefail

refs=("$@")
if [ "${#refs[@]}" -eq 0 ]; then
  echo "prefetch_images: no image refs supplied" >&2
  exit 2
fi

# Rootless docker was brought up in stage4 and persists in this distro; give it a moment
# in case cleanup's `compose down` left the daemon settling.
for _ in $(seq 1 15); do docker info >/dev/null 2>&1 && break; sleep 2; done
if ! docker info >/dev/null 2>&1; then
  echo "prefetch_images: rootless docker daemon not reachable — cannot seed images" >&2
  exit 1
fi

echo "== prefetch: seeding ${#refs[@]} runtime image(s) into the engine =="
fail=0
for ref in "${refs[@]}"; do
  pulled=0
  for attempt in 1 2 3; do
    echo "  pull ${ref} (attempt ${attempt}/3)"
    if docker pull "$ref"; then pulled=1; break; fi
    sleep 5
  done
  if [ "$pulled" -ne 1 ]; then
    echo "  [ERROR] could not pull ${ref} from the public registry" >&2
    fail=1
    continue
  fi
  if ! docker image inspect "$ref" >/dev/null 2>&1; then
    echo "  [ERROR] ${ref} reports pulled but is not present locally" >&2
    fail=1
  fi
done

echo "== images cached in the engine =="
docker images --format '  {{.Repository}}:{{.Tag}} ({{.Size}})'

if [ "$fail" -ne 0 ]; then
  echo "prefetch_images: FAILED — one or more runtime images could not be seeded." >&2
  echo "  The runtime brain VM has no network under mirrored, so these MUST be baked in now." >&2
  exit 1
fi
echo PREFETCH_DONE
