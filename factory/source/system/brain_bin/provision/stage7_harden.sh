#!/usr/bin/env bash
# Stage 7 (run as ROOT in the distro). Apply the brain HARDENING posture — the
# "standard shape" from ../brain_security_model.md. Idempotent; safe to re-run.
#
# Usage: stage7_harden.sh [personal|server]   (default: personal)
#
# Secure by default: there is NO lax "dev" posture. The brain never writes its own
# code in any posture — iterate on neurons through the code-in seam (point
# /etc/neurons-sync.conf at your working upstream and let the deployer force-sync),
# not by making /opt/input_neurons writable.
#
# ADR-0015 NOTE: neurons are now CONTAINER BUNDLES built from the host input_neurons/
# + action_neurons/ dirs, delivered at DEPLOY by system/brain_sbin/neurons_mount.py (a RO drvfs
# mount of those host dirs at /opt/input_neurons + /opt/action_neurons). The git/rsync
# neurons-sync timer below is the LEGACY single-neuron code-in seam — kept (renamed off
# the retired /opt/neurons) as an OPTIONAL operator seam, TRANSPORT=none by default so it
# is inert unless an operator opts in. The deploy path does not rely on it.
#
# What it enforces (all postures):
#   - runtime brain uid is NON-privileged (never sudo)                    -> invariant #6
#   - /opt/input_neurons is execute-only for the brain (deployer writes)  -> invariant #7
#   - an OPTIONAL code-in sync seam (root system timer), inert by default -> invariant #7
# Posture screws: see the case block near the bottom.
set -euo pipefail
# Brain identity: pass `BRAIN=<name>` (or `BRAIN_NAME=<name>`) in the env — there is NO
# baked default, so a forgotten name fails loud instead of silently provisioning the
# prototype. ($1 is POSTURE, so the name comes from the environment, not a positional.)
BRAIN="${BRAIN:-${BRAIN_NAME:?set BRAIN or BRAIN_NAME to the target brain name}}"
POSTURE="${1:-personal}"
HERE="$(cd "$(dirname "$0")" && pwd)"
NEURONS=/opt/input_neurons    # runtime code copy the brain EXECUTES (never writes)

echo "== [harden] posture=$POSTURE =="

# 1. De-privilege the runtime brain uid (the recipe never grants sudo; make it
#    explicit + fail loud if it ever drifted in).
if id -nG "$BRAIN" | tr ' ' '\n' | grep -qx sudo; then
  echo "  removing $BRAIN from sudo (runtime brain must be non-privileged)"
  gpasswd -d "$BRAIN" sudo || deluser "$BRAIN" sudo || true
fi
rm -f "/etc/sudoers.d/$BRAIN" 2>/dev/null || true
echo "  $BRAIN groups: $(id -nG "$BRAIN")"

# 2. Code run-copy: root-owned, brain read+execute, NO write.
install -d -o root -g root -m 0755 "$NEURONS"
echo "  $NEURONS = root:root 0755 (brain uid: read+execute, no write)"

# 3. Code-in seam: the deployer force-sync as a root SYSTEM timer (mirrors the
#    Chroma user timers, one privilege tier up — the brain is not the puller).
install -m 0755 "$HERE/neurons-sync.sh" /usr/local/sbin/neurons-sync.sh
if [ ! -f /etc/neurons-sync.conf ]; then
  cat > /etc/neurons-sync.conf <<'EOF'
# Transport for the code-in seam. The DEPLOYER pulls; the brain never does.
TRANSPORT=none            # git | rsync | none   (none = operator syncs manually)
UPSTREAM=                 # e.g. https://…/neurons.git  or  rsync source
BRANCH=main               # git only; force-reset to origin/BRANCH (main always wins)
EOF
  echo "  wrote /etc/neurons-sync.conf (TRANSPORT=none until an operator sets it)"
fi
cat > /etc/systemd/system/neurons-sync.service <<EOF
[Unit]
Description=Brain code-in seam — force-sync /opt/input_neurons from upstream (deployer, not brain)
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/neurons-sync.sh
EOF
cat > /etc/systemd/system/neurons-sync.timer <<'EOF'
[Unit]
Description=Periodic code-in sync
[Timer]
OnCalendar=*-*-* *:00/15:00
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now neurons-sync.timer 2>&1 | tail -1 || true

# 3b. brain-truths config-exposure seam: the host's brain_etc/ (admin-owned config,
#     see system/brain_sbin/brain_truths.py) mounted READ-ONLY into the distro. One root
#     SYSTEM .mount unit, durable across reboot (NOT hand-run, NOT fstab). The brain
#     reads /opt/brain_truths but cannot write it (-o ro). The apply primitive is NOT
#     installed here — it rides in ON the mount (brain_etc/scripts/), reachable at
#     /opt/brain_truths/scripts/ once the source is seeded + mounted at deploy.
install -d -m 0755 /opt/brain_truths
# Host path of THIS brain's brain_etc, spelled as the HOST spells it (it becomes the
# drvfs mount source below). The orchestrator MUST pass `BRAIN_ETC_HOST=<host path>` in
# the env — there is NO baked default, for the same reason BRAIN has none above: the
# install root cannot be guessed, and a wrong guess mounts the wrong config read-only
# over this brain's truths.
# (the :? message carries no nested ${...}, no apostrophe and no backslash: bash parses
#  quotes, expansions and escapes INSIDE the word, so any of them mangles it or breaks
#  the script at parse time. The example path is therefore spelled with forward slashes.)
BRAIN_ETC_HOST="${BRAIN_ETC_HOST:?set BRAIN_ETC_HOST to the host-spelled brain_etc path of this brain, e.g. C:/install/root/brains/<brain>/brain_etc}"
cat > /etc/systemd/system/opt-brain_truths.mount <<EOF
[Unit]
Description=Brain truths - host brain_etc exposed read-only
After=local-fs.target
[Mount]
What=${BRAIN_ETC_HOST}
Where=/opt/brain_truths
Type=drvfs
Options=ro
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
# enable now; --now may no-op if brain_etc isn't seeded yet (deploy seeds it), but
# the unit is enabled so it mounts at every boot once the source exists.
systemctl enable --now opt-brain_truths.mount 2>&1 | tail -1 || true
echo "  brain-truths: /opt/brain_truths <- ${BRAIN_ETC_HOST} (ro); apply primitive installed"

# 4. Posture screws.
case "$POSTURE" in
  personal)
    echo "  posture=personal: code execute-only; automount on; egress open"
    ;;
  server)
    # 4a. Pull up the host-fs bridge: no /mnt/c reach for the agent.
    if ! grep -q '^\[automount\]' /etc/wsl.conf 2>/dev/null; then
      printf '\n[automount]\nenabled=false\n' >> /etc/wsl.conf
      echo "  posture=server: /etc/wsl.conf automount disabled (needs wsl --terminate to apply)"
    fi
    # 4b. Egress allowlist scaffold — operator fills in reachable hosts. Left as a
    #     template (not applied live) so hardening never accidentally cuts the brain off.
    if [ ! -f /etc/neurons-egress.allow ]; then
      cat > /etc/neurons-egress.allow <<'EOF'
# One host/CIDR per line the brain runtime is permitted to egress to.
# Apply with your firewall of choice (nftables/iptables). Deny all else.
# Typical: the RAG (localhost), approved model endpoints, the sync upstream.
127.0.0.1
EOF
      echo "  posture=server: wrote /etc/neurons-egress.allow scaffold (operator applies fw rules)"
    fi
    ;;
  *)
    echo "  [ERROR] unknown posture: $POSTURE (want personal|server)"; exit 2 ;;
esac

echo "== STAGE 7 DONE (posture=$POSTURE) =="
