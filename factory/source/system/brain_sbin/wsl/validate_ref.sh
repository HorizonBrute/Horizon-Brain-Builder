set -e
B="${BRAIN_NAME:?set BRAIN_NAME}"
# Copy the reference config out of the read-only drvfs seam onto ext4 (docker bind-mounts
# from drvfs are unreliable), then run a throwaway nginx:1.27 (cached — the gateway uses it)
# to envsubst-render + `nginx -t`. This is the reference project's own documented validation.
install -d /tmp/refval/certs
cp /opt/rab/ref_nginx.conf.template /tmp/refval/nginx.conf.template
cp /opt/rab/ref_writer_tokens.map   /tmp/refval/writer_tokens.map
# throwaway self-signed cert so ssl_certificate loads (we never touch the live key)
openssl req -x509 -newkey rsa:2048 -nodes -keyout /tmp/refval/certs/key.pem \
  -out /tmp/refval/certs/cert.pem -days 1 -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1
chmod -R a+rX /tmp/refval/certs   # throwaway 1-day cert; rootless container must read it
su - "$B" -c 'docker run --rm --network '"${B}"'_net \
  -e CHROMA_TOKEN=x \
  -e NGINX_ENVSUBST_OUTPUT_DIR=/etc/nginx \
  -e NGINX_ENVSUBST_FILTER=CHROMA \
  -v /tmp/refval/nginx.conf.template:/etc/nginx/templates/nginx.conf.template:ro \
  -v /tmp/refval/writer_tokens.map:/etc/nginx/writer_tokens.map:ro \
  -v /tmp/refval/certs:/etc/nginx/certs:ro \
  nginx:1.27 sh -c "/docker-entrypoint.sh nginx -t" 2>&1'
rm -rf /tmp/refval
echo REFVAL-DONE
