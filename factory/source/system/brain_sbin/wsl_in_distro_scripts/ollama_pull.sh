#!/bin/sh
# ollama_pull.sh — robustly pull the brain's models, run INSIDE the ollama container.
# ====================================================================================
# Ollama registry pulls over this box's link are intermittently flaky (mid-transfer `EOF`),
# so a single `ollama pull` often dies at 30-60%. This script RETRIES each model until
# `ollama list` confirms it is present, so provisioning is reproducible instead of a coin flip.
#
# It runs where the `ollama` binary lives — INSIDE the ollama container — driven from the distro
# WITHOUT copying anything in (streamed to the container's own /bin/sh over stdin):
#
#   docker exec -i <brain>-ollama sh -s -- <model> [<model> ...]  < ollama_pull.sh
#   docker exec -i <brain>-ollama sh -s                           < ollama_pull.sh   # default set
#
# With no args it pulls the default brain model set (embedder + vision + a small text LLM).
# Exit 0 only when every requested model is present; non-zero (and which model) otherwise.
set -u

# Default model set (override by passing model names as args):
#   nomic-embed-text  — text embedder (input neurons + action-neuron query embedding)
#   moondream         — vision model (images caption strategy)
#   qwen2.5:0.5b      — small text LLM (action-neuron answer synthesis). NOTE: llama3.2:1b is a
#                       better synthesizer but one of its blobs EOFs consistently from this box's
#                       registry link (10/10 attempts); qwen2.5:0.5b pulls cleanly. Swap back to
#                       llama3.2:1b here + in ACTION_LLM_MODEL once the registry cooperates.
DEFAULT_MODELS="nomic-embed-text moondream qwen2.5:0.5b"
MODELS="$*"
[ -n "$MODELS" ] || MODELS="$DEFAULT_MODELS"

MAX_ATTEMPTS=10
SLEEP_BETWEEN=3

have_model() {
    # `ollama list` shows "<name>:<tag>"; match on the base name so "llama3.2:1b" is found
    # whether listed as llama3.2:1b or llama3.2:latest.
    base=$(printf '%s' "$1" | cut -d: -f1)
    ollama list 2>/dev/null | awk '{print $1}' | cut -d: -f1 | grep -qx "$base"
}

rc=0
for m in $MODELS; do
    if have_model "$m"; then
        echo "[ollama_pull] have '$m' already"
        continue
    fi
    n=0
    ok=0
    while [ "$n" -lt "$MAX_ATTEMPTS" ]; do
        n=$((n + 1))
        echo "[ollama_pull] pulling '$m' (attempt $n/$MAX_ATTEMPTS)"
        # A pull may exit non-zero on transient EOF; re-check presence regardless.
        ollama pull "$m" || true
        if have_model "$m"; then
            echo "[ollama_pull] OK '$m'"
            ok=1
            break
        fi
        echo "[ollama_pull] '$m' not complete (likely transient EOF) — retrying in ${SLEEP_BETWEEN}s"
        sleep "$SLEEP_BETWEEN"
    done
    if [ "$ok" -ne 1 ]; then
        echo "[ollama_pull] FAILED '$m' after $MAX_ATTEMPTS attempts"
        rc=1
    fi
done

echo "[ollama_pull] present models:"
ollama list
exit "$rc"
