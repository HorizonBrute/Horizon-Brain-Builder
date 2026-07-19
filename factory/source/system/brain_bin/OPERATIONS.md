# Developer RAG Brain — Operator Guide (ADR-0015/0017 stack)

How to actually **run and change** the current Developer RAG brain stack without it being
voodoo. Companion to `DEPLOYMENT.md` (stand-up + residency) and `TROUBLESHOOTING.md`
(when it breaks). Audience: the brain **operator** (owner of `brains/<brain>`, a member of
`horizon_humans`, or an admin) — **not** the brain account. Substitute your own `<brain>`.

Design of record: ADR-0015 (two-network inspection gateway) → ADR-0016/0017 (the neuron
**bundle** model + the gateway **path-router**). Live-proven in `brains/sorcerypunk_dev`.

> ⚠️ **FACTORY-MIRROR PARITY.** This guide documents the **live/intended** behavior per the
> live brain + ADRs. The factory **code** mirror (`factory/system/brain_sbin/gateway_config.py`,
> `factory/brain_etc.example/`) may lag the live generator in spots. Treat every "the generator
> emits…" statement as true of the **live** generator; verify against the factory copy before
> assuming a freshly-packaged brain has every surface below.

---

## 1. Mental model — a bundle of neurons behind one inspecting door

The stack is **resident services** + **neuron bundles**, split across **two private Docker
networks**. The gateway is the one published door and the inspection chokepoint.

| Service | Image | Role | Where it lives |
|---------|-------|------|----------------|
| `chroma` | `chromadb/chroma` | vector store | `brain_net` only — **SEALED** (`expose`-only, no host port, token-required). Aliased `chroma-svc`. |
| `ollama` | `ollama/ollama` | embeddings / LLM | `brain_net` only — **SEALED** like chroma. Aliased `ollama-svc`. Embed model: `nomic-embed-text`. |
| `gateway` | `nginx:1.27` | the ONE published surface | Bridges both nets. Terminates TLS, enforces token-role authz, runs the `:8443` path-router, and **logs the content** of gateway-mediated traffic. |
| `fail2ban` | `crazymax/fail2ban` | intrusion banning | Shares the gateway's netns; L3-drops IPs that trip repeated gateway 403s. |
| **input neuron** | `<brain>-input_neurons` | ingest (**write**) | Runs on **`brain_net`**. Talks to `chroma:8000` / `ollama:11434` **directly** (scoped writer token), **never** the gateway. A batch job (`restart:"no"`) run on demand / by a timer. |
| **action neuron** | `<brain>-action_neurons` | query (**read**) | The daemon shape (`action_neuron_api`) is a long-lived query server on `neuron_net` serving `/ask` on `:8080`; the on-demand shape (`action_neuron_cli`) is a one-shot `--query` job. Its read path is **gateway-mediated**. Holds only a **reader** token. |

### The neuron BUNDLE model (ADR-0017)

A **bundle** groups an **input neuron** (write) + an **action neuron** (read) over **one**
Chroma collection: input neurons WRITE it, action neurons READ it. A bundle may hold several of
each. Bundles are declared in the `===NEURONS===` zone of `brain_etc/brain.env` (the retired
`brain_etc/neuron/{sources,bundles}.yaml` are no longer hand-authored). The **default** bundle
(`DEFAULT_BUNDLE`, e.g. `example_neuron_bundle`) is rendered into a managed region of
`brain_etc/docker/compose.yaml` by `system/brain_sbin/neuron_compose.py`; **additional** bundles are
rendered by `system/brain_sbin/add_neuron_bundle.py` (edit the zone + re-run generate; never hand-edit
the region). A source belongs to a bundle via its neuron's placement in the zone (flattened into the
runtime `sources.yaml`).

### The two networks and the write/read split (the topology that matters)

- **`brain_net`** — chroma + ollama + gateway + **input neuron**. The sealed interior. Backends
  have **no host port**. The input neuron resolves `chroma`/`ollama` by service name and writes
  **directly** (ADR-0017 moved the write side onto `brain_net` — it is the trusted writer).
- **`neuron_net`** — the **action neurons** + gateway. The read/query path is **mediated by the
  gateway path-router**. The action neuron holds only a reader token, so it physically cannot
  write — any write must go back through an input neuron (the write-funnel invariant, enforced by
  the network + token role, not by convention).

> **Reconciles the old single-neuron story.** Earlier docs said "the neuron is `neuron_net`-only
> and reaches chroma/ollama *only* through the gateway." That described the pre-bundle ADR-0015
> single neuron. After the ADR-0017 split, **the input (write) neuron is on `brain_net`, direct**;
> **gateway mediation is the action (read) side.** The gateway is still a mandated inspection
> chokepoint for the surfaces it fronts (the external chroma/ollama consumer surfaces and the
> `:8443` action query API).

### The `:8443` path-router (ADR-0017 §Next)

The gateway publishes ONE path-routing surface on `:8443`:

```
https://<host>:8443/{bundle}/{neuron}/<whatever the neuron serves>
```

The gateway owns ONLY the `/{bundle}/{neuron}/` prefix: it matches it, resolves the neuron's
service on `neuron_net` at **request time** via Docker DNS (variable-in-`proxy_pass`), and
forwards the rest of the path/method/body **untouched**. `{neuron}` is the neuron's config name =
its compose service name = its `neuron_net` DNS name.

- **Admission** is governed by `brain.env` **`ACTION_ROUTE_ALLOW`**:
  - `any` (DEFAULT) — permit-any: every `/{bundle}/{neuron}/` target routes. `route_registry` is
    then only needed to **pin a non-default internal serve port** (default 8080).
  - `registry` — default-deny: **only** the `{bundle}/{neuron}` targets listed in
    `brain_etc/gateway/route_registry` route; anything else → uniform JSON 404.
- **Ports:** internal serve ports are **path-multiplexed** behind the one `:8443` listener (they
  never conflict); the **published host ports** (`CHROMA_GATEWAY_PORT` / `OLLAMA_GW_PORT` /
  `ACTION_GW_PORT` = 8000 / 11434 / 8443) are the 1:1 external→container map — `gateway_config.py`
  **fails closed** if two exposed listeners claim the same host port (`check_gateway_port_conflicts()`).

### Consumer surface (the read/ask side from the operator's seat)

`impulses/query_client` is the client that queries the brain over the gateway RAG routes — a
shared `core.BrainClient` with a **CLI** (`cli.py`) and an **MCP frontend** (`mcp_server.py`,
tools `ask_brain` / `brain_health`, wired via `mcp.example.json`). These are convenience clients,
not part of the running stack. See `impulses/query_client/{README.md,DESIGN.md}` and §7.

---

## 2. The control panel — `brain.env` (stack posture)

`brain_etc/brain.env` is the human-named source for the Compose environment (synced to
`~/docker/.env`). It answers **what runs, what is published, where, and how loudly it logs**.
Tuning (rate limits, fail2ban) lives in `gateway/gateway.conf`; chroma/ollama server knobs in
`chroma/chroma.env` / `ollama/ollama.env`. **Never print token values** into any doc/ticket/log —
refer to variable *names* only.

### Lifecycle (which containers run at all)

| Knob | Values | Default | Meaning |
|------|--------|---------|---------|
| `COMPOSE_PROFILES` | csv of profiles | `gateway,ollama,fail2ban` | Master gate. The `*_ENABLE` knobs mirror into this. |
| `GATEWAY_ENABLE` / `OLLAMA_ENABLE` / `FAIL2BAN_ENABLE` | on/off | on | Whether that service runs at all. |
| `NEURON_SCHEDULE_ENABLE` | on/off | on | Install the ingest schedule timers (driven by each input neuron's `schedule:` in the `===NEURONS===` zone, rendered into the runtime `sources.yaml` source `tags:`). Off = manual `docker compose run` only. |

### Per-service gateway posture (chroma / ollama)

`*_EXPOSE` (publish the external listener), `*_GW_TLS` (off/enforced), `*_GW_AUTHZ`
(open / read-open / token-role for chroma; open / token / token-role for ollama),
`*_GATEWAY_BIND` (127.0.0.1 Personal / 0.0.0.0 Server), `*_GATEWAY_PORT` (8000 / 11434). Defaults:
exposed, TLS enforced, token-role, 0.0.0.0.

### Action query-API posture (ADR-0017)

| Knob | Default | Meaning |
|------|---------|---------|
| `ACTION_EXPOSE` | on | Publish the action query API via the gateway `:8443` (loads `compose.action-neuron-gateway.yaml`). |
| `ACTION_GW_TLS` | enforced | Plaintext vs https (reuses the gateway cert). |
| `ACTION_GW_AUTHZ` | token | `token` = caller must present a bearer with the `action:call` grant (→ `action_tokens.map`); `open` = no gate (bare-POC). Admission only — the neuron holds a reader token, so it cannot write regardless. |
| `ACTION_ROUTE_ALLOW` | any | Path-router target admission (§1): `any` permit-any / `registry` default-deny via `route_registry`. |
| `ACTION_GW_BIND` / `ACTION_GW_PORT` | 127.0.0.1 / 8443 | Loopback (Personal) vs host NIC (Server, + firewall). |

### Network segmentation

`NEURON_NET_SUBNET` (`172.30.7.0/24`) + `GATEWAY_NEURON_IP` (`172.30.7.2`, the gateway's **static**
IP on neuron_net) — feed both compose and `gateway_config.py`. Must not overlap another docker net.

### Internal-traffic inspection (the ADR-0015 deliverable)

Per-surface, per-direction **verbosity ladders**: `off | basic | basic+headers | request |
request+response` (default **`request`** = capture the request body via stock nginx). Knobs:
`CHROMA_INTERNAL_INSPECT`, `CHROMA_EXTERNAL_INSPECT`, `OLLAMA_INTERNAL_INSPECT`,
`OLLAMA_EXTERNAL_INSPECT`, and **`ACTION_EXTERNAL_INSPECT`** (the `:8443` question→answer surface —
escalate to `request+response` to capture the **synthesized answer**, the brain's first live use of
njs response-body capture). `GATEWAY_INSPECT_LOG` = `unified` (one `access.log`) or `split`
(per-surface files + a metadata-only `access.log` so fail2ban never goes blind).

### The tokens (names only — never values)

| Variable | Held by | Scope |
|----------|---------|-------|
| `CHROMA_MASTER_TOKEN_FOR_GW` | gateway + chroma only | master (injected upstream; generated at deploy) |
| `NEURON_TOKEN__<bundle>__<neuron>` | one input neuron | scoped `chroma:writer` + `ollama:use` |
| `NEURON_TOKEN__<bundle>__<neuron>` | one action neuron | scoped `chroma:reader` + `ollama:use` (read-only — cannot write) |

**Named tokens, no auto-mint (config-flow refactor).** A neuron references its gateway bearer BY
NAME — `gateway_token: <name>` in the brain.env YAML zone — resolved to a NAMED entry (`label`) in
`brain_etc/gateway/token_registry`. The deploy auto-mints any named token the shipped zone
references (`seed_neuron_tokens.py`, default posture: input=writer, action=reader); create more with
`gateway_tokens.py grant --label <name> --grant <service:role> …` and rotate with `… rotate --label
…`. The resolved secret is delivered to each neuron's container as `NEURON_TOKEN__<bundle>__<neuron>`
in `~/docker/.env`; `gateway_config generate` fails closed if a neuron names a token that does not
exist.

---

## 3. The ingest pipeline, end to end

Declared in the `===NEURONS===` zone of `brain_etc/brain.env` (which renders the runtime
`/etc/neuron/sources.yaml` the input container reads; the hand-authored `brain_etc/neuron/*.yaml`
are retired). Each source binds: `source ──(consumption adapter)──► neuron (named pipeline in a
bundle) ──► collection`.

### Delivery (get the bytes into `brain_ro`)

`delivery.adapter` picks how the source tree arrives in the read-only ingest zone
(`knowledge/brain_ro`, mounted `:ro` into the neuron at `/knowledge`):

| Adapter | Behavior |
|---------|----------|
| `on_disk` | a directory already present in `brain_ro`. |
| `scripted` | a script printing JSON-Lines `{"path":..,"text":..}` to stdout. |
| `git` | clone/pull a repo. `auth:` = `public` (keyless HTTPS) / `operator-delivered` (tooling writes the tree out-of-band, adapter clones nothing) / `transient-cred` (short-lived token from `github.env`'s `GITHUB_TOKEN_ENV`, injected on the one delivery run, discarded). See `brain_etc/github/`. |

`ingest_scope.include` (e.g. `["*.md"]`) filters **after** delivery — delivery grabs everything,
scope decides what is embedded (**delivery ≠ ingest-scope**).

### Ingest (chunk → embed → upsert)

The input neuron chunks each doc (LlamaIndex `SentenceSplitter`, `chunk_size`/`overlap` from the
manifest), calls **ollama** (`nomic-embed-text`) to embed, and **upserts** to the bundle's
**chroma** collection — **directly on `brain_net`** with the scoped writer token. Deterministic
ids + a content-hash dedup cache mean a re-ingest re-embeds only what changed.

### The ONE correct command to run an ingest safely

An ingest MUST layer the exposure overlays **and** pass `--no-deps` — otherwise a bare
`docker compose run` recreates the `depends_on` gateway from the **base** compose only and
**drops the published ports** (TROUBLESHOOTING §E1). From `~/docker` in the distro (dispatch via
`run_as_brain`):

```
docker compose -f compose.yaml -f compose.chroma-gateway.yaml -f compose.ollama-gateway.yaml \
  -f compose.action-neuron-gateway.yaml \
  --profile neurons --profile gateway --profile ollama --profile fail2ban \
  run --rm --no-deps input_neuron_example --ingest-only
```

The `run` target is the **compose service = the input neuron's name** (`input_neuron_example` in the
shipped example bundle). `--ingest-only` is the default (sources pre-delivered, mounted `:ro`).
Delivery (`--deliver-only`, the git-clone write phase) is a separate write-capable step; git sources
and their delivery are declared in the `===NEURONS===` zone of `brain_etc/brain.env`.

---

## 4. The config-generation model — generate, sync, recreate

**Nothing under `*_auto_gen/` is hand-edited.** You edit the human-named seam; the generator
renders the backend; a sync lays it into the running stack; a recreate picks it up. The whole
chain is wrapped by one apply primitive:

### `reapply_brain_configs.py` — the apply primitive (NAME IT, USE IT)

`system/brain_sbin/reapply_brain_configs.py` is the canonical wrapper the live brain uses:
**regenerate → sync → force-recreate** in one call. `--services <svc>` scopes the recreate;
`--no-pull` skips image pulls. It is what journal 032 runs to lay a config change. Under the hood
it drives the manual chain:

```
brain.env (posture) ─┐
gateway.conf (tuning)├─► gateway_config.py ─► gateway/nginx_auto_gen/
token_registry ──────┘   gateway_tokens.py ─► gateway/token_maps_auto_gen/
                                   │
                                   ▼   (build_manifest → wsl/apply.manifest, only if MANIFEST_PAIRS changed)
                                   ▼
                    apply seam (run AS the brain) ─► ~/docker/nginx/
                                   │
                                   ▼
                    docker compose … up -d --force-recreate <service>
```

- **Generator outputs** include `nginx.conf.template`, `ratelimit.conf`, `chroma.conf`,
  `ollama.conf`, **`action.conf`** (the `action_backend` upstream + the `:8443` path-router
  server), `internal.conf`, `njs/inspect.js`, and the token maps (`reader_tokens.map`,
  `writer_tokens.map`, `ollama_use.map`, `ollama_admin.map`, `action_tokens.map`).
- **ASCII only** in generated nginx / `inspect.js` (a stray non-ASCII char crashes generation;
  write "sec" not "§"). Token maps are **LF-only** — a stray `\r` has bitten this seam.

### The safe change-and-apply loop

1. **Edit the seam** — `brain.env` (including its `===NEURONS===` zone, which renders the runtime
   `sources.yaml`), `gateway.conf`, `route_registry`, `token_registry`, or a generator script.
2. **`python system/brain_sbin/reapply_brain_configs.py --services <svc> [--no-pull]`** — regenerates,
   syncs as the brain, and force-recreates the affected service. (Run the sync **as the brain** —
   the gateway is rootless Docker whose socket lives in the brain's `XDG_RUNTIME_DIR`; never root.)
3. **Recreate — not `nginx -s reload`.** Maps/configs are bind-mounted as individual **files**; the
   apply replaces each by atomic rename (new inode) — a reload would keep reading the stale inode.
   Recreating re-establishes the binds and re-runs `envsubst` (which injects `${CHROMA_TOKEN}` into
   `internal.conf` at container start).

> **Editing config/nginx/knobs does NOT need an image rebuild.** Only a change to a neuron's
> **code** (`system/common_neuron_platform/input/*.py` / `.../action/*.py`, its Dockerfile/requirements)
> needs `docker build`. See TROUBLESHOOTING §E5 — the #1 source of wasted time. (Code is bind-mounted
> `:ro` from `/opt/input_neurons` / `/opt/action_neurons`, so a code edit alone needs no rebuild —
> a rebuild is only for the dependency substrate image.)

**Reach the brain:** `system/brain_sbin/run_as_brain.py --brain <brain> --wsl -- "<cmd>"` from
`brains/<brain>`. ~1000-char command cap — for anything long, drop a script into `brain_etc/`
(visible read-only at `/opt/brain_truths/` in-distro) and run that.

---

## 5. Token flow — scoped in, master injected

Every neuron reaches `chroma`/`ollama` **only through the gateway** (aliased on its neuron_net)
carrying its OWN scoped named token (delivered as `NEURON_TOKEN__<bundle>__<neuron>`). The gateway
validates that scoped token, **strips** the client Authorization, and **injects** the master
`CHROMA_MASTER_TOKEN_FOR_GW` upstream. After ADR-0015 the master lives in exactly one place — the
gateway (and chroma itself).

- Named tokens are auto-minted for the shipped zone at deploy (`seed_neuron_tokens.py`); create more
  with `gateway_tokens.py grant --label <name> --grant <service:role> …` and rotate with
  `gateway_tokens.py rotate --label <name>`. Consumer reader/writer tokens for the external surface:
  `gateway_token.py create|rotate|revoke` (see `knowledge/gateway_token_admin_howto.md`).
- **Rule:** never place a live token literally on a command line — read it from `os.environ` / the
  `.env` inside the container.

---

## 6. The uniform log schema (one shape, every surface)

Every format emits the same JSON keys (populated to the surface's level) so the blue-team parses
one shape and `fail2ban`'s anchors never move:

```
time, remote_addr, host, method, uri, status, body_bytes_sent, request_time,
role, allowed, surface, level,
req_headers{content_type, content_length, user_agent, accept, x_forwarded_for},
req_body, resp_body
```

`req_headers` **never** includes `Authorization`. `req_body` is present at `request` /
`request+response`; `resp_body` only at `request+response` (njs). Lands in `~/logs/gateway/`
(symlinked out of the distro for blue-team read). `docker logs` will not show these.

---

## 7. Consumer surface — `impulses/query_client`

The read/ask side, driven from the operator's seat (not an in-container neuron):

- **`core.BrainClient`** — a token'd HTTPS client that hits the gateway RAG routes
  (`/{bundle}/{neuron}/ask` on `:8443`, verifying the stack CA).
- **CLI** (`cli.py`) — one-shot `ask` / `health` from a terminal.
- **MCP frontend** (`mcp_server.py`) — exposes `ask_brain` / `brain_health` as MCP tools so an
  agent can query the brain; wired via `mcp.example.json`.

These are convenience clients — safe to add/remove without touching the running stack. Design +
usage: `impulses/query_client/{README.md,DESIGN.md}`.

---

*Sibling docs: `DEPLOYMENT.md` (stand-up + residency), `TROUBLESHOOTING.md` (failure modes),
`gateway/README.md` (gateway stack), `../brain_etc.example/` (the config seam READMEs),
`knowledge/gateway_token_admin_howto.md` (consumer tokens).*
