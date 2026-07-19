# Off-Box / External LAN Gateway Validation Cookbook (all services)

**Applies to:** proving the brain's gateway auth boundary **from a separate host on the LAN** — not localhost — across **all three exposed services**: Chroma (`:8000`), Ollama (`:11434`), and the action query API (`:8443`).
**Audience:** the brain **operator**, running from a second machine (SSH box, laptop) that can reach the brain over the LAN.
**Companion docs:** localhost / Chroma-only proof is `gateway_auth_verification_matrix.md`; the full model is `gateway_bearer_auth_SOP.md`; minting tokens is `gateway_token_admin_howto.md`.
**Status:** proven green live 2026-07-19 against a running brain. This encodes the **method**, not any secret or a specific brain's IP.

---

## 0. What this proves (and how it differs from the localhost matrix)

`gateway_auth_verification_matrix.md` proves Chroma auth **on 127.0.0.1**. This cookbook proves the same auth boundary **over the LAN, off-box**, and extends it to **every published door**:

| Service | Port | Read/use gate | Write/mgmt gate |
|---|---|---|---|
| **Chroma** | `:8000` | reader/writer → 200, none → 403 | writer → 200, reader/none → 403 |
| **Ollama** | `:11434` | `ollama:use` → 200, none → 403 | mgmt (`/api/pull`) → 403 even for a use token (admin-gated) |
| **action API** | `:8443` | `action:call` → 200, none → 403 | (single admission grant; no read/write tiers) |

Off-box adds two failure modes localhost never sees: **unreachable** (wrong IP, ports not published, firewall) and **TLS SAN mismatch** (cert only covers `127.0.0.1`). The pre-checks below exist to tell those apart from a genuine `403`.

---

## 1. Re-read the brain's LAN IP **live** first

The brain gets its LAN address from **DHCP** — it can change across reboots. Never trust a remembered IP. Ask the brain for its current address before every off-box run:

```bash
# host-side, as the operator (resolves the brain's own network namespace)
run_as_brain.py --brain <brain> --wsl -- bash -lc 'ip -brief addr show eth0'
```

Take the IPv4 from that output and use it as `<BRAIN_LAN_IP>` below. (If mirrored networking is in play, cross-check against `TROUBLESHOOTING.md` — a host that only answers on loopback is **not** LAN-reachable.)

```bash
# on the remote LAN host, after SSHing in:
BRAIN_LAN_IP=<BRAIN_LAN_IP>     # from `ip -brief addr show eth0` above — re-read every run
```

---

## 2. TCP reachability pre-check (distinguish "unreachable" from "auth failure")

Do this **before** any curl. A closed/dropped port and a `403` are completely different problems; prove the socket is open first so an auth result is meaningful.

```bash
for p in 8000 11434 8443; do
  if timeout 3 bash -c "</dev/tcp/$BRAIN_LAN_IP/$p" 2>/dev/null; then
    echo "port $p: OPEN"
  else
    echo "port $p: UNREACHABLE"
  fi
done
```

- All three **OPEN** → proceed to the auth matrix.
- Any **UNREACHABLE** → do **not** interpret later `403`/timeouts as auth results. Jump to §7 (the port-publish caveat) first.

> `nc -vz $BRAIN_LAN_IP 8000 11434 8443` works too if `nc` is installed. The `/dev/tcp` form needs nothing but bash.

---

## 3. Tokens for this run (placeholders — never embed a real token)

Mint via `gateway_tokens.py grant` (details + ACL rules in `gateway_token_admin_howto.md`). Off-box validation needs two tokens:

```bash
# host-side, operator — one read-only, one multi-service use/write/call token:
python <install-root>/<brain>/brain_sbin/gateway_tokens.py grant --label offbox-reader --grant chroma:reader
python <install-root>/<brain>/brain_sbin/gateway_tokens.py grant \
    --label offbox-rwc --grant chroma:writer --grant ollama:use --grant action:call
```

Each `grant` prints its raw token **once**. Copy the values into the remote host's shell (never commit them, never paste a live token into these docs):

```bash
# on the remote LAN host:
RD="<reader-token>"     # grants chroma:reader
WR="<writer-token>"     # grants chroma:writer + ollama:use + action:call
```

> **Important:** any token op (grant/rotate/revoke) can drop the gateway's published LAN ports — see §7. If §2 was green and turns red right after minting, that is the cause, not a bad token.

---

## 4. TLS trust

The gateway is TLS on every port. Two options off-box:

- **Quick check:** add `-k` to curl (skips CA verify). Fine for proving the auth codes; not for production clients.
- **Verified:** copy the gateway CA to the remote host and use `--cacert`:
  ```bash
  # the CA lives on the brain at /home/<brain>/gateway/gateway_out/cert.pem
  # copy it to the remote host, then:
  CA=/path/to/cert.pem
  # curl ... --cacert "$CA" ...   instead of  -k
  ```
  The default cert's SAN covers only `localhost`/`127.0.0.1`. For a **clean off-box** `--cacert` run the cert must be re-issued with a LAN SAN (see `gateway_bearer_auth_SOP.md` §2); otherwise `-k` is the pragmatic path.

Every curl below uses **`--connect-timeout 5 -m 15`** so a dropped or hung connection fails fast. (A curl with no timeout against a silently-dropped port hangs indefinitely — a real trap that masquerades as "stuck".)

```bash
C="curl.exe -s -k --connect-timeout 5 -m 15 -o /dev/null -w"   # swap -k for --cacert \"$CA\" when verified
```

---

## 5. The auth matrix (run from the remote host)

### 5.1 Chroma `:8000`

```bash
COL="https://$BRAIN_LAN_IP:8000/api/v2/tenants/default_tenant/databases/default_database/collections"

# READ  reader/writer -> 200, none -> 403
$C 'chroma read reader=%{http_code}\n' --oauth2-bearer "$RD" "https://$BRAIN_LAN_IP:8000/api/v2/heartbeat"
$C 'chroma read writer=%{http_code}\n' --oauth2-bearer "$WR" "https://$BRAIN_LAN_IP:8000/api/v2/heartbeat"
$C 'chroma read none  =%{http_code}\n'                        "https://$BRAIN_LAN_IP:8000/api/v2/heartbeat"

# WRITE  writer create -> 200, reader create -> 403, writer delete -> 200
#   throwaway collection "auth_probe"; NEVER use /reset as a write probe (it wipes the store).
$C 'chroma write writer-create=%{http_code}\n' -X POST   --oauth2-bearer "$WR" -H 'Content-Type: application/json' --data '{"name":"auth_probe"}' "$COL"
$C 'chroma write reader-create=%{http_code}\n' -X POST   --oauth2-bearer "$RD" -H 'Content-Type: application/json' --data '{"name":"auth_probe"}' "$COL"
$C 'chroma write writer-delete=%{http_code}\n' -X DELETE --oauth2-bearer "$WR" "$COL/auth_probe"
```

Expected: read `200/200/403`; write `200/403/200`.

### 5.2 Ollama `:11434`

```bash
# USE  use-token (/api/tags) -> 200, none -> 403
$C 'ollama use use-tok=%{http_code}\n' --oauth2-bearer "$WR" "https://$BRAIN_LAN_IP:11434/api/tags"
$C 'ollama use none   =%{http_code}\n'                        "https://$BRAIN_LAN_IP:11434/api/tags"

# MGMT  a use-token hitting a management path (/api/pull) -> 403 (admin-gated, not use-gated)
$C 'ollama mgmt use-tok=%{http_code}\n' -X POST --oauth2-bearer "$WR" -H 'Content-Type: application/json' --data '{"name":"nonexistent-probe"}' "https://$BRAIN_LAN_IP:11434/api/pull"
```

Expected: use `200/403`; mgmt `403` (the use token carries `ollama:use`, not `ollama:admin`, so management stays closed).

### 5.3 action query API `:8443` (path-router)

The gateway path-routes `/{bundle}/{neuron}/...` on `:8443` to the action neuron. Example bundle/neuron: `example_neuron_bundle/action_neuron_api`.

```bash
B=example_neuron_bundle/action_neuron_api

# HEALTH  call-token -> 200, none -> 403
$C 'action health call-tok=%{http_code}\n' --oauth2-bearer "$WR" "https://$BRAIN_LAN_IP:8443/$B/health"
$C 'action health none    =%{http_code}\n'                        "https://$BRAIN_LAN_IP:8443/$B/health"

# ASK  call-token -> 200, none -> 403
$C 'action ask call-tok=%{http_code}\n' --oauth2-bearer "$WR" "https://$BRAIN_LAN_IP:8443/$B/ask?q=ping"
$C 'action ask none    =%{http_code}\n'                        "https://$BRAIN_LAN_IP:8443/$B/ask?q=ping"
```

Expected: health `200/403`; ask `200/403`. (`action:call` is a single admission grant — no read/write tiers; the app behind it holds its own scoped reader token.)

---

## 6. Full expected matrix (eyeball against this)

| Service | Cell | token | expected |
|---|---|---|---|
| chroma | read | reader / writer / none | **200 / 200 / 403** |
| chroma | write (create/create/delete) | writer / reader / writer | **200 / 403 / 200** |
| ollama | use `/api/tags` | use / none | **200 / 403** |
| ollama | mgmt `/api/pull` | use | **403** |
| action | `/…/health` | call / none | **200 / 403** |
| action | `/…/ask?q=` | call / none | **200 / 403** |

Any disagreement → `gateway_bearer_auth_SOP.md` §6 troubleshooting; if it's specifically **unreachable/timeout** right after a token op, §7 first.

---

## 7. CRITICAL caveat — token ops can drop the published LAN ports (NOTE 001-58)

Minting / rotating / revoking a token, or **any** base-only `docker compose up --force-recreate gateway`, can leave the gateway **running but with NO published LAN ports** — so every off-box probe in §2 times out even though auth is fine and localhost still works. The port publish lives in the deploy overlay, and a base-only recreate drops it.

**If reachability (§2) fails right after a token op, re-publish, then re-test:**

```bash
# host-side, operator — reconciles the port publish (and firewall) without re-pulling images
python <install-root>/<brain>/brain_sbin/reapply_brain_configs.py --no-pull --services gateway
```

Then re-run §2. Ports OPEN again → the auth matrix results from §5 are trustworthy. This is why §2 runs **before** you interpret any `403`: an off-box timeout after minting is almost always the dropped-publish, not the token.

---

## 8. Cross-links

- `gateway_auth_verification_matrix.md` — the localhost / Chroma-only by-hand matrix (start here for a same-box check).
- `gateway_bearer_auth_SOP.md` — the full auth model, TLS/CA (incl. LAN-SAN reissue), troubleshooting.
- `gateway_token_admin_howto.md` — mint / rotate / revoke tokens and the operator ACL gate.
- `TROUBLESHOOTING.md` — LAN-vs-loopback reachability (mirrored networking, port publish) when §2 is red.

---

*Tool source of truth: `factory/source/system/brain_sbin/gateway_tokens.py` and `reapply_brain_configs.py` (canon); run the per-brain deployed mirror.*
