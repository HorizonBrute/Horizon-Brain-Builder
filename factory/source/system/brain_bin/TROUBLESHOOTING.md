# Brain Engine — Troubleshooting & Support Notes

Field-tested symptom → diagnose → cause → fix notes for a deployed brain (Chroma +
read-access TLS gateway on rootless Docker in WSL2). Written for an operator or support
agent working a live box. Companion to `DEPLOYMENT.md` (architecture + the happy path).

> **Running the ADR-0015/0017 Developer RAG stack** (4 services + neuron bundles on two private
> networks, with in-gateway content capture)? §§A–D below cover the base Chroma+gateway engine
> and residency, which still apply. The **RAG-stack-specific** failure modes — vanished published
> ports after a bare `run`, content-capture verification, the apply-rollback bug, and rebuild-vs-not
> — are in **§E** at the bottom. Operator model + knobs: `OPERATIONS.md`.

**Mental model in one line:** the brain runs in a WSL2 distro `AIOS-<brain>`; nothing is
reachable unless that distro is *booted*; a boot **residency/keepalive task** is what keeps
it booted; the **gateway** (nginx) is the only thing that answers on the host port, and it is
**hardened** (token-gated, TLS). Most "it's down" reports are really "the distro idled down."

> Naming note: the in-distro stack dir is **`~/docker/`** (renamed from `~/chroma/`) — it holds the
> **full chroma+gateway** compose (both containers), not just Chroma, hence "docker" not "chroma".
> Its data volume is **`~/knowledge/brain_rw/chroma`** (that IS Chroma's DB, in the `brain_rw` zone
> of the `knowledge/` data-in seam — brain invariant #4). The boot
> keepalive task is **`<brain>-docker-keepalive`** (renamed from `AIOS-<brain>-residency`). An
> already-deployed brain keeps the old names until re-deployed from a v0.4.0+ package.

---

## Fast triage (run these first)

All from the **brain's** Windows console (`whoami` → `<host>\<brain>`), because the distro is
registered under the brain's account and per-user WSL hides it from everyone else:

```
wsl -l -v                 # does AIOS-<brain> exist? STATE Running or Stopped?
wsl -l --running          # "no running distributions" == the distro is DOWN (the #1 cause)
```

If the distro is **Stopped/absent from --running**, the gateway *cannot* be up — go to
**§A (not persistent)**. If it's **Running**, go to **§D (gateway checks)**.

Boot + full triage in one (cold boot ~25–30s: systemd → user session → rootless docker):
```
wsl -d AIOS-<brain> -- bash -l -c 'id; systemctl is-system-running; docker ps -a'
```
Expect `uid=1000(<brain>)`, `running`, and both `<brain>-chroma` + `<brain>-gateway` containers
`Up` (they auto-start via `restart: unless-stopped` the moment docker starts on boot).

---

## §A — Gateway was up right after deploy, then stopped answering ("not persistent")

**Symptom.** `curl` to the gateway worked immediately post-deploy, but later fails
(connection refused / timeout). `wsl -l --running` shows **no running distributions**.

**Cause.** WSL2 shuts an idle distro's VM down. Without a keepalive holding it open, the
gateway is only up between accesses. A boot residency task is supposed to hold it — if the
gateway isn't persistent, that task is either **inert** (§B) or **exiting instead of holding**
(§C).

**Confirm.** Boot it by hand and see everything come up healthy:
```
wsl -d AIOS-<brain> -- bash -l -c 'docker ps'      # both containers Up == stack is fine
```
If the stack is healthy when booted by hand, the deployment is good — the problem is purely
the keepalive. Continue to §B/§C.

---

## §B — Residency task exists but never runs (inert)

**Symptom.** `schtasks /query /tn "<task>" /v /fo LIST` shows **`Last Result: 267011`**
(`0x41303` = *"task has not yet run"*) and it never changes; the distro never comes up at boot.

**Cause.** The task runs the brain under **Password logon**, which requires the brain account
to hold **`SeBatchLogonRight`** ("Log on as a batch job"). If it wasn't granted, the task can
never launch its principal — silently, with no error surfaced.

**Check (elevated):**
```
$sid=(New-Object System.Security.Principal.NTAccount("<brain>")).Translate([System.Security.Principal.SecurityIdentifier]).Value
$f="$env:TEMP\ur.cfg"; secedit /export /areas USER_RIGHTS /cfg $f | Out-Null
if ((Select-String "SeBatchLogonRight" $f) -match [regex]::Escape($sid)) {"BATCH: GRANTED"} else {"BATCH: MISSING"}
```

**Fix (elevated).** Grant it with the AIOS helper (or `secedit` by hand):
```
python <HORIZON_SYSTEM>\sbin\horizon_aios_brain_logon_rights.py grant <brain>
python <HORIZON_SYSTEM>\sbin\horizon_aios_brain_logon_rights.py check <brain>
```
> Code note: **fixed in package v0.4.0** — `residency.register()` now grants `SeBatchLogonRight`
> at **baseline** for every brain (via the AIOS LSA helper above) before it creates the task, so a
> fresh v0.4.0 deploy should never hit this. Seeing `267011` on a v0.4.0 deploy means the grant
> failed (check the deploy log for the `[WARN] SeBatchLogonRight` line) or the helper wasn't found
> (non-AIOS host) — grant it by hand as above. (Previously the right was gated behind
> `create_brain --automation scheduled`, which is why the first sorcerypunk_dev task was inert.)

---

## §C — Residency task launches but immediately exits (code 2), returns to Ready

**Symptom.** After §B is fixed, running the task shows `Status: Running` briefly then flips back
to `Ready`; the VM starts then idles down. Re-running by hand, the keepalive exits with a bash
**syntax error / exit code 2**.

**Cause.** The keepalive was an **inline shell string** with `$(...)` and nested quotes, e.g.
`wsl -d AIOS-<brain> -- bash -lc "cd ~/docker; for i in $(seq 1 30); do docker compose up -d && break; sleep 5; done; exec sleep infinity"`.
The `wsl.exe -- bash -lc "…"` invocation path (and the Task Scheduler XML `<Arguments>` round-trip)
**mangles** `$(seq 1 30)` and the nested quotes — the `for` loop is split across lines →
`syntax error near unexpected token '2'` → bash exits **2** *before* reaching `exec sleep
infinity`, so nothing holds the VM. (Same class as the `(a|b)` regex breaking under a
double-`bash -lc` wrap.)

**Fix — use a script file, never inline shell for anything non-trivial.** **Fixed in package
v0.4.0:** the deployer now ships this script by default (`residency.write_keepalive()` in phase 2)
and the task runs `bash -l <path>`, so a fresh v0.4.0 deploy is not inline and should not hit exit
2. If you are recovering an older deploy by hand, put the logic in a file inside the distro and
have the task run it as a **login** shell (so `/etc/profile.d` sets the rootless-docker env):

`~/keepalive.sh` (in the distro, owned by the brain):
```bash
#!/usr/bin/env bash
# Hold the WSL VM resident so the gateway stays reachable. Containers auto-start via
# restart:unless-stopped; the compose up is belt-and-suspenders.
cd "$HOME/docker" && docker compose up -d
exec sleep infinity
```
Point the task at it — **absolute path, `-l` for the login shell, no quotes/parens/`$()` on the
command line** (elevated):
```
schtasks /change /tn "<task>" /tr "wsl.exe -d AIOS-<brain> -- bash -l /home/<brain>/keepalive.sh"
```
(`/change` re-prompts for the run-as password on a Password-logon task — that's normal; it's the
brain's keystore password going back into the task's LSA credential, masked, not logged.)

**Verify the fix (elevated):**
```
schtasks /run /tn "<task>"
schtasks /query /tn "<task>" /v /fo LIST | findstr /I "Status Result"
```
- **`Status: Running` + `Last Result: 267009`** (`0x41301` = *"task is currently running"*) that
  **stays** Running = the keepalive is holding. 
- Back to `Ready` = it bailed again; re-check the script path/contents.

Decisive persistence proof: leave it idle ~15 min (or reboot), then `curl` the gateway **cold**
without touching the distro. If it answers, persistence is real.

---

## §D — Gateway checks & things that look like errors but aren't

**`curl … /api/v2/heartbeat` returns `403 Forbidden` from `nginx/…`.**
NOT an error — the gateway is **up and hardened**. The stack ships token-gated (mode B: writes
need a token; mode C: *everything* needs a token). A `403` from nginx means TLS terminated and
auth was enforced — the gateway is serving. For a `200`, pass a reader token:
```
curl.exe -sk -H "Authorization: Bearer <reader-token>" https://127.0.0.1:<port>/api/v2/heartbeat
```
Mint one with the token tooling (`gateway_token.py … create --label … --role reader`).

**`netstat` shows nothing on the gateway port, but `curl` works.**
Two reasons, both expected: (1) plain `netstat` lists only ESTABLISHED connections — use
`netstat -an | findstr :<port>` to see LISTENs; (2) under WSL2 **mirrored** networking the
listener lives in the distro's mirrored namespace with **no Windows process owning the socket**,
so it may not appear as a Windows listener even with `-an` — yet it's fully reachable (that's
what lets a LAN client reach it with no portproxy). Authoritative check is **inside** the distro:
```
wsl -d AIOS-<brain>            # then, at the bash prompt:
ss -ltnp | grep -E ':8000|:<port>'
```

**`wsl: Failed to translate 'C:\…'` spam on every `wsl` call.**
Harmless. `/mnt/c` automount is **off** on every posture (by design — a sealed engine shouldn't
mount the Windows FS), so WSL can't translate the Windows `$PATH` for interop. It's noise, not a
failure. (Tooling that needs a host path into the distro must be automount-independent — use an
in-distro script/drop dir, not a `/mnt/c` path.)

**`wsl: … ConfigureNetworking … 0x8007054f … falling back to networkingMode None`.**
Mirrored-networking init failure — and the **#1 remaining risk** for LAN reach now that deploy writes the
brain's mirrored `.wslconfig` automatically (see below). Deploy putting `networkingMode=mirrored` in the
brain profile does **not** guarantee mirrored actually engages: *sometimes* this is transient (retry /
restart the distro); on some hosts it is **persistent** — mirrored never engages and the VM stays NAT.
When mirrored works the port is reachable on loopback AND the LAN; under NAT an in-distro `0.0.0.0` bind
surfaces on the host as `127.0.0.1` only (via `wslrelay`) — **loopback, not LAN.** This is precisely the
host-side condition that live LAN verification must rule out on a fresh deploy.
Diagnose the mode from *inside* the distro: mirrored → shares a host IP + has a `loopback0` iface;
NAT → a `172.x`/`10.x` `eth0` and no `loopback0`.

**Server posture is reachable, but only on host loopback — not the LAN.** Two independent causes used to
be open deploy-tooling gaps; both are now **FIXED in tooling — implemented, pending live LAN verification**
(a fresh deploy + off-box test has not yet confirmed them end-to-end; see project NOTE 001-31). If an
off-box client still can't reach the RAG endpoint after a v0.4.0+ deploy, work through both in order:
- **`.wslconfig` is PER-WINDOWS-USER and must live in the BRAIN account's RESOLVED profile — now written by
  deploy.** `--posture server` flips the in-distro bind to `0.0.0.0`, but the mirrored networking that
  carries that to the LAN is configured in `%UserProfile%\.wslconfig` of **whoever launches the distro** —
  the *brain* account, not the operator. A mirrored `.wslconfig` in the operator's profile does **nothing**
  for the brain's VM. **And the brain's profile is NOT necessarily `C:\Users\<brain>`:** if a stale/leftover
  dir squats the name, Windows materializes the real profile suffixed (`C:\Users\<brain>.<MACHINE>`), and a
  `.wslconfig` written to the plain path is silently ignored — WSL reads the profile at the
  registry-recorded `%UserProfile%`. Deploy now RESOLVES the real path before writing: `brain_profile_dir()`
  forces a `LOGON_WITH_PROFILE` logon, gets the SID via `Get-LocalUser`, and reads
  `HKLM\...\ProfileList\<SID>\ProfileImagePath`; `write_brain_wslconfig()` (called from `create_brain()` in
  `windows_deploy_brain.py`, on both fresh-provision and redeploy paths, idempotent and non-clobbering)
  writes `[wsl2]` / `networkingMode=mirrored` into that RESOLVED `.wslconfig` (icacls read granted to the
  brain; surfaced as a visibility symlink at `brain_etc/wsl/.wslconfig`) before the distro's first boot.
  Confirm the file exists at the resolved profile and that mirrored actually engaged (the `0x8007054f` entry
  above is the remaining host-side risk; live LAN proof is the Phase 9 PROVE). NOTE: `wsl --shutdown` run as the
  *operator* does **not** restart the *brain's* separate utility VM (the residency keepalive revives it,
  unchanged `wslrelay` PID) — cold-restart it **as the brain** (`run_as_brain --brain <b> -- wsl -- …` after
  the brain's own `wsl --shutdown`) so the `.wslconfig` is read.
- **Firewall is now derived from the exposed GW surfaces (was: `--port` only).** `firewall_apply()` in
  `gateway_port.py` used to add an inbound rule for the single `--port` (chroma, e.g. 8000) — NOT the action
  path-router (8443, the real RAG endpoint) or ollama (11434), so off-box clients couldn't reach those even
  with mirrored working. It now reconciles Windows Defender rules against `brain.env`, opening one
  subnet-scoped rule per **exposed** surface — `AIOS-<brain>-gw-<surface>` (e.g. `-gw-action`, `-gw-chroma`,
  `-gw-ollama`), `-RemoteAddress LocalSubnet -Profile Private,Domain` — wherever `<SURFACE>_EXPOSE=on` AND
  `<SURFACE>_GATEWAY_BIND=0.0.0.0`. Loopback-bound surfaces get no rule (stale ones are deleted), the legacy
  single `AIOS-<brain>-gateway` rule is auto-retired, and `firewall_release()` removes all per-surface rules
  on teardown. Check the live rules with `Get-NetFirewallRule -DisplayName 'AIOS-<brain>-gw-*'` and confirm
  one exists for each surface you expect exposed.

---

## Task Scheduler result-code decoder

| Code | Hex | Meaning |
|------|-----|---------|
| 267011 | 0x41303 | Task has **not yet run** — usually a **missing logon right** (§B), not a real failure |
| 267009 | 0x41301 | Task is **currently running** — for a keepalive, this is the healthy steady state |
| 267008 | 0x41300 | Task is ready to run at its next time (idle, not currently running) |
| 0 | 0x0 | Last run completed successfully |
| 2 | — | (from the action) bash **syntax/misuse** — see §C (inline-shell mangling) |

---

## What to escalate to engineering (not operator-fixable)

- Keepalive still exits after the §C fix → the script itself or the distro's docker/systemd state.
- Mirrored networking failing persistently (not transient) → host WSL/Hyper-V networking config.
- Gateway serving but a **reader** token can reach **write** paths → stale token map; re-apply the
  current `nginx.conf.template` + `--force-recreate gateway` (security fix, gateway lane).

---

## §E — Developer RAG stack (ADR-0015/0017: two networks, neuron bundles, path-router)

The current Developer RAG brain runs **chroma + ollama + gateway + fail2ban** (resident) plus
**neuron bundles** — a per-collection pair of an **input neuron** (`<brain>-input_neurons`, write,
batch, on `brain_net` direct to chroma/ollama) and an **action neuron** (`<brain>-action_neurons`,
read, a long-lived query server on `neuron_net` behind the gateway `:8443` path-router). The
gateway bridges both nets and is the only published surface. Operator model, knobs, the bundle
model, and the change-and-apply loop: **`OPERATIONS.md`**. Design of record: ADR-0015/0017.

> ⚠️ **Factory-mirror parity.** This stack is **live-proven in `brains/sorcerypunk_dev`**. The
> factory now ships the two-network / bundle / path-router config as the `brain_etc.example/`
> template (seeded into `brain_etc/` at deploy and laid by `reapply_brain_configs.py`), but the
> factory **code** mirror (`factory/system/brain_sbin/gateway_config.py`) may still lag the live generator
> in spots. These notes describe the **live** stack; verify against the deployed brain before assuming.

---

### §E1 — THE BIG ONE: published ports vanish / external "connection refused" on :8000 or :11434

**Symptom.** External clients suddenly get **connection refused** on `https://<host>:8000`
(chroma) or `:11434` (ollama), usually **right after an ingest or a stray `docker compose run`**.
The stack looks up (`docker ps` shows the gateway `Up`), but nothing answers off-box.

**Detect (decisive):**
```
docker port <brain>-gateway
```
**Empty output = the published ports were stripped.** A healthy gateway shows
`8000/tcp -> 0.0.0.0:8000` and `11434/tcp -> 0.0.0.0:11434`.

**Root cause.** A bare `docker compose run input_neuron_example` (the run target is the neuron's compose
service name; or any `up`/`run` that does **not** layer the
exposure overlays) recreates the `depends_on` gateway from the **base `compose.yaml` only** — and
the base file defines **no published ports** (each service's exposure is a *separate* overlay:
`compose.chroma-gateway.yaml`, `compose.ollama-gateway.yaml`). The internal listeners survive (the
base file mounts them), so **internal ingest still works while the external door is gone** — which
is exactly why it slips by unnoticed.

**Fix — re-up with all three compose files layered:**
```
docker compose -f compose.yaml -f compose.chroma-gateway.yaml -f compose.ollama-gateway.yaml \
  up -d --force-recreate gateway fail2ban
```
Then re-check `docker port <brain>-gateway` — the two published ports should be back.

**Prevention — the ONE safe ingest command.** ALWAYS run neuron/ingest commands with the overlays
layered **and** `--no-deps` (the resident stack is already up, so don't let `run` touch it):
```
docker compose -f compose.yaml -f compose.chroma-gateway.yaml -f compose.ollama-gateway.yaml \
  -f compose.action-neuron-gateway.yaml \
  --profile neurons --profile gateway --profile ollama --profile fail2ban \
  run --rm --no-deps input_neuron_example --ingest-only
```
`--no-deps` is what stops the `run` from recreating (and clobbering) the gateway. This was proven
live to preserve the published ports. (Layer `compose.action-neuron-gateway.yaml` too when
`ACTION_EXPOSE=on`, so the `:8443` publish also survives.)

---

### §E2 — Stack health checks

```
docker ps                         # chroma, ollama, gateway Up; fail2ban Up; action neuron Up (query server)
docker port <brain>-gateway       # 8000->…:8000, 11434->…:11434, and (ACTION_EXPOSE=on) 8443->…:8443 (see §E1)
docker exec <brain>-gateway nginx -t   # config syntactically valid + all includes present (action.d/*.conf)
docker network inspect <brain>_neuron_net   # gateway + action neuron present; gateway at the static IP (172.30.7.2)
curl.exe -sk https://127.0.0.1:8443/<bundle>/action_neuron_api/health   # path-router reaches the action neuron
```
Expect the gateway on **both** nets: on `neuron_net` at `GATEWAY_NEURON_IP` (aliased
`chroma`+`ollama`), on `brain_net` reaching the real backends via `chroma-svc`/`ollama-svc`.
A `403` on an unauthenticated external request is **not** an error — it is the hardened gateway
enforcing token-role authz (same as base §D).

---

### §E3 — `apply_brain_truths.sh` rollback asymmetry (decouple sync from validation)

**Symptom.** After a config sync + a failing post-sync action, a generated file (e.g.
`njs/inspect.js`) goes **missing** from `~/docker/nginx/`, and the "rollback" leaves the stack in a
worse state than before.

**Root cause (a real latent bug — ADR-0015 Follow-up 6).** The sync **replaces** files with `mv`
(needs only parent-dir write → succeeds on root-owned runtime files), but the **rollback restores**
with `cp -p` (needs file write → **fails** on those same root-owned files with "Permission denied").
So a failed post-sync action leaves the NEW files in place and only `rm`s genuinely-new ones — the
rollback silently does **not** protect the live stack.

**Work around it (until the rollback is made `mv`-symmetric):**
1. **Sync clean with NO post-action** — do not rely on the apply's own validate-and-rollback.
2. **Validate separately in a throwaway container.** The static-IP internal listeners mean a plain
   `compose run gateway` collides on `172.30.7.2`; **override the throwaway's neuron_net IP** to
   dodge the collision. Write a tiny override, then `nginx -t`:
   ```
   # /tmp/gwval.yaml
   services:
     gateway:
       networks:
         neuron_net:
           ipv4_address: 172.30.7.9
   ```
   ```
   docker compose -f compose.yaml -f compose.chroma-gateway.yaml -f compose.ollama-gateway.yaml \
     -f /tmp/gwval.yaml run --rm --no-deps gateway nginx -t
   ```
3. Only if clean, recreate the live gateway (§E1 fix command).

Never trust a blind recreate of the live gateway — validate in the throwaway first.

---

### §E4 — Verifying content capture is working

Content capture is the whole point of ADR-0015 Phase 2. A **good** unified-log line (one JSON
object per request, `escape=json`) carries the uniform schema:
```
{"time":"…","remote_addr":"…","host":"…","method":"POST","uri":"/api/embeddings",
 "status":200,"body_bytes_sent":…,"request_time":…,"role":"writer","allowed":"1",
 "surface":"ollama-internal","level":"request",
 "req_headers":{"content_type":"application/json","content_length":"…","user_agent":"…",
                "accept":"…","x_forwarded_for":"…"},
 "req_body":"{\"model\":\"nomic-embed-text\",\"prompt\":\"…\"}","resp_body":""}
```
Check for:
- `"surface"` matches the listener (`ollama-internal` / `chroma-external` / …) and `"level"`
  matches the `*_INSPECT` knob for it.
- **`"req_body"` is populated** at level `request` — that is the captured content going to the
  service. (Empty `req_body` on a POST that *should* have one → the body wasn't buffered; confirm
  `client_body_buffer_size` is sized to the max body and that `$request_body` is referenced in the
  format, not pre-`set`.)
- **`"resp_body"` is empty** unless the surface is `request+response` (njs) — see §E5-njs.
- **`Authorization` is NEVER present** in `req_headers` — tokens must never land in the log. If you
  ever see a token in a log line, treat it as an incident.

Where it lands: `~/logs/gateway/access.log` (unified) or `inspect_<surface>.log` files + a
metadata-only `access.log` (split). `docker logs` will **not** show these — they are files under
the gateway's log mount, symlinked out of the distro for blue-team read.

**`request+response` (njs) is BUILT but UNPROVEN live.** No surface uses it yet. Before enabling it
in production, confirm against the running gateway: (a) the image actually ships
`modules/ngx_http_js_module.so` (`docker exec <brain>-gateway ls /etc/nginx/modules/`), and (b)
`js_body_filter`'s `r.sendBuffer` forwards correctly and `$insp_respbody` populates. `inspect.js`
carries a "VALIDATE LIVE" banner for this reason.

---

### §E5 — When a REBUILD is actually needed (vs almost never) — read before you `docker build`

**This is the #1 source of wasted time.** A neuron image is just the **dependency substrate**; the
neuron **CODE is bind-mounted read-only** from the code-in seam (`/opt/input_neurons` /
`/opt/action_neurons`), so a code edit alone needs **no rebuild**. A rebuild is only for the
substrate (deps changed).

**REBUILD (`docker build`) ONLY when a neuron's dependency substrate changes:** its `Dockerfile`,
`requirements.txt`, or the base image — for either `system/common_neuron_platform/input/` or
`.../action/`. Editing `common_neuron_platform/input/*.py` / `.../action/*.py` does **not** need a
rebuild (the code is mounted `:ro`;
recreate the service to pick it up). That is the **only** rebuild trigger.

**Do NOT rebuild — regenerate + sync + recreate instead — for ALL of these:**

| You changed… | Needs a rebuild? | Do this |
|--------------|------------------|---------|
| `brain.env` / `gateway.conf` knobs | **No** | `reapply_brain_configs.py` (`OPERATIONS.md` §4) |
| `nginx_auto_gen/*` / any generator (`gateway_config.py`, `ollama_gateway.py`) | **No** | same |
| `===NEURONS===` zone of `brain.env` (renders `sources.yaml` + bundles) / delivery config | **No** | `reapply_brain_configs.py` (neuron_compose / add_neuron_bundle renders compose) → re-run ingest |
| `route_registry` / `ACTION_ROUTE_ALLOW` | **No** | regen → recreate gateway (path-router is config) |
| token maps (`gateway_token.py …`) | **No** | the tool syncs + recreates the gateway itself |
| `compose.yaml` mounts/networks | **No** | recreate the affected service |
| `common_neuron_platform/input/*.py` / `.../action/*.py` code | **No** | code is mounted `:ro` — recreate the neuron service |
| a neuron's `Dockerfile` / `requirements.txt` (deps) | **YES** | `docker build` the substrate image, then run |

Do **not** rebuild "to be safe." Editing config/nginx/knobs/compose never needs an image build; the
gateway is stock `nginx:1.27` and the neuron image is just the dependency substrate. Reflexive
rebuilding is slow and changes nothing when the code layer is untouched.

---

## What to escalate (ADR-0015 stack)

- **`request+response` response capture** never populates `resp_body` after the §E4 checks pass →
  the njs module presence / `r.sendBuffer` contract (engineering; unproven-live path).
- **Ports keep vanishing** despite the §E1 prevention command → a caller in the operator's toolchain
  is still issuing a bare `run`/`up` without the overlays.
- **Rollback bug** (§E3) leaving the live stack degraded → the `apply_brain_truths.sh` fix
  (make the restore `mv`-symmetric) is a code change, not operator-fixable.
</content>
