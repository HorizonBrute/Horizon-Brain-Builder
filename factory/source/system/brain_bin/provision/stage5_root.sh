#!/usr/bin/env bash
# Stage 5a (ROOT): OS + Docker-engine auto-update policy (keep current by design).
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "== install unattended-upgrades =="
apt-get update -y -qq
apt-get install -y -qq unattended-upgrades >/dev/null
echo "installed: $(dpkg-query -W -f='${Version}' unattended-upgrades)"

echo "== periodic apt (update/download/upgrade/autoclean) =="
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Download-Upgradeable-Packages "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

echo "== allowed origins: security + updates + Docker repo =="
# Distro-aware: the Ubuntu ESM origins don't exist on Debian, and Debian's security
# archive is published under origin=Debian-Security. Branch on os-release ID so
# auto-security-updates actually match on whichever Debian-family base we built from.
# (Quoted heredocs: ${distro_id}/${distro_codename} are expanded by unattended-upgrade
# at runtime, NOT by bash here.)
. /etc/os-release
if [ "${ID}" = "debian" ]; then
cat > /etc/apt/apt.conf.d/52brain-unattended <<'EOF'
Unattended-Upgrade::Allowed-Origins {
    "origin=Debian,codename=${distro_codename},label=Debian";
    "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
    "origin=Debian,codename=${distro_codename}-updates,label=Debian";
    "Docker:${distro_codename}";
};
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "false";
EOF
else
cat > /etc/apt/apt.conf.d/52brain-unattended <<'EOF'
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}";
    "${distro_id}:${distro_codename}-security";
    "${distro_id}ESMApps:${distro_codename}-apps-security";
    "${distro_id}ESM:${distro_codename}-infra-security";
    "${distro_id}:${distro_codename}-updates";
    "Docker:${distro_codename}";
};
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "false";
EOF
fi

echo "== what would it upgrade right now (dry run) =="
echo "   NOTE: first build on a fresh WSL distro — this simulates the FULL upgrade set"
echo "   against a newly-downloaded package index. It can run for SEVERAL MINUTES with no"
echo "   output (the summary is buffered until it finishes). This is EXPECTED, not a hang —"
echo "   leave it running; initial system updates on a first-time distro are the slow part."
unattended-upgrade --dry-run 2>&1 | grep -iE 'Allowed origins|Checking|packages that|Adjusting|upgrade' | head -20 || true
echo "   (dry-run complete)"

echo "== apt timers =="
systemctl is-enabled apt-daily.timer apt-daily-upgrade.timer 2>&1 | tr '\n' ' '; echo
echo STAGE5A_DONE
