set -e
B="${BRAIN_NAME:?set BRAIN_NAME}"
M=/home/$B/chroma/nginx/reader_tokens.map
TOK=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
cp -a "$M" "$M.vbak"
printf '# --- token: label=infra-passthru role=reader ---\n"~*^Bearer\\s+%s$"  1;\n' "$TOK" >> "$M"
su - "$B" -c 'cd ~/chroma && docker compose up -d --force-recreate gateway' >/dev/null 2>&1
sleep 1
python3 - "$TOK" <<'PY'
import sys,ssl,urllib.request as u
t=sys.argv[1];c=ssl._create_unverified_context()
T="default_tenant";D="default_database"
def go(m,p,a=1):
 r=u.Request("https://127.0.0.1:8000"+p,method=m)
 if a:r.add_header("Authorization","Bearer "+t)
 try:
  x=u.urlopen(r,context=c,timeout=10);return x.status,x.read(180).decode().replace("\n"," ")
 except u.HTTPError as e:return e.code,e.read(140).decode().replace("\n"," ")
paths=[("GET","/api/v2/pre-flight-checks","handshake"),
       ("GET","/api/v2/version","handshake"),
       ("GET","/api/v2/heartbeat","handshake"),
       ("GET",f"/api/v2/tenants/{T}","tenant validate"),
       ("GET",f"/api/v2/tenants/{T}/databases","db list"),
       ("GET",f"/api/v2/tenants/{T}/databases/{D}","db validate"),
       ("GET",f"/api/v2/tenants/{T}/databases/{D}/collections","collection list"),
       ("POST",f"/api/v2/tenants/{T}/databases/{D}/collections","CREATE (write, want 403)")]
print("--- reader token through gateway ---")
for m,p,note in paths:
 s,b=go(m,p);print(f"{s}  {m:4} {p:58} [{note}]  {b[:70]}")
print("--- no token (want 403 all) ---")
for m,p,note in paths[:1]:
 s,b=go(m,p,a=0);print(f"{s}  {m:4} {p}  {b[:60]}")
PY
: > "$M"; cat "$M.vbak" > "$M"; rm -f "$M.vbak"
su - "$B" -c 'cd ~/chroma && docker compose up -d --force-recreate gateway' >/dev/null 2>&1
echo COVERAGE-DONE
