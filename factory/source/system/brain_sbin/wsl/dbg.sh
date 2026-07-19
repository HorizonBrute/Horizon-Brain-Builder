set -e
B="${BRAIN_NAME:?set BRAIN_NAME}"
M=/home/$B/chroma/nginx/reader_tokens.map
TOK=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
cp -a "$M" "$M.vbak"
printf '# --- token: label=dbg role=reader ---\n"~*^Bearer\\s+%s$"  1;\n' "$TOK" >> "$M"
echo "reader-token-lines: $(grep -c '1;' "$M")"
echo "appended-regex-shape: $(grep -o '"~\*\^Bearer[^"]*"' "$M" | tail -1 | sed 's/Bearer.*/Bearer\\s+<TOK>$"/')"
su - "$B" -c 'cd ~/chroma && docker compose exec -T gateway nginx -s reload'
python3 - "$TOK" <<'PY'
import sys,ssl,urllib.request as u
t=sys.argv[1];c=ssl._create_unverified_context()
r=u.Request("https://127.0.0.1:8000/api/v2/heartbeat");r.add_header("Authorization","Bearer "+t)
try:print("READ status",u.urlopen(r,context=c,timeout=10).status)
except u.HTTPError as e:print("READ status",e.code)
PY
echo "== access log tail (no tokens logged by design) =="
su - "$B" -c 'tail -2 ~/logs/gateway/access.log'
: > "$M"; cat "$M.vbak" > "$M"; rm -f "$M.vbak"
su - "$B" -c 'cd ~/chroma && docker compose exec -T gateway nginx -s reload'
echo DBG-DONE
