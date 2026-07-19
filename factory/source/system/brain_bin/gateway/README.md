# brain gateway — stack artifacts

The authored, reviewable source for the brain's **inspecting TLS gateway** stack — the one
published surface fronting the sealed chroma + ollama backends and the action neurons.
Consumes the verified reference impl (`projects/lightweight-chroma-proxy/sample`) into the brain.
Design + rationale: ADR-0002 (read-access gateway) → **ADR-0015** (two-network inspection gateway)
→ **ADR-0017** (neuron bundles + the `:8443` path-router); `brain_workshop journal_007` onward.

These files are the source of truth; the live stack is a copy laid into the distro at
`~/docker/` by the provision recipe (`../provision/stage4_brain.sh`). **Authoring is
owner-side; deploying is a brain `runas` step** (see `../DEPLOYMENT.md`).

## The roles (ADR-0015/0017)

| Service | Role | Exposure |
|---------|------|----------|
| `chroma` | vector store | **SEALED** — `expose:`-only on `brain_net`, **token-required**, no host port |
| `ollama` | embeddings / LLM | **SEALED** like chroma on `brain_net` |
| `gateway` | nginx TLS gateway + **`:8443` path-router** | **the ONE published surface** — bridges `brain_net` + `neuron_net`; publishes chroma `:8000`, ollama `:11434`, and the action query API `:8443`; TLS always on |
| `fail2ban` | intrusion banning sidecar | shares the gateway netns; L3-drops IPs that trip repeated 403s |
| **input neuron** (`<brain>-input_neurons`) | ingest (**write**) | container on **`brain_net`** → `chroma:8000` / `ollama:11434` **directly** (scoped writer token), never via the gateway |
| **action neuron** (`<brain>-action_neurons`) | query (**read**) | the daemon shape (`action_neuron_api`) is a long-lived query server on `neuron_net` serving `/ask` on `:8080` **behind the gateway path-router** (the on-demand `action_neuron_cli` is a one-shot instead); holds only a reader token |

Why the gateway owns port 8000: plugins expect Chroma there, so the gateway takes it and
Chroma vacates it. WSL forwards distro-loopback ports to Windows, so gateway and Chroma
cannot share one — the gateway is the single door, Chroma sits behind it on `brain_net`.

## The `:8443` path-router (ADR-0017)

The gateway publishes ONE path-routing surface for the read/query side:

```
https://<host>:8443/{bundle}/{neuron}/<whatever the neuron serves>
```

It owns only the `/{bundle}/{neuron}/` prefix — it matches it, resolves the neuron's service on
`neuron_net` at **request time** via Docker DNS, and forwards the rest untouched. `{neuron}` is the
neuron's config name = compose service name = neuron_net DNS name. **Admission** is `brain.env`
`ACTION_ROUTE_ALLOW`: `any` (default, permit-any) or `registry` (default-deny via
`brain_etc/gateway/route_registry`, which also pins a non-default internal serve port, default
8080). The published `:8443` host port is `ACTION_GW_PORT`; `gateway_config.py` fails closed on a
host-port collision. Exposing it loads `compose.action-neuron-gateway.yaml` (when `ACTION_EXPOSE=on`),
which mounts the rendered `action.conf` into `/etc/nginx/action.d/` and publishes the port.

## Posture dial (secure by default — no lax/no-auth tier)

TLS is **always on**. The posture changes only the gateway's host **bind address** and
whether a firewall rule ships. Unified with the runtime-hardening dial in
`../provision/stage7_harden.sh` (`personal` / `server`; the old `dev` is removed).

| Posture | `GATEWAY_BIND` | Firewall | Cert | Fit |
|---------|----------------|----------|------|-----|
| **Personal** | `127.0.0.1` | none | self-signed | solo dev, personal/exec brain |
| **Server** | `0.0.0.0` | Windows Defender inbound rule (scope to subnet) | self-signed (+ LAN SAN) | small workgroup |
| **Enterprise** | `0.0.0.0` | deployer-managed ingress | **BYO cert** (overwrite `certs/`) | fully-unattended |

Personal still uses TLS: on a shared user space it buys **server-authentication**
(defeats a process squatting :8000), and only pays off if clients **verify** — so ship
`cert.pem` to clients with verify on (see below).

**Multi-brain hosting / port assignment.** Multiple brains on one host each forward their
distro loopback to the *shared* Windows loopback, so each needs a **distinct host port**.
Assign it (and, on Server, the matching per-brain firewall rule) with the host-side elevated
tool `system/brain_sbin/gateway_port.py` — it collision-checks the port, edits `~/docker/.env`
(`GATEWAY_PORT`/`GATEWAY_BIND`) + recreates the gateway via `run_as_brain`, and manages the
`AIOS-<brain>-gateway` Defender rule:

```
gateway_port.py show                          # brain→port map + who's listening
gateway_port.py set --port 8001               # Personal (loopback), no fw rule
gateway_port.py set --port 8001 --bind server # Server (0.0.0.0) + subnet fw rule
```
A port-only change needs no cert regen; switching to Server warns you to re-issue the cert
with a LAN SAN so off-box clients validate.

## Authz modes (operator choice at onboarding)

The gateway's admission decision is one of three `$allowed` maps in `nginx/nginx.conf.template`.
Onboarding selects one (default **B**); the `brain_sbin` token tooling writes it.

- **B — read-open / write-needs-token (DEFAULT).** Reads pass; writes require a writer token.
  The ADR's "read-access gateway" identity — reading plugins just work, store protected.
- **C — everything needs a token.** No client gets anything without a reader/writer token.
- **A — open RW pass-through.** Any client gets full RW (still TLS + token injection). Weakest.

Writer tokens live in `nginx/writer_tokens.map` (**admin-writable only**; the brain sees but
can't edit its own allow-list). Manage them with `system/brain_sbin/gateway_token.py` (runs as root
in-distro, dispatched via `run_as_brain.py --root`; edits the map + reloads nginx after an
`nginx -t` check). A token is printed once, at create/rotate:

```
run_as_brain.py --root --script gateway_token.py -- list
run_as_brain.py --root --script gateway_token.py -- create --label obsidian-readback
run_as_brain.py --root --script gateway_token.py -- rotate --label obsidian-readback
run_as_brain.py --root --script gateway_token.py -- revoke --label obsidian-readback
```

## Cert & trust

`gen-cert.sh` (runs in-distro at install) writes `~/gateway/gateway_out/{cert.pem,cert.key}`
(the gateway-owned TLS home — NOT under `~/docker`; these are the gateway's certs, not
docker's) with a correct SAN (Personal: localhost+127.0.0.1; Server: pass extra `DNS:`/`IP:`
args). Key is mode 600, readable only by the gateway identity (ACL primer: visible location,
protected key). Trust path: point client at `cert.pem` → import to OS/host trust store →
(last resort) verify-off. Enterprise BYO = drop your real `cert.pem`/`cert.key` in
`~/gateway/gateway_out/`.

## Logging

The gateway writes structured JSON access logs (role field, never the token) to
`~/logs/gateway/`, symlinked out of the distro for blue-team read, with a host-side 30-day
rollover. It's the single choke point → a natural front-door audit log even in mode A.

## Intrusion banning (fail2ban, ADR 0012)

The rate limit (429) *throttles* a burst; it has no memory. A `fail2ban` sidecar closes the
gap: it watches the JSON access log and **bans** a source IP that trips too many denials
(repeated 403 = token probing) in a window, then **self-heals** (auto-unban). It shares the
gateway's network namespace and drops banned IPs at L3 (iptables), in front of nginx — so
`nginx.conf.template` is untouched. Policy is on the seam (`brain_etc/fail2ban/jail.d/`): the
three knobs are `bantime` (self-heal), `findtime` + `maxretry` (failures per window). On/off
is `FAIL2BAN_ENABLE` in `.env`. It requires the gateway; leave it off on a self-contained brain.
Correctness depends on the log showing real client IPs (ADR 0012 §5) — the live-verify gate.

## Files

- `compose.yaml` — the base stack (chroma + gateway + ollama + fail2ban + the neuron bundle service
  blocks rendered from the `===NEURONS===` zone of `brain_etc/brain.env` by `neuron_compose.py` (default
  bundle) / `add_neuron_bundle.py` (additional bundles)).
- `compose.chroma-gateway.yaml` / `compose.ollama-gateway.yaml` / `compose.action-neuron-gateway.yaml` —
  the per-surface **exposure overlays** (publish `:8000` / `:11434` / `:8443`; layer them or the
  ports drop — see TROUBLESHOOTING §E1).
- `.env.example` — template; onboarding renders the live `.env` (incl. the generated `CHROMA_TOKEN`).
- `nginx/nginx.conf.template` + the generated `chroma.conf` / `ollama.conf` / `action.conf` /
  `internal.conf` / `njs/inspect.js` — the gateway config (authz modes + credential injection +
  the `:8443` path-router + content inspection). Rendered by `gateway_config.py`; do not hand-edit.
- token maps (`reader_tokens.map`, `writer_tokens.map`, `ollama_use.map`, `ollama_admin.map`,
  `action_tokens.map`) — rendered from `token_registry` by `gateway_tokens.py`; admin-only.
- `gen-cert.sh` — self-signed cert generation + trust guidance.

> **Live config seam:** on a deployed brain these are seeded/edited under
> `brain_etc/gateway/` (see `../../brain_etc.example/gateway/README.md`) and applied by
> `system/brain_sbin/reapply_brain_configs.py`. The files here are the factory canon source.

## Deploy (brain `runas` step)

Onboarding (admin, host-side) seeds `.env` + tokens, applies ACLs + the firewall rule (Server),
and distributes the cert. Bringing the stack up is a brain session — always **through the
`run_as_brain` bridge**, never a hand-crafted `wsl`/`runas` call. The canonical deploy runs the
full migrate recipe (`stage4_brain.sh`: lay stack → gen cert → down old → up new → self-verify):

```
# from an elevated console (logged in as the brain's keystore owner):
run_as_brain.py --wsl --script C:\Horizon.AIOS\brains\<brain_name>\brain_bin\provision\stage4_brain.sh -- personal
#   posture arg: `personal` (loopback, localhost cert) | `server` (0.0.0.0, needs firewall + LAN-SAN cert)
# verify over TLS through the gateway (also part of stage4's self-check):
run_as_brain.py --wsl -- "curl -s --cacert ~/gateway/gateway_out/cert.pem https://127.0.0.1:8000/api/v2/heartbeat"
```

Bridge contract (as of the 2026-07-02 fix): `--` is ssh-style — the payload is one shell string for
`bash -lc`; a caller-supplied `bash -lc <string>` is **unwrapped** (not re-wrapped), so both forms
work. Use `--script … -- <args>` for non-trivial shell — it passes a Windows script path (translated
to `/mnt/<drive>`) plus args as space-safe tokens. Canonical wording: `run_as_brain.py --help`.
