#!/usr/bin/env bash
# Stage 3 (run as the BRAIN user). Installs rootless Docker for this user.
set -uo pipefail
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export PATH=/usr/bin:/sbin:/usr/sbin:$PATH
unset DOCKER_HOST 2>/dev/null || true

echo "user=$(whoami) uid=$(id -u) XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
echo "user systemd: $(systemctl --user is-system-running 2>&1 | head -1)"

echo "== rootless setup =="
dockerd-rootless-setuptool.sh install 2>&1 | tail -20

# --- ADR 0012 §5(1): pin the rootless port-driver to slirp4netns -----------------------
# RootlessKit's DEFAULT 'builtin' port driver MASQUERADES the external source IP — every LAN
# client is logged by nginx as the docker bridge gateway (e.g. 172.x.x.1), which makes the
# gateway's fail2ban banning INERT (it can never see, hence never ban, a real attacker).
# 'slirp4netns' preserves the true client IP. This MUST live in canon: it was previously
# hand-applied during the ADR-0012 proof and silently lost on the next from-scratch redeploy,
# regressing real-IP fidelity to 0%. A systemd --user drop-in so it survives restarts AND every
# redeploy. Pairs with .wslconfig networkingMode=mirrored (ADR 0012 §5(2), host-level).
echo "== pin rootless port-driver -> slirp4netns (ADR 0012 s5: real client IP for fail2ban) =="
mkdir -p ~/.config/systemd/user/docker.service.d
cat > ~/.config/systemd/user/docker.service.d/port-driver.conf <<'EOF'
[Service]
Environment="DOCKERD_ROOTLESS_ROOTLESSKIT_PORT_DRIVER=slirp4netns"
EOF
systemctl --user daemon-reload 2>&1 | tail -1 || true
echo "wrote docker.service.d/port-driver.conf (slirp4netns)"

echo "== enable + (re)start user docker service =="
systemctl --user enable docker 2>&1 | tail -1 || true
# restart (not start): the setuptool install above may already have started dockerd, and a
# no-op 'start' would NOT re-read the drop-in env — restart guarantees rootlesskit picks up
# --port-driver=slirp4netns (starts it cleanly if it isn't running yet).
systemctl --user restart docker 2>&1 | tail -1 || true
sleep 3
echo "docker user service active: $(systemctl --user is-active docker 2>&1)"
echo "rootlesskit port-driver: $(pgrep -af rootlesskit | grep -oE 'port-driver=[A-Za-z0-9]+' | head -1 || echo 'NOT-RUNNING-YET')"

# NOTE: ~/.bashrc covers INTERACTIVE shells only. Non-interactive login shells
# (`bash -lc "..."`, e.g. via run_as_brain --wsl and the gateway deploy) get
# DOCKER_HOST from the root-owned /etc/profile.d/10-brain-rootless-docker.sh drop-in
# (written in stage2b) — Ubuntu's ~/.bashrc `return`s early when non-interactive, so
# these appended exports would otherwise be invisible to the bridge. Keep both.
echo "== persist env in ~/.bashrc (interactive shells) =="
if ! grep -q 'DOCKER_HOST' ~/.bashrc 2>/dev/null; then
cat >> ~/.bashrc <<'EOF'

# Rootless Docker (brain-owned engine)
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export PATH=/usr/bin:$PATH
export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
EOF
echo "added env to ~/.bashrc"
else
echo "~/.bashrc already has DOCKER_HOST"
fi

echo "== verify (rootless) =="
export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
docker version --format 'client={{.Client.Version}} / server={{.Server.Version}}' 2>&1 | head -1
echo "rootless line: $(docker info 2>/dev/null | grep -i rootless || echo NOT-FOUND)"
echo "context: $(docker context show 2>/dev/null)"
echo STAGE3_DONE
