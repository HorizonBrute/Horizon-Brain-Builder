set -e
B="${BRAIN_NAME:?set BRAIN_NAME}"
WM=/home/$B/chroma/nginx/writer_tokens.map
RM=/home/$B/chroma/nginx/reader_tokens.map
cp -a "$WM" "$WM.tbak"; cp -a "$RM" "$RM.tbak"
cat /opt/rab/_test_writer.map >> "$WM"
cat /opt/rab/_test_reader.map >> "$RM"
su - "$B" -c 'cd ~/chroma && docker compose up -d --force-recreate gateway' >/dev/null 2>&1
sleep 1
su - "$B" -c 'cd ~/chroma && docker compose exec -T gateway nginx -t' 2>&1 | grep -i success || { echo "NGINX-FAIL"; exit 1; }
echo "INSTALLED test writer+reader tokens; gateway recreated"
