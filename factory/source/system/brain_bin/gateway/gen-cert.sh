#!/usr/bin/env bash
# Generate the brain gateway's self-signed TLS cert + key (runs IN the distro, at
# install, as the brain). Output → ~/gateway/gateway_out/{cert.pem,cert.key}, which
# the gateway compose mounts read-only at /etc/nginx/certs.
#
# Posture-aware SAN:
#   Personal → DNS:localhost, IP:127.0.0.1  (loopback only).
#   Server   → the above PLUS the host's LAN name/IP so off-box clients validate.
#              Pass extra SAN entries as args, e.g.:
#                 ./gen-cert.sh DNS:brainhost.lan IP:192.168.1.20
#
# TLS is ALWAYS on (incl. Personal): on a shared user space it buys server-auth
# (defeats a process squatting :8000 before the gateway starts) — but ONLY if the
# client verifies the cert. So after generating, distribute cert.pem to clients and
# default them to verify (see "TRUST THE CERT" below and README "Cert & trust").
#
# BRING YOUR OWN CERT (Enterprise): skip this script; drop your real cert.pem +
# cert.key into ~/gateway/gateway_out/ (same filenames). Nothing else changes.
set -euo pipefail

CERT_DIR="${HOME}/gateway/gateway_out"
mkdir -p "${CERT_DIR}"
CERT="${CERT_DIR}/cert.pem"
KEY="${CERT_DIR}/cert.key"

# Base SAN (always) + any extra entries passed as args (Server posture).
SAN="DNS:localhost,IP:127.0.0.1"
for extra in "$@"; do
    SAN="${SAN},${extra}"
done

openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "${KEY}" \
    -out "${CERT}" \
    -days 3650 \
    -subj "/CN=localhost/O=Brain Gateway" \
    -addext "subjectAltName=${SAN}"

# Key is readable only by the gateway identity (the brain uid runs rootless nginx).
# The ACL primer's "gateway TLS private key" surface: visible location, protected key.
chmod 600 "${KEY}"
chmod 644 "${CERT}"

echo "Wrote:"
echo "  ${CERT}"
echo "  ${KEY}  (mode 600)"
echo
echo "SAN:"
openssl x509 -in "${CERT}" -noout -ext subjectAltName | sed 's/^/  /'
echo
echo "TRUST THE CERT (so TLS actually buys server-auth, not just encryption):"
echo "  * same-host client : point it at ${CERT} (or copy it out via the ~/gateway/gateway_out symlink)."
echo "  * Windows clients  : import cert.pem into the user/host trust store, then clients verify by default."
echo "  * least-secure     : verify-off (development escape hatch only — defeats the squatter protection)."
