#!/usr/bin/env bash
# Stage 2b (run as ROOT, after the systemd restart).
set -uo pipefail
# Brain identity: pass `BRAIN=<name>` (or `BRAIN_NAME=<name>`) — no baked default, so a
# forgotten name fails loud instead of silently provisioning the prototype.
BRAIN="${BRAIN:-${BRAIN_NAME:?set BRAIN or BRAIN_NAME to the target brain name}}"

echo "pid1=$(cat /proc/1/comm)"
echo "system-running: $(systemctl is-system-running 2>&1)"

echo "--- disable the rootful docker daemon (we run rootless only) ---"
systemctl disable --now docker.service docker.socket 2>&1 | tail -3 || true
echo "docker.service enabled? $(systemctl is-enabled docker.service 2>&1)"
echo "docker.service active?  $(systemctl is-active docker.service 2>&1)"

echo "--- linger status for $BRAIN ---"
loginctl show-user "$BRAIN" 2>/dev/null | grep -i linger || echo "(user manager not shown yet)"

echo "--- user runtime dir ---"
ls -ld "/run/user/$(id -u "$BRAIN")" 2>&1 || echo "no /run/user/<uid> yet"

echo "--- login-shell env for rootless Docker (covers NON-interactive login shells) ---"
# WHY: run_as_brain --wsl runs `bash -lc "<cmd>"` — a NON-interactive LOGIN shell.
# Ubuntu's ~/.bashrc returns early for non-interactive shells, so the DOCKER_HOST /
# XDG_RUNTIME_DIR that stage3 appends to ~/.bashrc is NOT seen by `bash -lc "docker ..."`
# (the shape the bridge + the gateway deploy use). A /etc/profile.d drop-in is sourced
# by /etc/profile for EVERY login shell regardless of interactivity, so the brain's
# rootless socket is reachable THROUGH the bridge. Guarded to the brain user so root/
# other logins are unaffected.
cat > /etc/profile.d/10-brain-rootless-docker.sh <<EOF
# Brain rootless-Docker env for login shells (interactive AND non-interactive).
# Written by stage2b_root.sh; see system/brain_bin/provision/README.md.
if [ "\$(id -un)" = "$BRAIN" ]; then
    export XDG_RUNTIME_DIR="/run/user/\$(id -u)"
    export DOCKER_HOST="unix:///run/user/\$(id -u)/docker.sock"
fi
EOF
chmod 0644 /etc/profile.d/10-brain-rootless-docker.sh
echo "wrote /etc/profile.d/10-brain-rootless-docker.sh"

echo STAGE2B_DONE
