# SOP — Gateway Bearer-Token Authorization (Read & Read/Write)

**Applies to:** any brain running the Chroma read-access gateway (nginx TLS front, authz **mode C**).
**Audience:** the brain **operator** — whoever owns `<install-root>/<brain>` on the host. Not the brain account.
**Status:** operator path (create/rotate/revoke/list) proven live; curl client path proven live; Python/Chroma paths documented against the same verified HTTP contract.

---

## 0. The model in one picture

The gateway is the **auth boundary**. Chroma is never exposed directly.

```
client  ──TLS :8000──►  nginx gateway  ──plaintext (brain_net)──►  chroma:8000
        Bearer <reader|writer>          strips client header,        TokenAuthn:
                                        injects Bearer <CHROMA_TOKEN>  requires the
                                                                       static token
```

- **Reader token** → only safe read methods pass (GET heartbeat / version / query / get / list).
- **Writer token** → any method (add / upsert / delete / reset, plus all reads).
- **No token / bad token** → `403` at the gateway. Chroma is never reached.
- The client **never sees** `CHROMA_TOKEN`; the gateway swaps the client's reader/writer bearer for Chroma's static token upstream.

Scope is decided by the **scope grant(s)** carried by the token in the gateway **token registry** (`<brain_root>/brain_etc/gateway/token_registry`): `chroma:reader` vs `chroma:writer` (plus `ollama:use`, `ollama:admin`, `action:call` for the other surfaces). The per-service nginx maps (`reader_tokens.map` / `writer_tokens.map` / …) are **generated** from that registry — they are derived artifacts, not the source of truth, and are never hand-edited.

---

## 1. Create keys (operator task)

The tool: `brains/<brain>/brain_sbin/gateway_token.py`. Run the **deployed mirror** (it resolves its own brain + `/home/<brain>` from its own path). Run it **host-side, as the operator** — no elevation needed as long as you can write the seam (owner or admin). It writes the authoritative seam (`brain_etc/gateway/*.map`), syncs it into the running stack, and force-recreates the gateway so the change is live.

### Create a READ-only key
```powershell
python %AIOS_INSTALL_ROOT%\<brain>\brain_sbin\gateway_token.py create --label obsidian-readback --role reader
```

### Create a READ/WRITE key
```powershell
python %AIOS_INSTALL_ROOT%\<brain>\brain_sbin\gateway_token.py create --role writer --label ingest-pipeline
```

Each `create` prints the raw token **exactly once**:
```
================================================================
  NEW reader token  (label=obsidian-readback)
  Authorization: Bearer 1f7b112ccf29...be96089d
  ^ shown ONCE — copy it now; it cannot be recovered, only rotated.
================================================================
```
Copy it immediately into the consumer's secret store. It is never echoed again.

### The other verbs
```powershell
# list labels + fingerprints (never the secret); read-only, no gate
python ...\gateway_token.py list

# replace a key: the new secret works, the old one dies
python ...\gateway_token.py rotate --label obsidian-readback

# kill a key
python ...\gateway_token.py revoke --label ingest-pipeline
```

### Where tokens live & why they're durable
- Source of truth (authoritative): `brains/<brain>/brain_etc/gateway/{reader,writer}_tokens.map` — **operator-writable, brain is RX-only**.
- The keepalive re-syncs that seam into the running container every cycle, so a token written here **survives**. (Writing the in-distro copy directly does not — that was the old bug.)
- Format is byte-identical to the deploy's bootstrap mint, so `list/rotate/revoke` interoperate with bootstrap tokens.

### Who gets which key
| Consumer | Role | Why |
|---|---|---|
| Query-only readers (notes UI, search, an Obsidian read-back plugin) | **reader** | can search, cannot mutate the store |
| Ingesters / writers (neuron pipeline, bulk import) | **writer** | needs add/upsert/delete |

Issue **one label per consumer** so you can rotate/revoke that consumer without touching others.

---

## 2. What you need to connect (any client)

1. **Endpoint:** `https://<host>:8000` (TLS is always on, both postures).
2. **The token:** `Authorization: Bearer <token>` on every request.
3. **The CA cert** to trust the gateway's TLS: `~/gateway/gateway_out/cert.pem` on the brain
   (`/home/<brain>/gateway/gateway_out/cert.pem`). Copy it to the client host.
   - The cert's SAN currently covers **only** `localhost` / `127.0.0.1`. For an **off-box** client to validate TLS, re-issue the cert with a LAN SAN:
     ```
     run_as_brain.py --brain <brain> --wsl -- bash -lc 'cd ~/docker && ./gen-cert.sh DNS:<host>.lan IP:<lan-ip>'
     ```
     then restart the gateway.

---

## 3. Test with **curl**  ✅ (verified live)

`--oauth2-bearer` builds the `Authorization: Bearer` header for you (avoids quoting a space in the header value). `-H "Authorization: Bearer <tok>"` is equivalent.

```bash
CA=~/gateway/gateway_out/cert.pem
U=https://127.0.0.1:8000
RD=1f7b112c...   # reader token
WR=886a859f...   # writer token

# no token  -> 403
curl -s -o /dev/null -w '%{http_code}\n' --cacert $CA $U/api/v2/heartbeat

# reader, READ  -> 200
curl -s -o /dev/null -w '%{http_code}\n' --cacert $CA --oauth2-bearer $RD $U/api/v2/heartbeat

# reader, WRITE -> 403 (reset is a write/seal path)
curl -s -o /dev/null -w '%{http_code}\n' -X POST --cacert $CA --oauth2-bearer $RD $U/api/v2/reset

# writer, READ + WRITE -> 200
curl -s -o /dev/null -w '%{http_code}\n' --cacert $CA --oauth2-bearer $WR $U/api/v2/heartbeat
```

**Verified enforcement matrix:**

| Request | reader | writer | none |
|---|---|---|---|
| `GET /api/v2/heartbeat` (read) | **200** | 200 | **403** |
| `POST /api/v2/reset` (write) | **403** | 200 | 403 |

> Reading tip: no-token **403** = correct (mode C). Heartbeat JSON with no token = mode B (misconfigured). Empty response = stack down.

---

## 4. Test with a **Python HTTPS request** (`requests`)

Same contract — set the header, trust the CA. Copy `cert.pem` to the client host first.

```python
import requests

BASE  = "https://127.0.0.1:8000"
CA    = r"C:\path\to\cert.pem"          # copy of ~/gateway/gateway_out/cert.pem
TOKEN = "1f7b112c...be96089d"           # reader or writer
H     = {"Authorization": f"Bearer {TOKEN}"}

# READ  (reader or writer) -> 200
print(requests.get(f"{BASE}/api/v2/heartbeat", headers=H, verify=CA).status_code)

# WRITE (writer only; reader -> 403)
print(requests.post(f"{BASE}/api/v2/reset", headers=H, verify=CA).status_code)
```

- `verify=CA` points requests at the gateway's CA cert. Off-box, this **requires** the LAN-SAN cert from §2.
- Dev-only shortcut: `verify=False` skips TLS validation (silences the warning with `urllib3.disable_warnings()`), but do not ship that.

---

## 5. Test via the **Chroma client** (`chromadb.HttpClient`)  ✅ (verified live, chromadb 1.5.9)

The gateway only cares about the `Authorization: Bearer` header, so pass it directly. `HttpClient` has **no** `verify=` param, so trust the self-signed CA via `SSL_CERT_FILE` **before** constructing the client (see the CA gotcha below).

```python
import os, chromadb
os.environ["SSL_CERT_FILE"] = r"C:\path\to\cert.pem"    # gateway CA (SAN must match host)

def client(tok):
    return chromadb.HttpClient(host="127.0.0.1", port=8000, ssl=True,
                               headers={"Authorization": f"Bearer {tok}"})

# READ  (reader or writer) — returns a nanosecond int
print(client("<reader-token>").heartbeat())   # token granting chroma:reader (from brain_etc/gateway/token_registry)

# WRITE (writer only; reader → 403)
wc  = client("<writer-token>")                # token granting chroma:writer (from brain_etc/gateway/token_registry)
col = wc.create_collection("auth_probe", get_or_create=True)
col.add(ids=["1"], embeddings=[[0.1, 0.2, 0.3, 0.4]], documents=["hello brain"])
print(col.count())                    # -> 1
wc.delete_collection("auth_probe")    # cleanup
```

Native-auth equivalent (Chroma's own knob, same header on the wire):
```python
from chromadb.config import Settings
client = chromadb.HttpClient(host="127.0.0.1", port=8000, ssl=True,
    settings=Settings(
        chroma_client_auth_provider="chromadb.auth.token_authn.TokenAuthClientProvider",
        chroma_client_auth_credentials=TOKEN))       # sent as Authorization: Bearer <TOKEN>
```

### Two gotchas found while verifying this (both real)
1. **`SSL_CERT_FILE` *replaces* the public CA bundle** for `chromadb`'s HTTP client (httpx). So if you also do **client-side embedding** — `col.add(documents=[...])` *without* passing `embeddings=` — Chroma downloads its default embedding model over the internet on first use, and that TLS handshake fails (`CERTIFICATE_VERIFY_FAILED`) because only the gateway cert is trusted. Fixes: **(a)** pass explicit `embeddings=` (as above — the write proof needs no model), or **(b)** trust a **combined** bundle:
   ```
   python -c "import certifi,shutil; shutil.copy(certifi.where(),'combined.pem')"
   Get-Content C:\path\to\cert.pem | Add-Content combined.pem     # append gateway cert
   $env:SSL_CERT_FILE = 'combined.pem'                             # trusts internet AND gateway
   ```
   or **(c)** install a real-SAN gateway cert and skip `SSL_CERT_FILE` entirely.
2. **Client init issues several reads** (heartbeat / version / tenant+db / auth identity). Those are all GETs and are allow-listed for readers, so a reader client constructs fine — it only fails at the first *mutating* call.

**Verified live:** reader `heartbeat` → ok · writer `create_collection`+`add` → count 1 → deleted · reader `create_collection` → `{"status":403,"message":"forbidden"}`.

---

## 6. Troubleshooting

| Symptom | Meaning | Fix |
|---|---|---|
| `403` with no/blank token | Correct — mode C denies anonymous | expected |
| `403` with a token you just made | token not live yet, or wrong role for the path | confirm `create` ended with "change is live"; check reader-vs-writer; `list` to confirm it's there |
| Heartbeat returns JSON with **no** token | gateway is in mode B (read-open), not C | re-apply canon gateway template / redeploy gateway |
| Empty / connection refused | stack down | `run_as_brain --brain <brain> --wsl -- docker compose -f /home/<brain>/docker/compose.yaml ps` |
| curl error 60 / cert verify failed | CA not trusted or SAN mismatch (off-box) | use `--cacert`; regen cert with LAN SAN (§2) |
| Token vanished after a minute | (legacy bug) tool wrote in-distro copy | fixed — the tool writes the seam source; ensure you're on the current `gateway_token.py` |

---

## 7. Defense-in-depth note (why the gateway isn't the only wall)

Chroma itself also requires the static `CHROMA_TOKEN` for **every** request (compose env: `CHROMA_SERVER_AUTHN_PROVIDER` = token auth). So even a foothold on `brain_net` that bypasses the gateway hits Chroma's own `401/403` without that token. The gateway's job is to turn that one all-or-nothing token into scoped reader/writer roles — not to be the sole guard. (Bypass test: from a container on `brain_net`, `curl http://chroma:8000/api/v2/heartbeat` with no token → expect 401/403; with `Bearer <CHROMA_TOKEN>` → 200.)

---

## 8. Client-tool compatibility (popular second-brain tools on Chroma)

To work through this gateway a tool must: (a) use Chroma, (b) via **HttpClient** (not embedded/`PersistentClient`), (c) let you set a custom `Authorization: Bearer` header (or Chroma's token auth provider), and (d) trust a custom self-signed CA.

| Tool | Uses Chroma? | Connects via | Bearer header configurable? | Custom CA/TLS? | Verdict | Evidence |
|---|---|---|---|---|---|---|
| **AnythingLLM** | Optional (1 of ~10) | HttpClient (`CHROMA_ENDPOINT`) | **Yes** — `CHROMA_API_HEADER=Authorization` + `CHROMA_API_KEY=Bearer <tok>` | via `NODE_EXTRA_CA_CERTS` (Node), no UI field | **With config** | [.env.example](https://github.com/Mintplex-Labs/anything-llm/blob/master/docker/.env.example), [docs](https://docs.anythingllm.com/setup/vector-database-configuration/local/chroma) |
| **LangChain apps** (`langchain-chroma`) | Yes (if app picks Chroma) | HttpClient (`host/port/ssl/headers`) | **Yes** — native `headers=` or token auth provider | `ssl=True` + `REQUESTS_CA_BUNDLE` | **Yes** | [docs](https://docs.langchain.com/oss/python/integrations/vectorstores/chroma) |
| **Mem0** | Optional (20+) | HttpClient **or** inject your own `client=` | **Yes** — pass a pre-built `chromadb.HttpClient(headers=…, ssl=True)` | via injected client | **With config** | [docs](https://docs.mem0.ai/components/vectordbs/dbs/chroma) |
| **PrivateGPT** | Optional (default Qdrant) | HttpClient (`database: chroma`) | Not in `settings.yaml` — needs code patch to inject `headers=`/auth `Settings` | no first-class field | **Code edit** | [docs](https://docs.privategpt.dev/manual/storage/vector-stores) |
| `chromadb` client (reference/escape hatch) | Yes | HttpClient | **Yes** — `headers={"Authorization":"Bearer …"}` | `ssl=True` + env CA | **Yes** | [auth cookbook](https://cookbook.chromadb.dev/security/auth-1.0.x/) |
| Khoj | **No** (pgvector) | — | — | — | Excluded | [setup](https://docs.khoj.dev/get-started/setup/) |
| Reor / Continue.dev | **No** (LanceDB, embedded) | — | — | — | Excluded | — |
| Quivr | **No** (Supabase/pgvector) | — | — | — | Excluded | — |
| Danswer / Onyx | **No** (Vespa→OpenSearch) | — | — | — | Excluded | — |
| GPT4All (LocalDocs) | **No** (SQLite+hnswlib) | — | — | — | Excluded | — |
| Obsidian: Smart Connections / Copilot | **No** (local/Orama, in-vault) | — | — | — | Excluded | — |
| Obsidian / Logseq "chroma" plugins | No mainstream plugin ships an authed Chroma HttpClient; render-process can't easily present a CA | — | Unknown | Unknown | Unlikely | (gap — verify in plugin registry if needed) |

**Takeaways**
- **Most "second brain" apps are excluded outright** — Khoj, Reor, Quivr, Danswer/Onyx, GPT4All, and both major Obsidian plugins **don't use Chroma**, so this gateway is moot for them.
- **Embedded-only ⇒ can't reach the gateway.** Only tools that use Chroma's **HttpClient** open a network socket and are even candidates.
- **`headers={"Authorization":"Bearer …"}` on `chromadb.HttpClient` is the universal escape hatch** — tools exposing the raw client (LangChain, Mem0 via `client=`) get bearer + CA support for free; host/port-only tools (PrivateGPT) need a small patch.
- **AnythingLLM is the one turnkey PKM app that works with config** (arbitrary `CHROMA_API_HEADER`/`CHROMA_API_KEY`). Validate its TLS/CA trust (`NODE_EXTRA_CA_CERTS`) before relying on it.
- **Self-signed CA trust is the weakest link everywhere** — no tool has a "trust this CA" field; every path depends on the client runtime's env (`REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE` for Python, `NODE_EXTRA_CA_CERTS` for Node). Treat it as a per-tool prerequisite. A real-SAN cert (§2) removes this friction for LAN consumers.

_Confidence: high for the Chroma-usage and auth-param rows; the Obsidian/Logseq "chroma plugin" row is a genuine gap (Low-Med) worth a direct plugin-registry check if it matters._

---

*Tool source of truth: `factory/source/system/brain_sbin/gateway_token.py` (canon); run the per-brain deployed mirror.*
