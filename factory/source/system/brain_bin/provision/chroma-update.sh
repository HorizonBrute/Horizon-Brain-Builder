#!/usr/bin/env bash
# Chroma auto-update, posture A: always move to the newest STABLE tag.
# Resilient: snapshot before bump, health-check after, auto-rollback on failure.
# Every action is written to the JSON-lines maintenance log.
set -uo pipefail
source "$HOME/bin/brain-jlog.sh"
STACK="$HOME/docker"; ENVF="$STACK/.env"
# Chroma is SEALED behind the TLS gateway (stage4) — there is no plaintext host port to
# probe. Health-check THROUGH the gateway over TLS on the configured port, verifying with
# the stack's own CA. Probing http://127.0.0.1:8000 directly always fails and makes the
# update timer thrash (bump -> "unhealthy" -> rollback -> "unhealthy"), stranding images.
PORT="$(grep -E '^GATEWAY_PORT=' "$ENVF" | cut -d= -f2)"; PORT="${PORT:-8000}"
CACERT="$HOME/gateway/gateway_out/cert.pem"; HB="https://127.0.0.1:${PORT}/api/v2/heartbeat"

hc() { for _ in $(seq 1 15); do curl -fsS --connect-timeout 3 --max-time 10 --cacert "$CACERT" "$HB" >/dev/null 2>&1 && return 0; sleep 2; done; return 1; }
cur="$(grep -E '^CHROMA_VERSION=' "$ENVF" | cut -d= -f2)"

# Resolve newest stable tag (exclude *.dev*, arch-suffixed) across Hub pages.
latest=""; url="https://hub.docker.com/v2/repositories/chromadb/chroma/tags?page_size=100&ordering=last_updated"
for _ in $(seq 1 8); do
  [ -z "$url" ] && break
  resp="$(curl -fsSL --connect-timeout 5 --max-time 25 "$url" 2>/dev/null)" || break
  pt="$(echo "$resp" | jq -r '.results[].name' 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' || true)"
  latest="$(printf '%s\n%s\n' "$latest" "$pt" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)"
  url="$(echo "$resp" | jq -r '.next // empty' 2>/dev/null)"
done

if [ -z "$latest" ]; then
  ( cd "$STACK" && docker compose pull -q >/dev/null 2>&1 && docker compose up -d >/dev/null 2>&1 ) || true
  jlog chroma update_check noop "$cur" "" "could not resolve newest stable; refreshed current tag" ""
  exit 0
fi

target="$(printf '%s\n%s\n' "$cur" "$latest" | sort -V | tail -1)"
if [ "$target" = "$cur" ]; then
  ( cd "$STACK" && docker compose pull -q >/dev/null 2>&1 && docker compose up -d >/dev/null 2>&1 ) || true
  jlog chroma update_check noop "$cur" "$latest" "already newest; refreshed image for rebuilds" ""
  exit 0
fi

# Newer stable exists -> snapshot, then bump.
jlog chroma update_bump start "$cur" "$latest" "pre-update snapshot" ""
"$HOME/bin/chroma-backup.sh" >/dev/null 2>&1 || jlog chroma backup warn "$cur" "$latest" "pre-update backup failed" ""

sed -i "s/^CHROMA_VERSION=.*/CHROMA_VERSION=${latest}/" "$ENVF"
( cd "$STACK" && docker compose pull -q >/dev/null 2>&1 && docker compose up -d >/dev/null 2>&1 )

if hc; then
  jlog chroma update_ok ok "$cur" "$latest" "healthy after bump" ""
  docker image prune -f >/dev/null 2>&1 || true
else
  sed -i "s/^CHROMA_VERSION=.*/CHROMA_VERSION=${cur}/" "$ENVF"
  ( cd "$STACK" && docker compose up -d >/dev/null 2>&1 )
  if hc; then
    jlog chroma update_rollback ok "$latest" "$cur" "bump unhealthy; rolled back" ""
  else
    jlog chroma update_rollback fail "$latest" "$cur" "rollback unhealthy; RESTORE FROM BACKUP" ""
  fi
fi
