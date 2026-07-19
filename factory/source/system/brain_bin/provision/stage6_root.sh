#!/usr/bin/env bash
# Stage 6a (ROOT): tools needed by the maintenance layer.
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -qq
apt-get install -y -qq jq zstd curl >/dev/null
echo "jq=$(jq --version) zstd=$(zstd --version | head -1)"
echo STAGE6A_DONE
