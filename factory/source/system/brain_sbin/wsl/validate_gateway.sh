set -e
B="${BRAIN_NAME:?set BRAIN_NAME}"
M=/home/$B/chroma/nginx/reader_tokens.map
TOK=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
cp -a "$M" "$M.vbak"
printf '# --- token: label=infra-validate role=reader ---\n"~*^Bearer\\s+%s$"  1;\n' "$TOK" >> "$M"
su - "$B" -c 'cd ~/chroma && docker compose up -d --force-recreate gateway' >/dev/null 2>&1
sleep 1
python3 - "$TOK" <<'PY'
import sys,ssl,urllib.request as u
t=sys.argv[1];c=ssl._create_unverified_context()
def go(m,p,a=1):
 r=u.Request("https://127.0.0.1:8000"+p,method=m)
 if a:r.add_header("Authorization","Bearer "+t)
 try:
  x=u.urlopen(r,context=c,timeout=10);return x.status,x.headers.get("Server"),x.read(200).decode()
 except u.HTTPError as e:return e.code,e.headers.get("Server"),e.read(160).decode()
s,h,d=go("GET","/api/v2/version");   print("VERSION  +reader:",s,"| upstream Server=%r"%h,"| body=",d)
s,h,d=go("GET","/api/v2/heartbeat"); print("HEARTBEAT+reader:",s,"| upstream Server=%r"%h,"| body=",d)
s,h,d=go("POST","/api/v2/tenants/default_tenant/databases/default_database/collections"); print("WRITE    +reader:",s,"(want 403)")
# rate limit: hammer one IP
codes={}; b429=None
for i in range(250):
 r=u.Request("https://127.0.0.1:8000/api/v2/heartbeat"); r.add_header("Authorization","Bearer "+t)
 try: st=u.urlopen(r,context=c,timeout=10).status
 except u.HTTPError as e:
  st=e.code
  if st==429 and b429 is None: b429=e.read(120).decode()
 codes[st]=codes.get(st,0)+1
print("RATE-LIMIT 250 rapid:",codes,"| sample 429 body:",b429)
PY
: > "$M"; cat "$M.vbak" > "$M"; rm -f "$M.vbak"
su - "$B" -c 'cd ~/chroma && docker compose up -d --force-recreate gateway' >/dev/null 2>&1
echo VALIDATE-DONE
