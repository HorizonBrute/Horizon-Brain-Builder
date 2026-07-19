#!/usr/bin/env bash
# Consistent Chroma data snapshot -> ~/backups/chroma_<ts>.tar.zst (rotated).
# Stops the container briefly for a consistent copy (acceptable for single-user/indie).
set -uo pipefail
source "$HOME/bin/brain-jlog.sh"
STACK="$HOME/chroma"; DATA="$HOME/chroma_store"; BK="$HOME/backups"; KEEP=7
mkdir -p "$BK"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
art="$BK/chroma_${stamp}.tar.zst"

( cd "$STACK" && docker compose stop >/dev/null 2>&1 ) || true
if tar -C "$DATA" -cf - . 2>/dev/null | zstd -q -o "$art" 2>/dev/null; then
  ( cd "$STACK" && docker compose start >/dev/null 2>&1 ) || ( cd "$STACK" && docker compose up -d >/dev/null 2>&1 )
  # rotate: keep newest $KEEP
  ls -1t "$BK"/chroma_*.tar.zst 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -f
  sz="$(du -h "$art" | cut -f1)"
  jlog chroma backup ok "" "" "kept=$KEEP size=$sz" "$art"
  echo "backup ok: $art ($sz)"
else
  ( cd "$STACK" && docker compose up -d >/dev/null 2>&1 ) || true
  jlog chroma backup fail "" "" "tar/zstd failed" ""
  echo "backup FAILED" >&2
  exit 1
fi
