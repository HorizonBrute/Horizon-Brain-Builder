#!/usr/bin/env bash
# Deliver one host DATA seam (impulses/, knowledge/brain_ro/) onto the distro's ext4 home.
#
# Args:  $1 = host Windows path (the drvfs source)   e.g. C:\install\root\brains\<b>\impulses
#        $2 = in-distro destination (on ext4)        e.g. /home/<b>/impulses
#        $3 = owner user for the delivered tree      e.g. <b>
#
# WHY A TRANSIENT drvfs MOUNT AND NOT /mnt/c:
# The caller used to read the host tree through the /mnt/c automount. Server posture bakes
# `[automount] enabled=false` into wsl.conf (stage7_harden), so /mnt/c does not exist there and
# the copy failed with `cp: cannot stat '/mnt/c/...'` — the input neuron then found no provider
# script and exited 1 while the deploy still reported success. An EXPLICIT drvfs mount does not
# depend on the automount tree and works under BOTH postures, which is why the neuron code-in
# seams (/opt/*_neurons) and the config seam (/opt/brain_truths) already mount this way. One
# path for both postures — no posture branch.
#
# WHY COPY AND NOT JUST MOUNT:
# A nested 9p (drvfs) mount does NOT propagate into rootless docker: a bind-mounted 9p seam
# shows EMPTY inside the neuron container. These are RUNTIME bind mounts, so the bytes must
# land on ext4. The mount is only the SOURCE we copy from, and is released immediately after.
#
# Runs as root: mount/umount/chown require it. The copy is chowned to the brain so the neuron
# container (uid 1000) owns what it bind-mounts.
set -uo pipefail

src_win="${1:?host Windows source path required}"
dst="${2:?in-distro destination required}"
owner="${3:?owner user required}"

mp="$(mktemp -d /mnt/.brain_seam_XXXXXX)" || { echo "ERROR: could not create mountpoint" >&2; exit 1; }

# Always release the mount + scratch dir, on every exit path — a leaked 9p mount would pin the
# host dir and silently poison the next delivery.
cleanup() {
    mountpoint -q "$mp" && umount "$mp"
    rmdir "$mp" 2>/dev/null
    return 0
}
trap cleanup EXIT

if ! mount -t drvfs "$src_win" "$mp" -o ro; then
    echo "ERROR: drvfs mount failed: ${src_win}" >&2
    exit 1
fi

mkdir -p "$dst"
# cp -r merges and removes nothing: re-running a deploy refreshes the shipped fixtures without
# clobbering a git-delivered tree that neuron_deliver.sh wrote alongside them.
if ! cp -r "$mp/." "$dst/"; then
    echo "ERROR: copy failed: ${src_win} -> ${dst}" >&2
    exit 1
fi

chown -R "${owner}:${owner}" "$dst" || { echo "ERROR: chown failed: ${dst}" >&2; exit 1; }

echo "delivered ${src_win} -> ${dst} (owner ${owner})"
