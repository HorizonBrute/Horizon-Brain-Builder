set -e
B="${BRAIN_NAME:?set BRAIN_NAME}"
T=/home/$B/chroma/nginx/nginx.conf.template
TS=$(date -u +%Y%m%dT%H%M%SZ)
cp -a "$T" "$T.hbak-$TS"
echo "backup: $T.hbak-$TS"
python3 /opt/rab/harden_nginx.py "$T"
su - "$B" -c 'cd ~/chroma && docker compose up -d --force-recreate gateway' >/dev/null 2>&1
sleep 1
if su - "$B" -c 'cd ~/chroma && docker compose exec -T gateway nginx -t' 2>&1 | grep -q successful; then
  echo "NGINX-OK: hardening live"
  su - "$B" -c 'cd ~/chroma && docker compose ps --format "{{.Name}} {{.State}} {{.Status}}"'
else
  echo "NGINX-FAIL: restoring backup"
  cp -a "$T.hbak-$TS" "$T"
  su - "$B" -c 'cd ~/chroma && docker compose up -d --force-recreate gateway' >/dev/null 2>&1
  su - "$B" -c 'cd ~/chroma && docker compose exec -T gateway nginx -t' 2>&1
  exit 1
fi
echo DONE
