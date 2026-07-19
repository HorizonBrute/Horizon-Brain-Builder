set -e
B="${BRAIN_NAME:?set BRAIN_NAME}"
T=/home/$B/chroma/nginx/nginx.conf.template
M=/home/$B/chroma/nginx/reader_tokens.map
cp -a "$T" "$T.logbak"; cp -a "$M" "$M.logbak"
python3 - "$T" <<'PY'
import sys
p=sys.argv[1];s=open(p).read()
old='\'"allowed":"$allowed"\''
new='\'"allowed":"$allowed",\'\n          \'"ir":"$is_read","iw":"$is_writer","ird":"$is_reader"\''
assert old in s, "log anchor not found"
open(p,'w').write(s.replace(old,new,1));print("log_format patched")
PY
TOK=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
printf '"~*^Bearer\\s+%s$"  1;\n' "$TOK" >> "$M"
su - "$B" -c 'cd ~/chroma && docker compose up -d --force-recreate gateway' >/dev/null 2>&1
sleep 1
python3 - "$TOK" <<'PY'
import sys,ssl,urllib.request as u
t=sys.argv[1];c=ssl._create_unverified_context()
r=u.Request("https://127.0.0.1:8000/api/v2/heartbeat");r.add_header("Authorization","Bearer "+t)
try:print("READ",u.urlopen(r,context=c,timeout=10).status)
except u.HTTPError as e:print("READ",e.code)
PY
echo "== log tail (flags: ir=is_read iw=is_writer ird=is_reader) =="
su - "$B" -c 'tail -1 ~/logs/gateway/access.log'
cp -a "$T.logbak" "$T"; : > "$M"; cat "$M.logbak" > "$M"; rm -f "$T.logbak" "$M.logbak"
su - "$B" -c 'cd ~/chroma && docker compose up -d --force-recreate gateway' >/dev/null 2>&1
echo LOGDBG-DONE
