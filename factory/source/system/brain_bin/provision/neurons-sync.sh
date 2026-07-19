#!/usr/bin/env bash
# neurons-sync.sh — the LEGACY code-in seam applier. Runs as ROOT (the deployer identity),
# NEVER as the brain. Force one-way sync of the runtime code copy from upstream, so
# the brain executes code it cannot write, and the code cannot drift (main wins).
# Transport is pluggable via /etc/neurons-sync.conf. See ../brain_security_model.md.
#
# ADR-0015: SUPERSEDED for the default deploy by system/brain_sbin/neurons_mount.py, which mounts
# the host input_neurons/ + action_neurons/ dirs RO into the distro (the neuron images are
# built from those). This timer stays as an OPTIONAL operator seam (TRANSPORT=none by
# default => inert) for setups that prefer a git/rsync pull over the host-dir mount.
set -uo pipefail
CONF=/etc/neurons-sync.conf
NEURONS=/opt/input_neurons
TRANSPORT=none; UPSTREAM=; BRANCH=main
[ -f "$CONF" ] && . "$CONF"

log() { echo "[neurons-sync $(date -u +%FT%TZ)] $*"; }

case "$TRANSPORT" in
  none)
    log "TRANSPORT=none — code-in seam inert (neurons_mount.py owns /opt/input_neurons); nothing to do"; exit 0 ;;
  git)
    [ -n "$UPSTREAM" ] || { log "no UPSTREAM set; skipping"; exit 0; }
    if [ -d "$NEURONS/.git" ]; then
      git -C "$NEURONS" fetch --prune origin "$BRANCH" || { log "fetch failed"; exit 1; }
      git -C "$NEURONS" reset --hard "origin/$BRANCH"  || { log "reset failed"; exit 1; }
    else
      # first sync: clone into a fresh dir, then swap into place
      tmp="$(mktemp -d)"
      git clone --depth 1 --branch "$BRANCH" "$UPSTREAM" "$tmp/neurons" \
        || { log "clone failed"; rm -rf "$tmp"; exit 1; }
      rm -rf "$NEURONS"; mv "$tmp/neurons" "$NEURONS"; rm -rf "$tmp"
    fi ;;
  rsync)
    [ -n "$UPSTREAM" ] || { log "no UPSTREAM set; skipping"; exit 0; }
    rsync -a --delete "$UPSTREAM"/ "$NEURONS"/ || { log "rsync failed"; exit 1; } ;;
  *)
    log "unknown TRANSPORT=$TRANSPORT"; exit 2 ;;
esac

# Re-assert execute-only ownership after every sync (belt and suspenders: even a
# successful write by anyone else is reverted to deployer-owned, brain read+execute).
chown -R root:root "$NEURONS"
find "$NEURONS" -type d -exec chmod 0755 {} +
find "$NEURONS" -type f -exec chmod 0644 {} +
# entrypoints stay executable
find "$NEURONS" -type f \( -name '*.sh' -o -name '*.py' \) -exec chmod 0755 {} + 2>/dev/null || true
log "synced ($TRANSPORT) and re-asserted root:root execute-only"
