set -e
B="${BRAIN_NAME:?set BRAIN_NAME}"
M=/home/$B/chroma/nginx/reader_tokens.map
TOK=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
cp -a "$M" "$M.vbak"
printf '# --- token: label=infra-verify role=reader ---\n"~*^Bearer\\s+%s$"  1;\n' "$TOK" >> "$M"
su - "$B" -c 'cd ~/chroma && docker compose up -d --force-recreate gateway' >/dev/null 2>&1
sleep 1
python3 - "$TOK" <<'PY'
import sys,ssl,urllib.request as u
t=sys.argv[1];c=ssl._create_unverified_context()
def go(m,p,a):
 r=u.Request("https://127.0.0.1:8000"+p,method=m)
 if a:r.add_header("Authorization","Bearer "+t)
 try:return u.urlopen(r,context=c,timeout=10).status
 except u.HTTPError as e:return e.code
W="/api/v2/tenants/default_tenant/databases/default_database/collections"
print("READ  +reader :",go("GET","/api/v2/heartbeat",1),"(want 200)")
print("WRITE +reader :",go("POST",W,1),"(want 403)")
print("READ  no-auth :",go("GET","/api/v2/heartbeat",0),"(want 403)")
print("WRITE no-auth :",go("POST",W,0),"(want 403)")
PY
: > "$M"; cat "$M.vbak" > "$M"; rm -f "$M.vbak"
su - "$B" -c 'cd ~/chroma && docker compose up -d --force-recreate gateway' >/dev/null 2>&1
echo "== reader map after restore (mapline count, no tokens shown) =="
grep -c '1;' "$M" || true
echo VERIFY-DONE
