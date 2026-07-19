#!/usr/bin/env bash
# neuron_build.sh — build BOTH neuron role images (deps substrate) from their code-in seams.
#   input  neurons: context=/opt/input_neurons  -> <brain>-input_neurons
#   action neurons: context=/opt/action_neurons -> <brain>-action_neurons
# Tests the 9p build-context read path. Code is baked from the seam at build time.
set -eu
cd "$HOME/docker"
brain="$(basename "$HOME")"
# A neuron depends_on gateway (which depends_on chroma); ollama/gateway/fail2ban are
# profile-gated, so their profiles must be active for a valid project graph even at build
# time. All input neurons share the ONE -input_neurons image and all action neurons the ONE
# -action_neurons image; exactly two compose services carry a `build:` context (the default
# bundle's first input + first action neuron, generated from the brain.env zone by
# neuron_compose.py). `docker compose build` with NO service arg builds precisely those
# build-context services — zone-agnostic, so it needs no default-bundle / neuron-name math.
profiles="--profile ollama --profile gateway --profile fail2ban --profile neurons"
echo "== building ${brain}-input_neurons + ${brain}-action_neurons (contexts over 9p) =="
docker compose $profiles build
echo
echo "== images =="
docker images "${brain}-input_neurons" --format '  {{.Repository}}:{{.Tag}}  {{.Size}}  {{.CreatedSince}}'
docker images "${brain}-action_neurons" --format '  {{.Repository}}:{{.Tag}}  {{.Size}}  {{.CreatedSince}}'
echo "== DONE =="
