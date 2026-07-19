#!/usr/bin/env bash
# neuron_deliver.sh — the git-delivery WRITE phase wrapper (ADR-0015 / brain_etc/github).
# ======================================================================================
# Delivery is the ONE write-capable step: it clones/refreshes git sources INTO brain_ro so
# the (read-only) ingest phase can then embed them. It runs as a SEPARATE one-off, never on
# the long-lived ingest service:
#
#     docker compose ... run --rm --no-deps \
#         -e GITHUB_TOKEN="$transient" <bundle>_input_1 \
#         /app/delivery/neuron_deliver.sh --tags daily
#
# A transient token (github.env GITHUB_TOKEN_ENV, default GITHUB_TOKEN) is injected ONLY here
# and discarded with the container — it is never in the ingest service's env. `auth: public`
# and `auth: operator-delivered` sources need no token. All args pass straight through to the
# neuron's --deliver-only phase (so --tags / --source narrow which sources are fetched).
set -euo pipefail
exec python /app/neuron.py --deliver-only "$@"
