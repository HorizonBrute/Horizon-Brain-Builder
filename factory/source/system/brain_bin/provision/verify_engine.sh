#!/usr/bin/env bash
set -uo pipefail
echo "whoami=$(whoami)"
echo "DOCKER_HOST=${DOCKER_HOST:-<unset>}"
echo "daemon reachable: server=$(docker version --format '{{.Server.Version}}' 2>&1)"
echo "rootless: $(docker info 2>/dev/null | grep -i rootless || echo NOT-FOUND)"
echo "running containers: $(docker ps --format '{{.Names}}' 2>&1 | tr '\n' ' ')"
DPID=$(cat /run/user/1000/docker.pid 2>/dev/null || echo '')
if [ -n "$DPID" ]; then
  echo "dockerd pid=$DPID owner=$(ps -o user= -p "$DPID" 2>/dev/null | tr -d ' ')"
fi

echo "== hardening posture (invariants #6/#7) =="
# Runs as the brain user; default the name to the current Linux user (override via BRAIN=).
BRAIN="${BRAIN:-$(id -un)}"
id -nG "$BRAIN" 2>/dev/null | tr ' ' '\n' | grep -qx sudo \
  && echo "  sudo:            FAIL - $BRAIN is in sudo (runtime must be non-privileged)" \
  || echo "  sudo:            ok - $BRAIN non-sudo"
echo "  /opt/input_neurons:  $(stat -c '%U:%G %a' /opt/input_neurons 2>/dev/null || echo '<not present>') (expect root:root 755 or RO mount)"
[ -e /opt/input_neurons ] && { [ -w /opt/input_neurons ] \
  && echo "  code writable:   FAIL - brain can write its own code" \
  || echo "  code writable:   ok - brain cannot write /opt/input_neurons"; }
echo "  /opt/action_neurons: $(stat -c '%U:%G %a' /opt/action_neurons 2>/dev/null || echo '<not present>') (expect root:root 755 or RO mount)"
echo "  code-in seam:    $(systemctl is-enabled neurons-sync.timer 2>/dev/null || echo '<not installed>') / TRANSPORT=$(. /etc/neurons-sync.conf 2>/dev/null; echo "${TRANSPORT:-?}")"
echo "  automount:       $(awk '/\[automount\]/{f=1} f&&/enabled/{print $0; f=0}' /etc/wsl.conf 2>/dev/null || echo 'default(on)')"
