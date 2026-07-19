set -e
B="${BRAIN_NAME:?set BRAIN_NAME}"
T=/home/$B/chroma/nginx/nginx.conf.template
TS=$(date -u +%Y%m%dT%H%M%SZ)
cp -a "$T" "$T.rbak-$TS"
python3 /opt/rab/add_read_paths.py "$T" "chromadb client init (get_user_identity)" \
  '~^GET:/api/v2/auth/identity/?(\?.*)?$'
su - "$B" -c 'cd ~/chroma && docker compose up -d --force-recreate gateway' >/dev/null 2>&1
sleep 1
if su - "$B" -c 'cd ~/chroma && docker compose exec -T gateway nginx -t' 2>&1 | grep -q successful; then
  echo "NGINX-OK: auth/identity read added live"
else
  echo "NGINX-FAIL: restoring"; cp -a "$T.rbak-$TS" "$T"
  su - "$B" -c 'cd ~/chroma && docker compose up -d --force-recreate gateway' >/dev/null 2>&1
  exit 1
fi
