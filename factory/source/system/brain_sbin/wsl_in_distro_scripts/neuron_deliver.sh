#!/usr/bin/env bash
# neuron_deliver.sh — the WRITE phase: clone the manifest's git sources into brain_ro.
# Runs the neuron image with brain_ro mounted READ-WRITE (the ingest container mounts it
# :ro). Delivery != ingest; this is the only step that writes brain_ro.
#
# AUTH is driven by the github config seam (brain_etc/github/ -> ~/docker/github):
#   * public              keyless HTTPS — no credential, nothing to inject.
#   * operator-delivered  the tree is pre-written into brain_ro; the adapter clones nothing.
#   * transient-cred      a SHORT-LIVED credential, supplied ONLY on this run and NEVER
#                         persisted. Two shapes, selected by protocol:
#                           - https: an HTTPS token (Authorization header, git.py).
#                           - ssh:   a key from the in-brain vault (gh_auth), unsealed HERE
#                                    into an EPHEMERAL ssh-agent whose socket is bind-mounted
#                                    into the delivery container for the single run.
#
# The transient material never rides argv or env across the host boundary (run_as_brain
# forwards neither). The HOST orchestrator (system/brain_sbin/neuron_deliver.py) drops it as a
# 0600 file on the config seam's .transient dir and passes the IN-DISTRO path here:
#
#     neuron_deliver.sh [--token-file PATH] [--ssh-pass-file PATH]
#
#   --token-file      file holding the HTTPS transient token; exported into $GITHUB_TOKEN_ENV
#                     and forwarded to the delivery container for the single run.
#   --ssh-pass-file   file holding the gh_auth STORE PASSPHRASE; used to `unseal-ssh` the vault
#                     into an ephemeral ssh-agent, whose socket is mounted into the container.
#
# Everything this script unseals lives in tmpfs (RAM) and is shredded on exit no matter what;
# the ssh-agent is killed on exit.
set -eu
B="$(basename "$HOME")"
GH_ENV="$HOME/docker/github/github.env"
GH_AUTH_SCRIPT="/opt/brain_wsl_in_distro_scripts/gh_auth_gpg.sh"

TOKEN_FILE=""
SSH_PASS_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --token-file)    TOKEN_FILE="${2:-}"; shift 2 ;;
    --ssh-pass-file) SSH_PASS_FILE="${2:-}"; shift 2 ;;
    *) echo "  [ERROR] unknown arg '$1' (want --token-file / --ssh-pass-file)" >&2; exit 2 ;;
  esac
done

# --- tmpfs scratch (RAM), agent + unsealed keys shredded/killed on exit -------------------
SCRATCH=""
for c in "${XDG_RUNTIME_DIR:-}" /dev/shm /tmp; do
  [ -n "$c" ] && [ -d "$c" ] || continue
  if SCRATCH="$(mktemp -d "$c/neuron_deliver.XXXXXX" 2>/dev/null)"; then break; fi
done
[ -n "$SCRATCH" ] || { echo "  [ERROR] no usable scratch dir" >&2; exit 1; }
AGENT_PID=""
cleanup() {
  [ -n "$AGENT_PID" ] && kill "$AGENT_PID" 2>/dev/null || true
  find "$SCRATCH" -type f -exec sh -c 'dd if=/dev/urandom of="$1" bs=1 count=$(wc -c <"$1") 2>/dev/null || true; rm -f "$1"' _ {} \; 2>/dev/null || true
  rm -rf "$SCRATCH" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# The transient-token env var NAME the config declares (default GITHUB_TOKEN); read WITHOUT
# sourcing secrets — github.env holds only non-secret config, the token value is in OUR env.
TOKEN_ENV="GITHUB_TOKEN"
if [ -f "$GH_ENV" ]; then
  v="$(sed -n 's/^GITHUB_TOKEN_ENV=//p' "$GH_ENV" | tail -1)"
  [ -n "$v" ] && TOKEN_ENV="$v"
fi

cred_args=()

# --- HTTPS transient token: read the seam-drop into our env, forward with -e NAME ---------
if [ -n "$TOKEN_FILE" ]; then
  [ -f "$TOKEN_FILE" ] || { echo "  [ERROR] --token-file not found: $TOKEN_FILE" >&2; exit 1; }
  export "$TOKEN_ENV"="$(cat "$TOKEN_FILE")"
fi
if [ -n "${!TOKEN_ENV:-}" ]; then
  cred_args+=(-e "$TOKEN_ENV")
  echo "== transient HTTPS credential present in \$$TOKEN_ENV -> forwarding to this run only =="
else
  echo "== no transient HTTPS token (fine for public / operator-delivered / ssh) =="
fi

# --- SSH transient cred: unseal the vault into an EPHEMERAL ssh-agent ----------------------
# The container's git.py relies on a forwarded $SSH_AUTH_SOCK (with the pinned known_hosts).
# We unseal in-distro, load the key(s) into a throwaway agent, and mount its socket in.
if [ -n "$SSH_PASS_FILE" ]; then
  [ -f "$SSH_PASS_FILE" ] || { echo "  [ERROR] --ssh-pass-file not found: $SSH_PASS_FILE" >&2; exit 1; }
  [ -f "$GH_AUTH_SCRIPT" ] || { echo "  [ERROR] vault script missing: $GH_AUTH_SCRIPT" >&2; exit 1; }

  unsealed="$SCRATCH/ssh_unsealed"
  GH_AUTH_PASSPHRASE="$(cat "$SSH_PASS_FILE")" bash "$GH_AUTH_SCRIPT" unseal-ssh "$unsealed" >/dev/null
  [ -s "$unsealed" ] || { echo "  [ERROR] vault unsealed no SSH keys (import one with gh_auth.py import-ssh)" >&2; exit 1; }

  # Split the concatenated BEGIN/END blocks into one 0600 file per key (ssh-add wants one
  # key per file). Same block-loop shape the vault uses to avoid stray-newline corruption.
  keydir="$SCRATCH/keys"; mkdir -p "$keydir"; chmod 700 "$keydir"
  awk -v d="$keydir" '
    /-----BEGIN .*PRIVATE KEY-----/ { n++; f=sprintf("%s/k%03d", d, n); inb=1 }
    inb { print > f }
    /-----END .*PRIVATE KEY-----/   { close(f); inb=0 }
  ' "$unsealed"

  # Ephemeral agent. ssh-add must be NON-INTERACTIVE — vault keys are expected passphrase-less
  # (deploy keys); a passphrase-protected key fails fast here instead of hanging the run.
  eval "$(ssh-agent -s -a "$SCRATCH/agent.sock")" >/dev/null
  AGENT_PID="$SSH_AGENT_PID"
  added=0
  for k in "$keydir"/k*; do
    [ -f "$k" ] || continue
    chmod 600 "$k"
    if SSH_ASKPASS=/bin/false SSH_ASKPASS_REQUIRE=never DISPLAY= ssh-add "$k" >/dev/null 2>&1; then
      added=$((added+1))
    else
      echo "  [WARN] a vault SSH key was rejected (passphrase-protected keys are unsupported here)" >&2
    fi
  done
  [ "$added" -gt 0 ] || { echo "  [ERROR] no SSH keys loaded into the agent — cannot reach a private ssh remote" >&2; exit 1; }
  echo "== ephemeral ssh-agent: $added key(s) loaded, socket mounted for this run only =="
  cred_args+=(-v "$SSH_AUTH_SOCK":/ssh-agent:ro -e SSH_AUTH_SOCK=/ssh-agent)
fi

echo "== deliver (git) -> brain_ro =="
docker run --rm \
  --network "${B}_net" \
  --env-file "$GH_ENV" \
  "${cred_args[@]}" \
  -v "$HOME/docker/neuron":/etc/neuron:ro \
  -v "$HOME/docker/github":/etc/github:ro \
  -v "$HOME/knowledge/brain_ro":/knowledge:rw \
  -e KNOWLEDGE_ROOT=/knowledge \
  -e NEURON_MANIFEST=/etc/neuron/sources.yaml \
  "${B}-input_neurons" --deliver-only
echo
echo "== delivered tree =="
ls -la "$HOME/knowledge/brain_ro/SorceryPunk" 2>&1 | head -20 | sed 's/^/  /'
echo "  total files: $(find "$HOME/knowledge/brain_ro/SorceryPunk" -type f 2>/dev/null | wc -l)"
echo "  *.md files:  $(find "$HOME/knowledge/brain_ro/SorceryPunk" -type f -name '*.md' 2>/dev/null | wc -l)"
echo "== DONE =="
