#!/usr/bin/env python3
r"""testupload_through_gateway.py — one-shot UPLOAD smoke test for a brain's
Chroma bearer-token gateway.

WHAT THIS PROVES
  You can push documents INTO the Chroma vector store THROUGH the authed nginx
  gateway using nothing but the Python standard library — no local `chromadb`,
  no embedding model download. It exercises the full write path a real ingester
  uses: create collection -> add records -> read them back (count + get by id).
  Companion to `gateway_auth_verification_matrix.md`, which proves the *auth*
  boundary (reader/writer/no-token). This proves the *upload* path end to end.

PLACEHOLDER-EMBEDDING CAVEAT (read this)
  The embedding vectors here are DETERMINISTIC HASHES of the text, NOT semantic
  embeddings. That is enough to prove upload + storage + retrieval BY ID or BY
  METADATA works. It is NOT enough for semantic query: a `query` by meaning will
  return garbage until you re-ingest with a real embedding function. Swap in a
  real embedder before trusting nearest-neighbour results.

INGESTION GOES THROUGH THE CLIENT API / GATEWAY
  Record data enters the store via the Chroma v2 REST API behind the gateway
  (this script's `add` call) — never by writing raw files into the store on
  disk. The gateway's writer token authorises the write.

CONFIG (all overridable; token is REQUIRED)
  CHROMA_WRITER_TOKEN   required — a chroma:writer-scoped bearer token minted in
                        <brain_root>/brain_etc/gateway/token_registry (never printed)
  CHROMA_GATEWAY_BASE   default https://127.0.0.1:8000
  CHROMA_TENANT         default default_tenant
  CHROMA_DATABASE       default default_database
  CHROMA_COLLECTION     default smoke_test_docs
  CHROMA_DOCS_DIR       default: the repo's shipped sample docs
                        (factory/source/knowledge/brain_ro/example_input_files/docs)
  argv[1] (optional)    overrides the docs dir for this run

USAGE (PowerShell)
  $env:CHROMA_WRITER_TOKEN = "<a chroma:writer token from the registry>"
  python testupload_through_gateway.py
  # or point it at a different docs dir:
  python testupload_through_gateway.py C:\some\other\docs

TLS: uses an unverified SSL context (-k equivalent) for a quick local check.
For a verified run, trust the gateway CA instead (see the verification matrix).
"""
import glob, hashlib, json, os, ssl, sys, urllib.error, urllib.request

WR = os.environ.get("CHROMA_WRITER_TOKEN")
if not WR:
    sys.exit("ERROR: set CHROMA_WRITER_TOKEN (a chroma:writer bearer token) in your env first.")

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_DOCS = os.path.normpath(os.path.join(
    _HERE, "..", "factory", "source", "knowledge", "brain_ro", "example_input_files", "docs"))

GATEWAY  = os.environ.get("CHROMA_GATEWAY_BASE", "https://127.0.0.1:8000").rstrip("/")
TENANT   = os.environ.get("CHROMA_TENANT", "default_tenant")
DATABASE = os.environ.get("CHROMA_DATABASE", "default_database")
COLL     = os.environ.get("CHROMA_COLLECTION", "smoke_test_docs")
DOCS     = (sys.argv[1] if len(sys.argv) > 1
            else os.environ.get("CHROMA_DOCS_DIR", _DEFAULT_DOCS))
BASE = f"{GATEWAY}/api/v2/tenants/{TENANT}/databases/{DATABASE}"
DIM  = 8
CTX  = ssl._create_unverified_context()  # -k equivalent; swift local check


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
            headers={"Authorization": f"Bearer {WR}", "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, context=CTX)
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def fake_vec(text):
    """Deterministic PLACEHOLDER embedding (NOT semantic) from a hash."""
    h = hashlib.sha256(text.encode()).digest()
    return [(h[i] / 255.0) for i in range(DIM)]


# 1) create collection (get_or_create)
st, res = call("POST", "/collections", {"name": COLL, "get_or_create": True})
print(f"[create] {st}  {res if st>=400 else 'ok id='+res.get('id','?')}")
if st >= 400:
    sys.exit(1)
cid = res["id"]

# 2) upload each top-level .md as one document
files = sorted(glob.glob(os.path.join(DOCS, "*.md")))
if not files:
    print(f"[warn]   no .md files under {DOCS} — nothing to upload"); sys.exit(1)
ids, embs, docs, metas = [], [], [], []
for f in files:
    txt = open(f, encoding="utf-8").read()
    ids.append(os.path.basename(f))
    embs.append(fake_vec(txt))
    docs.append(txt)
    metas.append({"source": os.path.basename(f), "bytes": len(txt.encode())})

st, res = call("POST", f"/collections/{cid}/add",
               {"ids": ids, "embeddings": embs, "documents": docs, "metadatas": metas})
print(f"[add]    {st}  files={len(ids)} -> {[os.path.basename(f) for f in files]}")
if st >= 400:
    print("  body:", res); sys.exit(1)

# 3) verify: count + fetch one back (retrieval by id — meaningful even with placeholder vecs)
st, res = call("GET", f"/collections/{cid}/count")
print(f"[count]  {st}  count={res}")
st, res = call("POST", f"/collections/{cid}/get",
               {"ids": [ids[0]], "include": ["metadatas", "documents"]})
if st < 400:
    meta = res.get("metadatas", [[]])
    print(f"[get]    {st}  id={ids[0]}  meta={meta}  doc_preview={repr((docs[0][:60]))}")
else:
    print(f"[get]    {st}  {res}")
