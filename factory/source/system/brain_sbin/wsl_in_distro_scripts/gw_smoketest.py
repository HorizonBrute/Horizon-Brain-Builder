#!/usr/bin/env python3
"""
gw_smoketest.py — end-to-end gateway smoke test (Ollama embed -> Chroma add -> query).
================================================================================
Proves the full path a consumer takes THROUGH the gateway:
  1. read tokens from the RO config seam (/opt/brain_truths/gateway/token_registry),
  2. embed a few docs via Ollama  (POST /api/embed, ollama:use token),
  3. add them to Chroma          (v2 API, chroma:writer token),
  4. query them back,
  5. DELETE the collection (self-cleaning — leaves no residue).

Self-contained (stdlib only). Runs in-distro; reaches the stack on localhost via the
published gateway ports. Exit 0 = PASS. Intended to be removed once the path is proven.
"""
import json
import ssl
import sys
import urllib.error
import urllib.request

OLLAMA = "https://localhost:11434"
CHROMA = "https://localhost:8000"
CHROMA_DB = "/api/v2/tenants/default_tenant/databases/default_database"
REGISTRY = "/opt/brain_truths/gateway/token_registry"
COLLECTION = "smoketest_ephemeral"   # Chroma names must start/end with [a-zA-Z0-9]
PREF_EMBED = ("nomic-embed-text", "mxbai-embed-large", "all-minilm")
DOCS = [
    ("doc1", "The gateway injects Chroma's token upstream and enforces read/write roles."),
    ("doc2", "Ollama serves embeddings sealed on brain_net; the gateway publishes it with auth."),
    ("doc3", "A developer consumer talks only to the gateway, never to the sealed services."),
]
QUERY = "How does a consumer reach the sealed services?"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def call(method, url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=120) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:  # noqa: BLE001
        return "ERR", repr(e)


def load_token(grant):
    """First token in the registry holding `grant` (e.g. 'chroma:writer')."""
    with open(REGISTRY, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            body = line.split("#", 1)[0].split()
            if not body:
                continue
            tok, grants = body[0], body[1:]
            flat = [g for chunk in grants for g in chunk.split(",")]
            if grant in flat:
                return tok
    raise SystemExit(f"[FAIL] no token with grant '{grant}' in {REGISTRY}")


def die(msg):
    print(f"[FAIL] {msg}")
    raise SystemExit(1)


def main():
    print("== load tokens from the RO config seam ==")
    use_tok = load_token("ollama:use")
    write_tok = load_token("chroma:writer")
    print(f"  ollama:use  ...{use_tok[-6:]}   chroma:writer ...{write_tok[-6:]}")

    print("== 1. ollama /api/tags (use token) ==")
    st, tags = call("GET", f"{OLLAMA}/api/tags", use_tok)
    if st != 200:
        die(f"/api/tags -> {st} {tags}")
    models = [m["name"] for m in tags.get("models", [])]
    print(f"  200; models: {models or '(none)'}")

    embed_model = next((p for p in PREF_EMBED
                        if any(m == p or m.startswith(p + ":") for m in models)), None)
    if embed_model is None:
        if not models:
            die("no models present. Pull one with an ollama:admin token, e.g.\n"
                "  wsl_scripts.py run gw_pull.py -- nomic-embed-text   (see notes)")
        embed_model = models[0]
        print(f"  no preferred embed model; trying first available: {embed_model}")
    else:
        print(f"  using embed model: {embed_model}")

    print("== 2. embed docs via ollama /api/embed ==")
    st, emb = call("POST", f"{OLLAMA}/api/embed", use_tok,
                   {"model": embed_model, "input": [d for _, d in DOCS]})
    if st == 404:  # older ollama: singular endpoint, one prompt at a time
        vectors = []
        for _, d in DOCS:
            s2, e2 = call("POST", f"{OLLAMA}/api/embeddings", use_tok,
                          {"model": embed_model, "prompt": d})
            if s2 != 200:
                die(f"/api/embeddings -> {s2} {e2}")
            vectors.append(e2["embedding"])
    elif st == 200:
        vectors = emb["embeddings"]
    else:
        die(f"/api/embed -> {st} {emb}")
    dim = len(vectors[0])
    print(f"  200; {len(vectors)} vectors, dim={dim}")

    print("== 3. chroma: create collection (writer token) ==")
    st, col = call("POST", f"{CHROMA}{CHROMA_DB}/collections", write_tok,
                   {"name": COLLECTION, "get_or_create": True})
    if st not in (200, 201):
        die(f"create collection -> {st} {col}")
    cid = col["id"]
    print(f"  {st}; collection id {cid}")

    print("== 4. add docs+embeddings ==")
    st, res = call("POST", f"{CHROMA}{CHROMA_DB}/collections/{cid}/add", write_tok, {
        "ids": [i for i, _ in DOCS],
        "embeddings": vectors,
        "documents": [d for _, d in DOCS],
        "metadatas": [{"src": "smoketest"} for _ in DOCS],
    })
    if st not in (200, 201):
        die(f"add -> {st} {res}")
    print(f"  {st}; added {len(DOCS)} docs")

    print("== 5. embed query + query collection ==")
    st, qv = call("POST", f"{OLLAMA}/api/embed", use_tok, {"model": embed_model, "input": [QUERY]})
    q_vec = qv["embeddings"][0] if st == 200 else None
    if q_vec is None:
        s2, e2 = call("POST", f"{OLLAMA}/api/embeddings", use_tok, {"model": embed_model, "prompt": QUERY})
        q_vec = e2["embedding"]
    st, hits = call("POST", f"{CHROMA}{CHROMA_DB}/collections/{cid}/query", write_tok,
                    {"query_embeddings": [q_vec], "n_results": 2,
                     "include": ["documents", "distances"]})
    if st != 200:
        die(f"query -> {st} {hits}")
    docs0 = hits.get("documents", [[]])[0]
    dist0 = hits.get("distances", [[]])[0]
    print(f"  200; top hits:")
    for d, dist in zip(docs0, dist0):
        print(f"    dist={dist:.4f}  {d[:70]}")

    print("== 6. cleanup: delete collection ==")
    st, res = call("DELETE", f"{CHROMA}{CHROMA_DB}/collections/{COLLECTION}", write_tok)
    print(f"  delete -> {st}")
    # verify gone
    st, cols = call("GET", f"{CHROMA}{CHROMA_DB}/collections", write_tok)
    names = [c.get("name") for c in cols] if isinstance(cols, list) else []
    if COLLECTION in names:
        die(f"collection still present after delete: {names}")
    print(f"  confirmed removed (remaining collections: {names})")

    print("\n[PASS] full Ollama->Chroma path through the gateway works, residue-free.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
