set -e
B="${BRAIN_NAME:?set BRAIN_NAME}"
T=/home/$B/chroma/nginx/nginx.conf.template
grep -qF '"~^..1$"' "$T" || { echo NOBUG-ALREADY-FIXED; exit 3; }
cp -a "$T" "$T.bak-$(date -u +%Y%m%dT%H%M%SZ)"
sed -i 's|"~\^\.\.1\$" 1;|"~^1.1$" 1;|' "$T"
sed -i 's|# any request bearing a valid reader token|# reader token on a READ path only|' "$T"
grep -qF '"~^..1$"' "$T" && { echo STILL-BUGGY-RESTORE; cp -a "$T".bak-* "$T"; exit 4; }
echo FIXED-ON-DISK
su - "$B" -c 'cd ~/chroma && docker compose up -d --force-recreate gateway'
su - "$B" -c 'cd ~/chroma && docker compose exec -T gateway nginx -t'
su - "$B" -c 'cd ~/chroma && docker compose ps --format "{{.Name}} {{.State}} {{.Status}}"'
echo DONE
