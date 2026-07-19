#!/usr/bin/env bash
# Set the gateway host port and bind in ~/docker/.env, then recreate the gateway.
# Args:  $1 = port   $2 = bind (127.0.0.1 | 0.0.0.0)
set -e
f=~/docker/.env
[ -f "$f" ] || { echo "no ~/docker/.env — gateway stack not deployed"; exit 3; }

upsert() { if grep -q "^$1=" "$f"; then sed -i "s|^$1=.*|$1=$2|" "$f"; else echo "$1=$2" >> "$f"; fi; }
upsert CHROMA_PORT "$1"
upsert GW_BIND_ADDRESS "$2"

# Recreate with the exposure overlays that match the two-zone model, so the published ports
# and service configs are not dropped. An overlay layers when the gateway publishes
# (EXTERNAL_GATEWAY_ENABLE=on) and the surface is enabled; the action overlay layers whenever
# the gateway publishes.
files="-f compose.yaml"
if grep -q '^EXTERNAL_GATEWAY_ENABLE=on' "$f"; then
  grep -q '^CHROMA_ENABLE=on' "$f" && files="$files -f compose.chroma-gateway.yaml"
  grep -q '^OLLAMA_ENABLE=on' "$f" && files="$files -f compose.ollama-gateway.yaml"
  files="$files -f compose.action-neuron-gateway.yaml"
fi
cd ~/docker && docker compose $files up -d gateway
