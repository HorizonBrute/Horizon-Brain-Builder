#!/usr/bin/env bash
# gh_auth_gpg.sh — the POSIX crypto core of the in-brain credential vault.
# =======================================================================
# Runs IN the brain's POSIX environment (the WSL/Lima distro), where gpg-agent works
# natively — on Windows the host gpg cannot socket an agent for a native-Windows GNUPGHOME,
# so ALL gpg lives here, never in the host orchestrator (gh_auth.py). The host tool owns the
# OS keystore + drops the admin's cleartext material on a seam the brain can read; this
# script does the sealing.
#
# The store lives on EXT4 (brain_rw), NOT the drvfs config seam: gpg-agent needs a real
# POSIX homedir for its socket, the brain must be able to WRITE it (the config seam is
# brain-read-only), and keeping it out of brain_etc keeps key material out of the git repo.
#
# ONE STORE PASSPHRASE, TWO SEALED BLOBS (symmetric AES256), plus the keyring:
#   * ssh_keylist.gpg   — the SSH private keys, sealed directly.
#   * gpg_secrets.gpg   — a SIDECAR: `fpr <TAB> base64(passphrase)` per imported GPG key.
#   * gpg_store/        — the GnuPG keyring; imported keys keep THEIR OWN passphrase, which
#                         the sidecar records. This sidesteps gpg's non-interactive passwd
#                         limitation (loopback reuses --passphrase for every prompt, so a key
#                         CANNOT be re-passphrased to a new value in one call). `reset` then
#                         only re-seals the two symmetric blobs — never touches the keyring.
#
# CONTRACT
#   $GH_STORE                  store base dir (default: $HOME/knowledge/brain_rw/gh_auth).
#   $GH_AUTH_PASSPHRASE        store passphrase.
#   $GH_AUTH_NEW_PASSPHRASE    new passphrase (reset only).
#   $GH_AUTH_IMPORT_PASSPHRASE the imported GPG key's own passphrase (import-gpg; default empty).
#   stdin                      cleartext key material (import-ssh / import-gpg).
#
#   gh_auth_gpg.sh <init|import-ssh|import-gpg|status|reset|recreate|unseal-ssh [PATH]>
#
# Passphrases + transient cleartext go to a tmpfs scratch dir (RAM), shredded on exit.
set -eu

GH_STORE="${GH_STORE:-$HOME/knowledge/brain_rw/gh_auth}"
GNUPGHOME_DIR="$GH_STORE/gpg_store"
SSH_SEALED="$GH_STORE/ssh_keylist.gpg"
GPG_SECRETS="$GH_STORE/gpg_secrets.gpg"

die() { echo "  [ERROR] $*" >&2; exit 1; }
info() { echo "  $*"; }
command -v gpg >/dev/null 2>&1 || die "gpg not found in this environment."

# --- tmpfs scratch (RAM), shredded on exit -----------------------------------------------
SCRATCH=""
for c in "${XDG_RUNTIME_DIR:-}" /dev/shm /tmp; do
  [ -n "$c" ] && [ -d "$c" ] || continue
  if SCRATCH="$(mktemp -d "$c/gh_auth.XXXXXX" 2>/dev/null)"; then break; fi
  SCRATCH=""
done
[ -n "$SCRATCH" ] || die "no usable scratch dir (tried \$XDG_RUNTIME_DIR, /dev/shm, /tmp)."
cleanup() {
  find "$SCRATCH" -type f -exec sh -c 'dd if=/dev/urandom of="$1" bs=1 count=$(wc -c <"$1") 2>/dev/null || true; rm -f "$1"' _ {} \; 2>/dev/null || true
  rm -rf "$SCRATCH" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

_need_pass() { [ -n "${GH_AUTH_PASSPHRASE:-}" ] || die "GH_AUTH_PASSPHRASE not set (forwarded by the orchestrator)."; }
_ensure_home() { mkdir -p "$GNUPGHOME_DIR"; chmod 700 "$GNUPGHOME_DIR"; }
_pwfile() { local f; f="$(mktemp "$SCRATCH/pw.XXXXXX")"; chmod 600 "$f"; printf '%s' "$1" > "$f"; printf '%s' "$f"; }

# --- generic symmetric seal/unseal (AES256) with round-trip-verified seal -----------------
# gpg can exit nonzero on an agent hiccup while still writing valid ciphertext, so a seal is
# trusted only if it round-trips; an unseal distinguishes a wrong passphrase from agent noise.
_seal() {  # _seal <passphrase> <src-file> <dest-file>
  local pw="$1" src="$2" dest="$3" PWFILE; PWFILE="$(_pwfile "$pw")"
  gpg --homedir "$GNUPGHOME_DIR" --batch --yes --no-tty --pinentry-mode loopback \
      --passphrase-file "$PWFILE" --symmetric --cipher-algo AES256 -o "$dest.tmp" < "$src" || true
  [ -f "$dest.tmp" ] || die "seal produced no output ($dest)."
  local chk; chk="$(mktemp "$SCRATCH/rt.XXXXXX")"
  gpg --homedir "$GNUPGHOME_DIR" --batch --yes --no-tty --pinentry-mode loopback \
      --passphrase-file "$PWFILE" -o "$chk" -d "$dest.tmp" 2>/dev/null || die "seal round-trip failed ($dest)."
  cmp -s "$src" "$chk" || die "seal round-trip mismatch — refusing to commit ($dest)."
  mv -f "$dest.tmp" "$dest"
}
_unseal() {  # _unseal <passphrase> <sealed-file> <out-file>  (empty out if sealed missing)
  local pw="$1" sealed="$2" out="$3" PWFILE; PWFILE="$(_pwfile "$pw")"
  : > "$out"; chmod 600 "$out"
  [ -f "$sealed" ] || return 0
  if ! gpg --homedir "$GNUPGHOME_DIR" --batch --yes --no-tty --pinentry-mode loopback \
        --passphrase-file "$PWFILE" -o "$out.d" -d "$sealed" 2>"$SCRATCH/err"; then
    grep -qiE 'decryption failed|bad session key' "$SCRATCH/err" && die "unseal failed — wrong passphrase?"
    [ -s "$out.d" ] || die "unseal failed: $(tr '\n' ' ' <"$SCRATCH/err" | cut -c1-200)"
  fi
  mv -f "$out.d" "$out" 2>/dev/null || true
}

# --- SSH helpers -------------------------------------------------------------------------
# Fingerprint each private-key block on stdin. A line-loop (not NUL-split) so no stray
# leading newline reaches ssh-keygen — that was the "(unreadable key block)" bug.
_ssh_fprs() {
  local line blk="" t
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      *BEGIN*PRIVATE\ KEY*) blk="$line"$'\n' ;;
      *END*PRIVATE\ KEY*)
        blk="$blk$line"$'\n'
        t="$(mktemp "$SCRATCH/k.XXXXXX")"; chmod 600 "$t"; printf '%s' "$blk" > "$t"
        ssh-keygen -lf "$t" 2>/dev/null || echo "(unreadable key block)"
        rm -f "$t"; blk="" ;;
      *) [ -n "$blk" ] && blk="$blk$line"$'\n' ;;
    esac
  done
}
_ssh_count() { grep -c 'BEGIN .*PRIVATE KEY' "$1" 2>/dev/null || echo 0; }

# --- commands ----------------------------------------------------------------------------
cmd_init() {
  _need_pass; _ensure_home
  gpg --homedir "$GNUPGHOME_DIR" --batch --list-keys >/dev/null 2>&1 || true
  local empty; empty="$(mktemp "$SCRATCH/e.XXXXXX")"; : > "$empty"
  _seal "$GH_AUTH_PASSPHRASE" "$empty" "$SSH_SEALED"
  _seal "$GH_AUTH_PASSPHRASE" "$empty" "$GPG_SECRETS"
  info "vault initialized at $GH_STORE (empty sealed keylist + gpg sidecar)."
}

cmd_import_ssh() {
  _need_pass; _ensure_home
  local in cur merged; in="$(mktemp "$SCRATCH/in.XXXXXX")"; chmod 600 "$in"; cat > "$in"
  grep -q 'PRIVATE KEY-----' "$in" || die "no SSH private-key blocks on stdin (BEGIN/END PRIVATE KEY)."
  cur="$(mktemp "$SCRATCH/cur.XXXXXX")"; chmod 600 "$cur"; _unseal "$GH_AUTH_PASSPHRASE" "$SSH_SEALED" "$cur"
  merged="$(mktemp "$SCRATCH/mg.XXXXXX")"; chmod 600 "$merged"
  cat "$cur" "$in" | awk '
    /-----BEGIN .*PRIVATE KEY-----/{b=$0"\n"; inb=1; next}
    inb{b=b$0"\n"} /-----END .*PRIVATE KEY-----/{ if(!(b in seen)){seen[b]=1; printf "%s\n", b} inb=0 }
  ' > "$merged"
  _seal "$GH_AUTH_PASSPHRASE" "$merged" "$SSH_SEALED"
  info "sealed keylist now holds $(_ssh_count "$merged") SSH key(s). Imported:"
  _ssh_fprs < "$in" | sed 's/^/    + /'
}

cmd_import_gpg() {
  _need_pass; _ensure_home
  local before after new_fprs mat; mat="$(mktemp "$SCRATCH/imp.XXXXXX")"; chmod 600 "$mat"; cat > "$mat"
  before="$(gpg --homedir "$GNUPGHOME_DIR" --list-secret-keys --with-colons 2>/dev/null | awk -F: '/^fpr:/{print $10}' | sort)"
  local out; out="$(mktemp "$SCRATCH/o.XXXXXX")"
  gpg --homedir "$GNUPGHOME_DIR" --batch --yes --no-tty --import < "$mat" > "$out" 2>&1 \
    || { cat "$out" >&2; die "gpg import failed."; }
  info "GPG import:"; grep -Ei 'imported|secret key|not changed|unchanged' "$out" | sed 's/^/    /' || true
  after="$(gpg --homedir "$GNUPGHOME_DIR" --list-secret-keys --with-colons 2>/dev/null | awk -F: '/^fpr:/{print $10}' | sort)"
  new_fprs="$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$after"))"
  [ -n "$new_fprs" ] || { info "  (no new secret keys)"; return 0; }
  # Record each new key's OWN passphrase in the sealed sidecar (fpr <TAB> base64(passphrase)).
  local side; side="$(mktemp "$SCRATCH/sc.XXXXXX")"; chmod 600 "$side"
  _unseal "$GH_AUTH_PASSPHRASE" "$GPG_SECRETS" "$side"
  local b64; b64="$(printf '%s' "${GH_AUTH_IMPORT_PASSPHRASE:-}" | base64 | tr -d '\n')"
  printf '%s\n' "$new_fprs" | while IFS= read -r fpr; do
    [ -n "$fpr" ] || continue
    grep -v "^$fpr	" "$side" > "$side.n" 2>/dev/null || true; mv -f "$side.n" "$side"
    printf '%s\t%s\n' "$fpr" "$b64" >> "$side"
    info "  tracked $fpr in the sealed gpg sidecar."
  done
  _seal "$GH_AUTH_PASSPHRASE" "$side" "$GPG_SECRETS"
}

cmd_status() {
  echo "gh_auth vault"
  echo "  store: $GH_STORE ($([ -d "$GNUPGHOME_DIR" ] && echo present || echo ABSENT))"
  [ -d "$GNUPGHOME_DIR" ] || return 0
  echo "  GPG secret keys:"
  gpg --homedir "$GNUPGHOME_DIR" --list-secret-keys --with-fingerprint 2>/dev/null | sed 's/^/    /' || echo "    (none)"
  if [ -n "${GH_AUTH_PASSPHRASE:-}" ]; then
    local t; t="$(mktemp "$SCRATCH/s.XXXXXX")"; chmod 600 "$t"
    _unseal "$GH_AUTH_PASSPHRASE" "$SSH_SEALED" "$t"
    echo "  sealed SSH keys: $(_ssh_count "$t")"
    _ssh_fprs < "$t" | sed 's/^/    /'
    _unseal "$GH_AUTH_PASSPHRASE" "$GPG_SECRETS" "$t"
    echo "  gpg passphrases tracked (sidecar): $(grep -c '	' "$t" 2>/dev/null || echo 0)"
  else
    echo "  sealed SSH keys / sidecar: (set GH_AUTH_PASSPHRASE to enumerate)"
  fi
}

cmd_reset() {
  _need_pass
  [ -n "${GH_AUTH_NEW_PASSPHRASE:-}" ] || die "reset needs GH_AUTH_NEW_PASSPHRASE."
  # Re-seal BOTH symmetric blobs under the new passphrase. The GPG keyring is untouched:
  # keys keep their own passphrases (recorded in the sidecar we just re-sealed).
  local t; t="$(mktemp "$SCRATCH/r.XXXXXX")"; chmod 600 "$t"
  _unseal "$GH_AUTH_PASSPHRASE" "$SSH_SEALED" "$t";     _seal "$GH_AUTH_NEW_PASSPHRASE" "$t" "$SSH_SEALED"
  _unseal "$GH_AUTH_PASSPHRASE" "$GPG_SECRETS" "$t";    _seal "$GH_AUTH_NEW_PASSPHRASE" "$t" "$GPG_SECRETS"
  info "vault re-sealed under the new passphrase (ssh keylist + gpg sidecar)."
}

cmd_recreate() {
  _need_pass
  rm -rf "$GNUPGHOME_DIR" "$SSH_SEALED" "$GPG_SECRETS" 2>/dev/null || true
  info "old vault destroyed."
  cmd_init
}

cmd_unseal_ssh() {
  _need_pass
  local out="${1:-$SCRATCH/ssh_unsealed}"
  _unseal "$GH_AUTH_PASSPHRASE" "$SSH_SEALED" "$out"
  info "unsealed $(_ssh_count "$out") SSH key(s) -> $out"
}

[ $# -ge 1 ] || die "usage: gh_auth_gpg.sh <init|import-ssh|import-gpg|status|reset|recreate|unseal-ssh [PATH]>"
cmd="$1"; shift || true
case "$cmd" in
  init) cmd_init "$@" ;; import-ssh) cmd_import_ssh "$@" ;; import-gpg) cmd_import_gpg "$@" ;;
  status) cmd_status "$@" ;; reset) cmd_reset "$@" ;; recreate) cmd_recreate "$@" ;;
  unseal-ssh) cmd_unseal_ssh "$@" ;; *) die "unknown command '$cmd'." ;;
esac
