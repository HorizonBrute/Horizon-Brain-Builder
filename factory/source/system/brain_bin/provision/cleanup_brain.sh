#!/usr/bin/env bash
# Pre-export cleanup (BRAIN user): pristine engine, keep config + current image.
set -uo pipefail
cd "$HOME/docker"
docker compose down >/dev/null 2>&1 || true
# Keep ONLY the pinned Chroma image (from .env); prune any other tag (e.g. a failed-update
# leftover), then ensure the pinned one is present so the exported engine is self-contained.
# (Was: a hard-coded `docker rmi chromadb/chroma:1.5.0` that removed the CURRENT image and
#  left stray versions behind — see LIFECYCLE-01 RUN 002 Finding 3.)
VER="$(grep -oP 'CHROMA_VERSION=\K.*' "$HOME/docker/.env" 2>/dev/null | tr -d '\r')"
if [ -n "$VER" ]; then
  docker images chromadb/chroma --format '{{.Repository}}:{{.Tag}}' \
    | grep -v ":${VER}$" | xargs -r docker rmi >/dev/null 2>&1 || true
  docker pull "chromadb/chroma:${VER}" >/dev/null 2>&1 || true
fi
rm -f "$HOME"/backups/*.tar.zst 2>/dev/null || true
: > "$HOME/logs/brain-maintenance.jsonl" 2>/dev/null || true

echo "== engine state for export =="
echo "chroma .env: $(grep CHROMA_VERSION "$HOME/docker/.env")"
echo "images cached:"; docker images --format '  {{.Repository}}:{{.Tag}} ({{.Size}})'
echo "chroma_store:"; ls -la "$HOME/chroma_store" 2>/dev/null | tail -n +1 | head
echo "backups (should be empty):"; ls -la "$HOME/backups" 2>/dev/null | tail -n +2
echo "timers still enabled:"; systemctl --user is-enabled chroma-backup.timer chroma-update.timer 2>&1 | tr '\n' ' '; echo
echo CLEANUP_DONE
