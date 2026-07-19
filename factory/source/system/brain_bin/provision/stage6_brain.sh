#!/usr/bin/env bash
# Stage 6b (BRAIN user): install maintenance scripts + systemd user timers.
# Arg 1 = scratchpad mount path holding the source scripts.
set -uo pipefail
SP="${1:?scratchpad path required}"
mkdir -p "$HOME/bin" "$HOME/logs" "$HOME/backups" "$HOME/.config/systemd/user"

echo "== install bin scripts =="
for f in brain-jlog.sh chroma-backup.sh chroma-update.sh; do
  tr -d '\r' < "$SP/$f" > "$HOME/bin/$f"
  chmod +x "$HOME/bin/$f"
  echo "  installed ~/bin/$f"
done

echo "== systemd user units =="
cat > "$HOME/.config/systemd/user/chroma-backup.service" <<'EOF'
[Unit]
Description=Chroma data snapshot (rotated)
[Service]
Type=oneshot
ExecStart=%h/bin/chroma-backup.sh
EOF
cat > "$HOME/.config/systemd/user/chroma-backup.timer" <<'EOF'
[Unit]
Description=Daily Chroma backup
[Timer]
OnCalendar=*-*-* 03:00:00
RandomizedDelaySec=30m
Persistent=true
[Install]
WantedBy=timers.target
EOF
cat > "$HOME/.config/systemd/user/chroma-update.service" <<'EOF'
[Unit]
Description=Chroma auto-update (newest stable, guarded)
[Service]
Type=oneshot
ExecStart=%h/bin/chroma-update.sh
EOF
cat > "$HOME/.config/systemd/user/chroma-update.timer" <<'EOF'
[Unit]
Description=Daily Chroma update check
[Timer]
OnCalendar=*-*-* 03:30:00
RandomizedDelaySec=1h
Persistent=true
[Install]
WantedBy=timers.target
EOF

echo "== enable timers =="
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user daemon-reload
systemctl --user enable --now chroma-backup.timer chroma-update.timer 2>&1 | tail -2 || true
systemctl --user list-timers chroma-* --no-pager 2>&1 | head -5

echo "== TEST: run a backup now =="
"$HOME/bin/chroma-backup.sh" || true

echo "== TEST: run an update check now =="
# Build-time smoke: the stack is NOT running during provisioning, so the live
# health-check path can stall. Hard-bound it so a maintenance script can never
# wedge the from-scratch build (the script itself is also curl-timeout-bounded).
timeout 90 "$HOME/bin/chroma-update.sh" || true

echo "== maintenance log (JSON lines) =="
cat "$HOME/logs/brain-maintenance.jsonl" 2>/dev/null | tail -5
echo "== backups present =="
ls -lh "$HOME/backups" 2>/dev/null
echo STAGE6B_DONE
