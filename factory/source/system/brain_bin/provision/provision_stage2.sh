#!/usr/bin/env bash
# Stage 2 (run as ROOT in the Ubuntu-24.04 distro, pre-systemd-restart).
# Creates the brain Linux user, subuid/subgid, wsl.conf (systemd + default user),
# installs Docker CE + rootless extras from Docker's official repo. No systemctl
# here (systemd is not up until the distro restarts with systemd=true).
set -euo pipefail
# Brain identity: pass `BRAIN=<name>` (or `BRAIN_NAME=<name>`) — no baked default, so a
# forgotten name fails loud instead of silently provisioning the prototype.
BRAIN="${BRAIN:-${BRAIN_NAME:?set BRAIN or BRAIN_NAME to the target brain name}}"

echo "== [1] create brain user =="
if id "$BRAIN" >/dev/null 2>&1; then
  echo "$BRAIN already exists"
else
  useradd -m -s /bin/bash "$BRAIN"
  echo "created $BRAIN (uid=$(id -u "$BRAIN"))"
fi

echo "== [2] subuid/subgid ranges (needed for rootless user namespaces) =="
grep -q "^$BRAIN:" /etc/subuid || usermod --add-subuids 100000-165535 "$BRAIN"
grep -q "^$BRAIN:" /etc/subgid || usermod --add-subgids 100000-165535 "$BRAIN"
echo "subuid -> $(grep "^$BRAIN:" /etc/subuid)"
echo "subgid -> $(grep "^$BRAIN:" /etc/subgid)"

echo "== [3] /etc/wsl.conf (systemd on, default user = brain) =="
# [interop] appendWindowsPath=false: with automount off (server posture) WSL still
# tries to translate the Windows PATH into /mnt/c on every launch and prints a
# 'wsl: Failed to translate C:\...' line per entry — cosmetic but noisy in every
# run_as_brain call. A sealed brain distro has no need for the Windows PATH, so we
# turn it off here (automount-independent; NOT the same as enabling automount).
cat > /etc/wsl.conf <<EOF
[boot]
systemd=true

[user]
default=$BRAIN

[interop]
appendWindowsPath=false
EOF
cat /etc/wsl.conf

echo "== [4] base packages =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -qq
apt-get install -y -qq ca-certificates curl gnupg uidmap dbus-user-session \
  slirp4netns fuse-overlayfs logrotate >/dev/null
echo "base packages ok"

echo "== [5] Docker official apt repo =="
install -m 0755 -d /etc/apt/keyrings
# Distro-agnostic: Docker publishes parallel repos at /linux/<id> (ubuntu, debian, ...).
# Derive id + codename from os-release so this provisions on any Debian-family base
# (Ubuntu, Debian, eLxr, ...), not just Ubuntu.
. /etc/os-release
curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list

echo "== [6] install Docker CE + compose + rootless extras =="
apt-get update -y -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin docker-ce-rootless-extras >/dev/null
echo "docker version: $(docker --version)"
echo "compose version: $(docker compose version | head -1)"

echo "== [7] linger for the brain (headless user services survive logout) =="
mkdir -p /var/lib/systemd/linger
touch "/var/lib/systemd/linger/$BRAIN"
echo "linger file created for $BRAIN"

echo "== STAGE 2 DONE =="
