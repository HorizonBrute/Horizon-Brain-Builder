set -e
B="${BRAIN_NAME:?set BRAIN_NAME}"
WM=/home/$B/chroma/nginx/writer_tokens.map
RM=/home/$B/chroma/nginx/reader_tokens.map
[ -f "$WM.tbak" ] && { : > "$WM"; cat "$WM.tbak" > "$WM"; rm -f "$WM.tbak"; }
[ -f "$RM.tbak" ] && { : > "$RM"; cat "$RM.tbak" > "$RM"; rm -f "$RM.tbak"; }
su - "$B" -c 'cd ~/chroma && docker compose up -d --force-recreate gateway' >/dev/null 2>&1
echo "RESTORED token maps; gateway recreated (test tokens removed)"
