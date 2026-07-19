#!/usr/bin/env bash
# apply_brain_truths.sh — sync host-authored config INTO the running stack, safely.
#
# THE ONE PLACE the mount->runtime copy happens. Every tool that needs fresh config
# (the boot keepalive, a gateway reload, a token change) calls THIS first — nobody
# hand-rolls a cp. That is what makes "admin edits on the host, then runs a tool"
# race-free: the copy is step 1 of the action, synchronous, fail-loud.
#
# Model: brain_etc/ on the host is the source of truth, exposed read-only at
# /opt/brain_truths (drvfs -o ro). This copies each file named in the manifest to
# its runtime location on ext4 (the stack reads the ext4 copy, never the 9p mount).
#
# Contract:
#   * mount missing / manifest missing      -> abort, touch nothing running.
#   * a copy fails                           -> restore every file already touched, abort.
#   * optional ACTION ("$@") fails afterward -> restore every touched file, abort.
# So the stack is never left on a half-applied config.
#
# Usage:  apply_brain_truths.sh [-- <action to run after a good sync>]
#   e.g.  apply_brain_truths.sh -- docker compose -f ~/docker/compose.yaml up -d --force-recreate gateway
set -uo pipefail

# BRAIN_TRUTHS_MOUNT overrides the mount root (tests only; default is the real mount).
MOUNT="${BRAIN_TRUTHS_MOUNT:-/opt/brain_truths}"
MANIFEST="${MOUNT}/wsl/apply.manifest"

die() { echo "apply_brain_truths: ERROR: $*" >&2; exit 1; }

# Split off an optional post-sync action after `--`.
ACTION=()
if [ "${1:-}" = "--" ]; then shift; ACTION=("$@"); fi

# 1. Preconditions — the source must actually be mounted and readable. (In test mode,
#    BRAIN_TRUTHS_MOUNT set, we skip the mountpoint assertion since it's a plain dir.)
if [ -z "${BRAIN_TRUTHS_MOUNT:-}" ]; then
  mountpoint -q "$MOUNT" || die "$MOUNT is not mounted (the brain-truths RO mount is down)."
fi
[ -r "$MANIFEST" ]     || die "manifest not readable: $MANIFEST"

# 2. Sync, remembering what we touched so we can roll back.
BACKUP="$(mktemp -d)"; trap 'rm -rf "$BACKUP"' EXIT
declare -a TOUCHED=()   # "dst\tbackup-or-NEW"
rollback() {
  echo "apply_brain_truths: rolling back ${#TOUCHED[@]} change(s)..." >&2
  for rec in "${TOUCHED[@]}"; do
    dst="${rec%%$'\t'*}"; bak="${rec#*$'\t'}"
    if [ "$bak" = "NEW" ]; then rm -f "$dst"; else cp -p "$bak" "$dst"; fi
  done
}

n=0
while IFS=$'\t' read -r src dst; do
  # CRLF-safe: the manifest is authored on Windows and may carry a trailing '\r' on the
  # LAST field. Left in, dst becomes '<path>\r' and every synced file lands at a PHANTOM
  # '<name>\r' path — the running stack never sees the update (root-caused 2026-07-04;
  # see objective 008). Strip a trailing CR from both fields defensively.
  src="${src%$'\r'}"; dst="${dst%$'\r'}"
  [ -z "${src:-}" ] && continue
  case "$src" in \#*) continue ;; esac
  full_src="${MOUNT}/${src}"
  [ -r "$full_src" ] || { rollback "${#TOUCHED[@]}"; die "source missing on mount: $src"; }
  mkdir -p "$(dirname "$dst")"
  if [ -e "$dst" ]; then bak="${BACKUP}/$(printf '%s' "$dst" | tr '/' '_')"; cp -p "$dst" "$bak"
  else bak="NEW"; fi
  # atomic replace: stage beside the dest, then mv
  tmp="${dst}.brain_truths.$$"
  if ! cp "$full_src" "$tmp" || ! mv "$tmp" "$dst"; then
    rm -f "$tmp"; TOUCHED+=("${dst}"$'\t'"${bak}"); rollback "${#TOUCHED[@]}"
    die "copy failed: $src -> $dst"
  fi
  TOUCHED+=("${dst}"$'\t'"${bak}")
  n=$((n+1))
done < "$MANIFEST"
echo "apply_brain_truths: synced $n file(s) from $MOUNT."

# 3. Optional action — validate by doing. Roll back the config if it fails.
if [ "${#ACTION[@]}" -gt 0 ]; then
  echo "apply_brain_truths: running post-sync action: ${ACTION[*]}"
  if ! "${ACTION[@]}"; then
    rollback "${#TOUCHED[@]}"
    die "post-sync action failed — config rolled back, stack left on the prior copy."
  fi
fi
echo "apply_brain_truths: OK."
