# Administrator How-To — Creating Gateway Bearer Tokens

**Applies to:** issuing / rotating / revoking the bearer tokens the Chroma gateway matches (reader + writer, authz **mode C**).
**Audience:** the brain **operator** — the human owner of `<install-root>/<brain>`, or an admin. **Not** the brain account.
**Tool:** `brains/<brain>/brain_sbin/gateway_token.py`.
**Companion:** verify new tokens with `gateway_auth_verification_matrix.md`; full model in `gateway_bearer_auth_SOP.md`.

---

## 1. Who can do this, and why

Token management writes the gateway's **seam source of truth** — the host-side, operator-owned config exposure `<install-root>/<brain>/brain_etc/gateway/*.map`. The security gate is the **filesystem ACL on that seam**, not an elevation check:

- **Write is granted to the operator set:** the file **owner**, **Administrators**, and **SYSTEM**. (If the host defines its own human-operator group, grant it here too — the tool's gate reads the ACL, it does not hardcode a group.)
- **The brain account/group is granted RX only** — read + execute, **no write** (and there is deliberately no DENY ACE — an explicit DENY here out-prioritized the RX grant and broke the brain's own read of its config, the mode-C read-wall). A brain that could write these maps could grant itself write, which is exactly why it can't.
- **Elevation is neither necessary nor sufficient.** Not necessary — the operator already owns the files. Not sufficient — being admin doesn't make you the operator; the tool's gate is a **probe write** that HONORS the ACL (`require_operator`). It passes for anyone the ACL lets write and fails otherwise.
- Making the change **live** additionally needs the brain's own credential (via `run_as_brain`) — a second thing the brain account does not hold for itself.

**`list` is read-only** and skips the gate; every write verb (`create` / `rotate` / `revoke`) runs the probe-write gate first.

---

## 2. Prerequisites

1. You are the operator (owner or admin) of `<install-root>/<brain>`.
2. Run the tool from the **brain's deployed mirror copy**: `<install-root>/<brain>/brain_sbin/gateway_token.py`. It resolves its brain **from its own folder** — `BRAIN_DIR` is the tool's parent's parent, and `/home/<brain>` is derived from that. The map path is derived from the tool's location, **not** from `--brain` (`--brain` only overrides the name, defaulting to the folder). **Do not** run the canon source copy; run the mirror so it targets the right brain's seam.
3. The gateway stack is up (for the live recreate to succeed). If it is down, the token change still saves durably to the seam and applies on the next keepalive.

---

## 3. List existing tokens (safe, no gate)

```powershell
python %AIOS_INSTALL_ROOT%\<brain>\brain_sbin\gateway_token.py list
```
Shows label + `sha256:` fingerprint + created timestamp per role. **The raw token is never echoed by `list`** — only at create/rotate, once.
```
WRITER tokens (…\brain_etc\gateway\writer_tokens.map):
  bootstrap                sha256:3159bdfe849c   created=2026-07-04T…Z

READER tokens (…\brain_etc\gateway\reader_tokens.map):
  bootstrap                sha256:7cba73d411a9   created=2026-07-04T…Z
```

---

## 4. Create a token

`--role writer` is the **default**; pass `--role reader` for a read-only key. Use **one label per consumer** so you can rotate/revoke that consumer alone.

```powershell
# READ-only key
python %AIOS_INSTALL_ROOT%\<brain>\brain_sbin\gateway_token.py create --label obsidian-readback --role reader

# READ/WRITE key (writer is default)
python %AIOS_INSTALL_ROOT%\<brain>\brain_sbin\gateway_token.py create --label ingest-pipeline --role writer
```

Expected output — the raw token is printed **exactly once**:
```
================================================================
  NEW reader token  (label=obsidian-readback)
  Authorization: Bearer 1f7b112ccf29…be96089d
  ^ shown ONCE — copy it now; it cannot be recovered, only rotated.
================================================================
[gateway_token] gateway synced + recreated — change is live.
```
**Copy it immediately** into the consumer's secret store. It is never recoverable — only rotated. (A duplicate label in the same role is rejected: use `rotate`.)

---

## 5. Rotate / revoke

```powershell
# Rotate: mint a new secret under the same label+role; the OLD secret dies immediately.
python %AIOS_INSTALL_ROOT%\<brain>\brain_sbin\gateway_token.py rotate --label obsidian-readback

# Revoke: kill a label entirely.
python %AIOS_INSTALL_ROOT%\<brain>\brain_sbin\gateway_token.py revoke --label ingest-pipeline
```
`rotate` prints a fresh token once (same one-shot rule as create). `revoke` finds the label across both maps and removes it. Both make the change live.

---

## 6. What happens under the hood

Each write verb:
1. **Writes the seam source** — `<install-root>/<brain>/brain_etc/gateway/{reader,writer}_tokens.map`, host-side and operator-owned, as **LF bytes** (never CRLF — see §7). This is the durable source of truth; the synced in-container copy is disposable and re-derived every keepalive cycle.
2. **Syncs it into the running stack** via the apply primitive (`apply_brain_truths.sh`), run **as the brain** (`run_as_brain --wsl`, never root — the gateway is rootless Docker whose socket lives in the brain's `XDG_RUNTIME_DIR`).
3. **Force-recreates the gateway container** (`docker compose up -d --force-recreate gateway`).

**Why force-recreate, not `nginx -s reload`:** the token maps are bind-mounted into the container as individual **files**, and the apply primitive replaces each via atomic rename (`cp tmp; mv tmp dst`). A file bind mount binds the **inode**, not the path — so after the `mv` the container still resolves the OLD inode, and a reload re-reads stale content (proven live: a reload left the container on the pre-change map). Recreating the container re-establishes the bind mounts against the current inodes (and re-runs envsubst).

If the live recreate can't run right now (stack down, `run_as_brain` missing), the change is still **saved to the seam** and applies on the next keepalive — the tool says so and points you at a `ps` check.

---

## 7. CRLF discipline

The maps are authored on Windows (host-side) but parsed in bash / read by nginx on the WSL side. The tool writes **pure LF** on every platform (`newline=""` + explicit `\n`, atomic replace). A stray `\r` has bitten this seam twice (keepalive manifest + phantom-file bugs) — if you ever hand-edit a `.map`, keep it LF-only.

---

## 8. Verify the new token works

Prove the new token end-to-end with the by-hand matrix: **`gateway_auth_verification_matrix.md`**. Point `$RD`/`$WR` at the token value you just issued (a `chroma:reader`- / `chroma:writer`-scoped token as recorded in `brain_etc/gateway/token_registry`) and confirm the expected cell:

- reader → READ **200**, WRITE **403**
- writer → READ **200**, WRITE **200**
- no token → **403**

If a token you just made returns **403** on a path it should allow: confirm `create` ended with "change is live", check reader-vs-writer for the path, and run `list` to confirm it's stored.

---

*Tool source of truth: `factory/source/system/brain_sbin/gateway_token.py` (canon); run the per-brain deployed mirror.*
