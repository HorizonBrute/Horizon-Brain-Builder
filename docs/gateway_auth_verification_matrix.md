# Gateway Auth — By-Hand Verification Matrix

**Applies to:** the brain's Chroma bearer-token gateway (nginx TLS front, authz **mode C**).
**Audience:** the brain **operator**. Copy-paste, top to bottom, to prove auth is live.
**Companion:** operator token management is in `gateway_token_admin_howto.md`; the full model is in `gateway_bearer_auth_SOP.md`.

---

## 0. What this proves

The gateway is the auth boundary. A **reader** token may only read; a **writer** token may read and write; **no token** is denied outright (mode C). These commands exercise all five cells and print the HTTP code so you can eyeball the result against this table:

| Test | reader (`$RD`) | writer (`$WR`) | no token |
|---|---|---|---|
| **READ** — `GET /api/v2/heartbeat` | **200** | **200** | **403** |
| **WRITE** — create/delete `auth_probe` collection | **403** | **200** | **403** |

> Reading tip: no-token **403** = correct (mode C). Heartbeat JSON with no token = mode B (misconfigured). Empty response / connection refused = stack down.

**Write test discipline:** the write probe creates **and deletes** a throwaway `auth_probe` collection. **Never** use `/reset` as a write probe — it wipes the store.

---

## 1. Setup (PowerShell)

Tokens are minted into the gateway **token registry** — `<brain_root>/brain_etc/gateway/token_registry`, the single source of truth. Each line is a **named token** carrying one or more **scope grants** (`chroma:reader`, `chroma:writer`, `ollama:use`, `ollama:admin`, `action:call`); the per-service nginx maps are *generated* from it. For this by-hand check, copy the **values** of a `chroma:reader`-scoped and a `chroma:writer`-scoped token out of that registry into the two shell variables below (never hardcode or commit a raw token). Gateway at `https://127.0.0.1:8000`. The Chroma v2 collections path includes tenant + database.

```powershell
# --- setup (PowerShell) ---
$RD = "<reader-token>"    # a token granting chroma:reader   (from <brain_root>/brain_etc/gateway/token_registry)
$WR = "<writer-token>"    # a token granting chroma:writer   (from <brain_root>/brain_etc/gateway/token_registry)
$COL = "https://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections"
```

**TLS trust:** `-k` (curl) / an unverified SSL context (python) skips CA-verify for a quick local check. For a clean, verified run use the real gateway CA — `~/gateway/gateway_out/cert.pem` on the brain (`/home/<brain>/gateway/gateway_out/cert.pem`); its SAN covers `127.0.0.1`. Copy it to the client host and swap `-k` for `--cacert <path>` (curl) or a verifying context.

---

## 2. curl

**READ — reader → 200** (writer also → 200; no token → 403)
```powershell
curl.exe -s -k -w "`n%{http_code}`n" --oauth2-bearer $RD https://127.0.0.1:8000/api/v2/heartbeat
```

**WRITE — writer → create 200, delete 200** (reader → 403)
```powershell
'{"name":"auth_probe"}' | Set-Content $env:TEMP\p.json -Encoding ascii -NoNewline
curl.exe -s -k -w "create=%{http_code}`n" -X POST --oauth2-bearer $WR -H "Content-Type: application/json" --data "@$env:TEMP\p.json" $COL
curl.exe -s -k -w "delete=%{http_code}`n" -X DELETE --oauth2-bearer $WR "$COL/auth_probe"
```

`--oauth2-bearer` builds the `Authorization: Bearer …` header for you (avoids quoting the space); `-H "Authorization: Bearer $WR"` is equivalent. To confirm the deny path, repeat the create with `$RD` — expect `create=403`.

---

## 3. Python stdlib (`urllib`)

No third-party install. Same contract — set the header, (here) skip verify with an unverified context.

**READ — reader → 200**
```powershell
python -c "import urllib.request,ssl; ctx=ssl._create_unverified_context(); r=urllib.request.Request('https://127.0.0.1:8000/api/v2/heartbeat', headers={'Authorization':'Bearer '+'$RD'}); print(urllib.request.urlopen(r,context=ctx).status)"
```

**WRITE — writer → 200** (reader raises `HTTPError 403`)
```powershell
python -c "import urllib.request,ssl,json; ctx=ssl._create_unverified_context(); r=urllib.request.Request('$COL', data=json.dumps({'name':'auth_probe'}).encode(), headers={'Authorization':'Bearer '+'$WR','Content-Type':'application/json'}, method='POST'); print(urllib.request.urlopen(r,context=ctx).status)"
# cleanup:
curl.exe -s -k -o NUL -w "delete=%{http_code}`n" -X DELETE --oauth2-bearer $WR "$COL/auth_probe"
```

---

## 4. Chroma client (`chromadb.HttpClient`)

Verified with chromadb 1.5.9. `HttpClient` has **no** `verify=` param, so trust the gateway CA via `SSL_CERT_FILE` **before** constructing the client (see gotchas).

```python
import os, chromadb
os.environ["SSL_CERT_FILE"] = r"C:\path\to\gw_cert.pem"   # gateway CA (SAN must match 127.0.0.1)

def client(tok):
    return chromadb.HttpClient(host="127.0.0.1", port=8000, ssl=True,
                               headers={"Authorization": f"Bearer {tok}"})

# READ  (reader or writer) -> nanosecond int
print(client("<reader-token>").heartbeat())   # token granting chroma:reader (from brain_etc/gateway/token_registry)

# WRITE (writer) -> count 1 -> deleted
wc  = client("<writer-token>")                # token granting chroma:writer (from brain_etc/gateway/token_registry)
col = wc.create_collection("auth_probe", get_or_create=True)
col.add(ids=["1"], embeddings=[[0.1, 0.2, 0.3, 0.4]], documents=["hello brain"])  # explicit embeddings = no model download
print(col.count())                    # -> 1
wc.delete_collection("auth_probe")    # cleanup

# NEGATIVE: reader create_collection -> raises {"status":403,"message":"forbidden"}
```

### Two gotchas (both real, found while verifying)
1. **`SSL_CERT_FILE` *replaces* the public CA bundle** for chromadb's HTTP client. So if you do client-side embedding — `col.add(documents=[...])` **without** passing `embeddings=` — Chroma tries to download its default embedding model over the internet on first use, and that TLS handshake fails (`CERTIFICATE_VERIFY_FAILED`) because only the gateway cert is trusted. Fixes: **(a)** pass explicit `embeddings=` (as above — the write proof needs no model), or **(b)** trust a **combined** bundle:
   ```powershell
   python -c "import certifi,shutil; shutil.copy(certifi.where(),'combined.pem')"
   Get-Content C:\path\to\gw_cert.pem | Add-Content combined.pem     # append gateway cert
   $env:SSL_CERT_FILE = 'combined.pem'                               # trusts internet AND gateway
   ```
   or **(c)** install a real-SAN gateway cert and skip `SSL_CERT_FILE` entirely.
2. **Client init issues several reads** (heartbeat / version / tenant+db / auth identity). Those are all GETs and are allow-listed for readers, so a reader client constructs fine — it only fails at the first *mutating* call.

---

## 5. Verified result matrix

reader read **200** · reader write **403** · writer read **200** · writer write **200** · no-token **403**.

If any cell disagrees, see the troubleshooting table in `gateway_bearer_auth_SOP.md` §6.

---

## 6. One-shot upload smoke test (`testupload_through_gateway.py`)

The matrix above proves the **auth boundary**; this script proves the **upload path** end to end — it pushes documents INTO the store THROUGH the gateway (create collection → add records → read back by id), stdlib only, no local `chromadb` and no embedding-model download.

```powershell
# writer token comes from your env (never hardcode it); defaults hit the local gateway
$env:<WRITER_TOKEN_ENV>   # must be set to a chroma:writer-scoped token minted in brain_etc/gateway/token_registry
python testupload_through_gateway.py
# override the docs dir if you like:
python testupload_through_gateway.py C:\some\other\docs
```

Defaults: gateway `https://127.0.0.1:8000`, `default_tenant`/`default_database`; the collection name and docs dir come from the script header (all overridable via env / argv).

> **Placeholder-embedding caveat:** the vectors are deterministic hashes, NOT semantic embeddings. Upload, storage and retrieval **by id / metadata** are real; **semantic query is NOT meaningful** until you re-ingest with a real embedding function. Ingestion goes through the client API / gateway, never by writing raw files into the store.

---

*Tool source of truth: `factory/source/system/brain_sbin/gateway_token.py` (canon); run the per-brain deployed mirror.*
