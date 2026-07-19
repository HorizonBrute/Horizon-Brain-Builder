# Brain Deployment Guide (factory)

Authoritative deployment doc for a brain's engine — the **base engine layer** (import,
residency, backup). Generic across brains; substitute your own `<brain>` for the
`<brain_name>` examples. Supersedes the earlier Docker-Desktop-era notes
(`docker_readme.md`, `brain_login_howto.md`, `knowledge_verify_howto.md`), which describe an
abandoned model — see the SECURITY MODEL section for why.

Audience: an IT admin (or an agent) standing up or operating a brain.

> **Scope note.** This guide covers the base-engine mechanics (WSL import, residency, backup).
> The **normal deploy is the orchestrator** (`windows_deploy_brain.py` / `linux_deploy_brain.py`),
> which drives the full end-to-end onboarding — see **§0 (end-to-end flow)** and
> `deploy/README.md`. The current **Developer RAG stack** (ADR-0015/0017: chroma + ollama +
> gateway + fail2ban on two private networks, **neuron bundles** — input (write) + action (read),
> the `:8443` path-router, and in-gateway content capture) — its operator model, the
> `brain.env`/`gateway.conf` control panel, the ingest command, and the config change-and-apply
> loop (`reapply_brain_configs.py`) — is in **`OPERATIONS.md`**; its failure modes are in
> `TROUBLESHOOTING.md` §E.

## 0. End-to-end onboarding flow (the orchestrated path)

The orchestrator sequences every building block into one converging deploy. The base-engine
import (§3) is one stage of it; the full-contract flow (aligned with `deploy/README.md`):

1. **Preflight + create-brain** — OS user/group/workspace/keystore password.
2. **Stage the code** into `brains/<brain>/` — COPIED straight from the factory source tree
   (no tarball, no build step): code + `brain_etc.example/` + the neuron/impulse/knowledge
   scaffolds (tier-4 runtime state never clobbered). `deploy --package <tar>` opts into a
   prebuilt snapshot instead.
3. **Engine** — **build from scratch by default** (download base Debian → provision → import
   under the brain account, §3), first-boot,
   stack up; register the **residency** boot task (below) — its keepalive layers the `*_EXPOSE`
   port overlays so the published ports survive every boot.
4. **Config-exposure seam** — `brain_truths.py provision` **seeds `brain_etc/` from the packaged
   `brain_etc.example/` template** (`__BRAIN_NAME__` substituted) + mounts it RO at
   `/opt/brain_truths`.
5. **Gateway** — set port/bind + firewall, mint the bootstrap reader+writer tokens, then
   **`reapply_brain_configs.py` lays the full path-router stack** (chroma + ollama + gateway +
   fail2ban — regenerate → sync → force-recreate).
6. **Neuron bundles** — mount the code-in seams (`neurons_mount.py`), render bundles
   (`add_neuron_bundle.py`), build + start the `<brain>-input_neurons` / `-action_neurons` images
   (skipped with a notice when only the TEMPLATE scaffold is present).
7. **Verify** — per-service: no-token 403 / reader-token 200 / write-reset 403 through the gateway,
   per-service liveness (gateway + chroma fatal; ollama + neuron bundles reported), residency running.

> **`--posture server` today = correct in-distro bind. The two deploy-tooling gaps that used to keep it
> host-LOOPBACK-only are now FIXED in tooling; LAN reach is IMPLEMENTED but PENDING LIVE LAN VERIFICATION
> (a fresh deploy + off-box test has not yet confirmed it end-to-end — see project NOTE 001-31):**
> 1. **Mirrored networking is now installed per-brain (config resolved-and-written; ENGAGEMENT pending
>    live proof at Phase 9).** Server binds `0.0.0.0` inside the distro, but under WSL2 NAT that only
>    surfaces on host `127.0.0.1`. LAN reach needs `networkingMode=mirrored` in the **brain account's**
>    `%UserProfile%\.wslconfig` — the operator's `.wslconfig` does not govern the brain's VM. The catch:
>    that path is **NOT** a string-built `C:\Users\<brain>\.wslconfig`. If a stale/leftover dir squats the
>    name, Windows materializes the real profile suffixed (`C:\Users\<brain>.<MACHINE>`), and a `.wslconfig`
>    written to the plain path is silently ignored — WSL reads the profile at the registry-recorded
>    `%UserProfile%`. Deploy now RESOLVES the real profile before writing: `brain_profile_dir()` forces a
>    `LOGON_WITH_PROFILE` logon, gets the account SID (`Get-LocalUser`), and reads
>    `HKLM\...\ProfileList\<SID>\ProfileImagePath`; `write_brain_wslconfig()` (called from `create_brain()`,
>    both fresh-provision and redeploy paths, idempotent, non-clobbering) writes the mirrored `.wslconfig`
>    to that RESOLVED path before the distro's first boot, and surfaces it as a visibility symlink at
>    `brain_etc/wsl/.wslconfig`. **Remaining host-side risk:** on some hosts mirrored hits a persistent
>    `0x8007054f` fallback to NAT — writing the config to the correct place does NOT guarantee mirrored
>    ENGAGES, so live proof (distro `eth0` = host LAN IP + a `loopback0` iface, reachable off-box) is
>    deferred to the Phase 9 clean-reinstall PROVE.
> 2. **Firewall is now derived from the exposed GW surfaces (implemented, pending verification).**
>    `firewall_apply()` used to open only the chroma `--port` (8000), leaving the action path-router (8443)
>    and ollama (11434) with no inbound rule. It now reconciles Windows Defender rules against `brain.env`:
>    one subnet-scoped rule (`AIOS-<brain>-gw-<surface>`, `-RemoteAddress LocalSubnet -Profile Private,Domain`)
>    per surface that is actually exposed (`<SURFACE>_EXPOSE=on` AND `<SURFACE>_GATEWAY_BIND=0.0.0.0`);
>    loopback-bound surfaces get no rule and any stale rule is deleted. The legacy single
>    `AIOS-<brain>-gateway` rule is auto-retired, and `firewall_release()` removes all per-surface rules on
>    teardown.

### How a source edit reaches a brain

**There is no build/repackage step anymore.** A source edit **anywhere in the factory tree**
reaches the next `deploy` directly — both the orchestrator and the payload are now live:

1. **Orchestrator — run in place.** `factory/windows_deploy_brain.py` (and its Linux sibling
   `linux_deploy_brain.py`) is the installer you execute directly. Your edit to it is **LIVE on
   the next `deploy`** — as it always was.
2. **Payload — now COPIED from the source tree at deploy.** Everything under `factory/system/**`
   (e.g. `system/brain_sbin/gateway_port.py`, the keepalive, `gateway_config.py`) is staged by
   `_stage_from_source`, which COPIES the member set straight from the factory source tree into
   `brains/<brain>/system/...`. There is no tarball and no `build_package.sh` step in between, so
   **editing a payload file IS live on the next `deploy`** — no repackage. At runtime the brain
   still executes its **own staged copy**, never the factory original; that copy is just refreshed
   from source every deploy.

So the old "footgun" (editing `system/**` did nothing until you rebuilt the tar) **is gone.**
Both loops collapse to one:

- **Anything in the factory tree:** edit → `deploy` → brain has it.

> **The one exception** is the opt-in `deploy --package <tar>` snapshot path: it deploys a
> prebuilt tarball (built separately by `deployer_tooling/build_package.sh`) and therefore
> **freezes the code at build time** — a later source edit does not reach a `--package` deploy
> until you rebuild that tar. This path exists only for offline/pinned installs; the default
> needs no tarball.

A plain `deploy` (no `--from-scratch`) reuses the existing distro and just **re-stages the code +
re-runs gateway config** — the fast way to push a code fix onto a running brain.
`--from-scratch` rebuilds the engine from a clean base.

---

## 1. Architecture in one paragraph

The brain runs **ChromaDB on rootless Docker inside a dedicated WSL2 distro**. The
Docker daemon and all containers run as the brain's **Linux uid** (`<brain_name>`,
uid 1000) - never root. The distro is registered under the brain's **Windows
account**, so per-user WSL hides it from the owner (`\\wsl$` only shows the current
Windows user's distros). The distro's disk lives at **`wsl/disk/ext4.vhdx`**
inside the brain folder, so backing up the brain folder backs up the entire engine
+ data. Chroma itself is **sealed** on the private `brain_net` (expose-only, no host
port, token-required); the **nginx TLS gateway** is the one published surface and owns
port 8000, terminating TLS (Personal binds `127.0.0.1`, auto-forwarded to Windows
`localhost:8000` by WSL2) and proxying reads to `chroma:8000` internally. A no-token
request gets `403`, not a plaintext 200.

Why this shape: it satisfies the security invariant "the brain owns its engine,
services, and data" at the OS level, it is identical to a packaged Linux
deployment (rootless Docker in the brain's home), and it needs no GUI - the entire
lifecycle is CLI, so it can be driven with `runas` as the brain account.

## 2. The engine artifact (built from scratch, transient)

The engine is **built from scratch at deploy by default** — download a base Debian (WSL Store
meta-distro, or `--imagefile <rootfs>` for a pinned/offline base; no rootfs image ships in the
repo) → run the `../provision` recipe → `wsl --export`. The resulting
`wsl/<brain>_engine.tar` is only the medium for the Windows cross-account import hop and is
DELETED after a successful deploy (`--export-engine` keeps/relocates it; `--engine-tar <path>`
restores a prebuilt one instead of building). The tar is a `wsl --export` of the fully
provisioned distro; it contains:
- Debian + the brain Linux user (uid 1000, subuid/subgid for user namespaces)
- systemd enabled; `[user] default=<brain_name>`
- rootless Docker (docker-ce + compose plugin + rootless extras), enabled as a
  systemd **user** service with **linger** (runs headless, no login needed)
- the stack at `~/docker/` (compose.yaml + overlays + .env), data dir `~/knowledge/brain_rw/chroma`
- the maintenance layer (`~/bin/`, systemd user timers): auto-update + backups + log
- `unattended-upgrades` for OS + Docker packages
- the pre-pulled images: `chromadb/chroma`, `ollama/ollama`, `nginx` (gateway), `crazymax/fail2ban`
  (the full path-router stack). The per-brain **neuron bundle** images (`<brain>-input_neurons` /
  `-action_neurons`) are built at the deploy neuron-bundle stage (§0.6), not baked into this tar.

> The path-router stack itself is **laid at deploy, not baked** into this base tar: the
> orchestrator seeds `brain_etc/` from `brain_etc.example/` and runs `reapply_brain_configs.py`
> (§0.4–0.5). `stage4_brain.sh` intentionally builds only the base engine.

## 3. Deploy (register under the brain account) - one interactive step

Everything is CLI, so the admin drives it as the brain via `runas`. The password
is in the OS keystore:
`python C:\Horizon.AIOS\horizon_system\sbin\horizon_aios_brain_credential.py get <brain_name> --show`

1. Open a shell **as the brain**:
   ```
   runas /user:<brain_name> cmd.exe
   ```
   (enter the password at the prompt - this is the one thing that must be interactive)

2. Import the engine under the brain's account, VHDX into the brain folder:
   ```
   wsl --import AIOS-<brain_name> ^
     "C:\Horizon.AIOS\brains\<brain_name>\wsl\disk" ^
     "C:\Horizon.AIOS\brains\<brain_name>\wsl\<brain_name>_engine.tar" ^
     --version 2
   ```

3. Confirm it is registered under the brain (per-user WSL):
   ```
   wsl -l -v            :: should list AIOS-<brain_name>
   ```

   **First boot after import:** run `wsl --terminate AIOS-<brain_name>` once before
   bringing the stack up, so `wsl.conf`'s `systemd=true` + default-user take effect (mirrors
   the build-time restart). The next `wsl -d ...` cold-boots systemd → user session → docker.

4. Bring Chroma up and verify (Chroma is **sealed behind the TLS gateway** — verify over
   TLS with the stack CA, not a plaintext port):
   ```
   wsl -d AIOS-<brain_name> -- bash -lc "cd ~/docker && docker compose up -d && sleep 6 && curl -s --cacert ~/gateway/gateway_out/cert.pem https://127.0.0.1:8000/api/v2/heartbeat"
   wsl -d AIOS-<brain_name> -- bash -lc "ls -ln ~/knowledge/brain_rw/chroma"   :: files owned by uid 1000
   ```

5. Once verified, the source tarball can be deleted (the live engine is the VHDX):
   `del "C:\Horizon.AIOS\brains\<brain_name>\wsl\<brain_name>_engine.tar"`

The owner (non-brain) account will NOT see `AIOS-<brain_name>` in its `wsl -l`
or `\\wsl$` - that is the isolation working.

### Residency — keep the engine continuously reachable (REQUIRED for a live gateway)

WSL2 shuts an idle distro's VM down, so **without a keepalive the gateway is only
intermittently up**: every access after idle cold-boots (~25-30 s for systemd → user
session → rootless docker). `Linger=yes` brings the user manager back up on that boot, but
nothing *triggers* the boot between `run_as_brain` calls. For a deployed brain you want the
distro held open and the stack running across reboots.

Register a **per-brain boot Scheduled Task, run as the brain** (stored credential; "run
whether logged on or not"), that launches the distro and holds the VM open with a keepalive
process. The keepalive is a **script file** (`~/keepalive.sh`) shipped into the distro, and the
task runs it as `bash -l <path>` — **not** an inline `bash -lc "…"` string:

```
# The task action (as registered by deploy/residency.py):
wsl.exe -d AIOS-<brain_name> -- bash -l /home/<brain_name>/keepalive.sh

# ~/keepalive.sh inside the distro (owned by the brain, uid 1000):
#!/usr/bin/env bash
cd "$HOME/docker" && docker compose up -d
exec sleep infinity
```

- **Why a script file, not an inline action.** The raw `wsl.exe -- bash -lc "…"` round-trip
  mangles any quoting the Task-Scheduler `<Arguments>` string carries (wsl.exe re-splits it, then
  bash re-splits again). An earlier inline keepalive with a `for i in $(seq 1 30)` retry loop was
  double-wrapped, its `$( )` spliced newlines, `for` split on them, and bash died
  `syntax error near '2'` (exit 2) **before** `exec sleep infinity` — so the VM idled down and the
  gateway vanished. A file has no round-trip. See TROUBLESHOOTING.md and journal_015.
- **No retry loop.** `restart: unless-stopped` on the compose services already auto-starts both
  containers at boot, so the keepalive only nudges the stack up once and then `exec sleep infinity`
  to hold the WSL utility VM resident — the process inside the distro is what keeps the VM alive.
- Boot trigger (not logon): the brain never logs in interactively, so the task runs at boot under
  the brain's stored credential. The password comes from the OS keystore (credential-get at §3 top).

> **Automated in deploy:** the keepalive **script** is shipped in by `brain_installer_2_brain.py`
> (phase 2, step 5) — it is the only stage that runs AS the brain and can therefore see the brain's
> per-user WSL distro; an elevated admin session cannot reach it. The boot **task** is registered +
> started by `brain_installer_1_admin.py` (admin) after phase 2 returns, via the shared
> `deploy/residency.py` module (`register()` writes the Task Scheduler XML and imports it with
> `schtasks /xml … /ru <brain> /rp <pw>`). Admin-side because a boot-triggered "run whether logged
> on" task needs elevation to create AND the brain password to store — both of which only the admin
> phase holds.
>
> **`register()` also grants `SeBatchLogonRight` ("Log on as a batch job") to the brain — baseline
> for every brain.** A Password-logon task's principal cannot launch without it: the task sits inert
> at `Last Result 267011` (has-not-run) forever with no error surfaced. (This was the concrete reason
> <brain_name>'s first task was dead on arrival — the right used to be gated behind
> `create_brain --automation scheduled`.) The grant uses the surgical, additive AIOS LSA helper
> (`horizon_aios_brain_logon_rights.py`), not a `secedit` policy reimport.
>
> **Password logon is the default** (S4U registration for another user needs `SeTcbPrivilege`, which
> a plain admin lacks → `Access is denied`). Re-run is idempotent (`/f`);
> `installer_1 --skip-residency` deploys without the keepalive; `--residency-logon s4u` opts into the
> passwordless experiment. **Validated end-to-end by the LIFECYCLE-02 reboot gate (PASSED
> 2026-07-03):** after a full host reboot, with no manual step, the gateway answered and the task was
> `Running` (`Last Result 267009`) from a session-0 boot fire — persistence survives a real reboot.
> The deploy orchestrator's `verify` stage now asserts the task is `Running` (holding the distro), so
> a deploy can no longer false-green with the stack merely up-at-the-moment.

## 4. Update + backup policy (automatic, by design)

Target users are small/indie devs who will not hand-patch, so the brain keeps
itself current automatically. Aggressive updates make backups load-bearing.

- **OS + Docker + apt packages:** `unattended-upgrades` (security + updates + the
  Docker repo). Runs on the `apt-daily` timers.
- **Chroma:** `~/bin/chroma-update.sh` on a daily user timer (`chroma-update.timer`,
  ~03:30). Posture: **newest stable tag, always.** Each bump = pre-update snapshot ->
  pull -> health-check `/api/v2/heartbeat` -> **auto-rollback** if unhealthy.
- **Backups:** `~/bin/chroma-backup.sh` daily (`chroma-backup.timer`, ~03:00), plus a
  pre-update snapshot. Rotated, keep 7, in `~/backups/chroma_<ts>.tar.zst`.
- **Everything is logged** as JSON lines to `~/logs/brain-maintenance.jsonl`
  (schema: ts, host, component, event, result, from, to, detail, artifact).
- **WSL platform + kernel (Windows side):** schedule `wsl --update` (see section 6).

## 5. Operations

Run as the brain (from a `runas` shell, or `wsl -d AIOS-<brain_name> -- ...`):

```
cd ~/docker
docker compose ps                       # status
docker compose up -d / down             # start / stop (down keeps data)
docker compose logs -f chroma           # logs
systemctl --user list-timers 'chroma-*' # maintenance timers
cat ~/logs/brain-maintenance.jsonl       # update/backup history
ls -lh ~/backups                        # snapshots
```

Where things live (inside the distro / VHDX):
- stack: `~/docker/` (compose.yaml, .env)  - data: `~/knowledge/brain_rw/chroma`
- backups: `~/backups/`  - logs: `~/logs/`  - tools: `~/bin/`

## 6. Backup & restore (the "back up that folder" story)

Because the VHDX lives in the brain folder, **backing up the brain folder captures
the entire engine + data**. Two layers:

- **In-distro snapshots** (automatic, above): fast restore of just the vector data.
  Restore one:
  ```
  wsl -d AIOS-<brain_name> -- bash -lc "cd ~/docker && docker compose down && \
    rm -rf ~/knowledge/brain_rw/chroma/* && zstd -dc ~/backups/chroma_<ts>.tar.zst | tar -C ~/knowledge/brain_rw/chroma -xf - && \
    docker compose up -d"
  ```
- **Whole-engine backup** (disaster recovery), as a Windows scheduled task run as the
  brain:
  ```
  schtasks /create /tn "AIOS-<brain_name>-engine-backup" /ru <brain_name> /rp <pw> ^
    /sc DAILY /st 04:00 /tr "wsl --export AIOS-<brain_name> C:\Horizon.AIOS\brains\<brain_name>\wsl\backups\engine_%DATE%.tar"
  ```
  And keep WSL current:
  ```
  schtasks /create /tn "WSL-update" /ru SYSTEM /sc WEEKLY /tr "wsl --update"
  ```

## 7. Security / isolation model (and one honest caveat)

- The engine, containers, and data are owned by the brain's Linux uid. The owner
  account cannot run them.
- The distro is registered under the brain's Windows account; per-user WSL hides it
  from the owner entirely (not in their `wsl -l`, not in their `\\wsl$`).
- To reach the brain's data, one must become the brain (its password) OR, as a
  local **admin**, copy the `ext4.vhdx` out of the brain folder and mount it. Admin
  is god on the box - this fence is meaningful against the non-admin owner and other
  users, not against admin. (Same caveat as any single-box isolation.)
- Windows-side `knowledge/brain_ro/` is the owner->brain read-only corpus door (the old
  `knowledge/inbox/` drop-point was removed in the config-flow refactor); the vector store lives
  behind `knowledge/brain_rw/chroma`.

## 8. Troubleshooting

> **Deeper symptom→diagnose→cause→fix notes (support-grade) live in [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md)** —
> especially persistence/residency (distro idles down, keepalive inert or exiting), why a `403` and an
> empty `netstat` are *expected*, and the Task Scheduler result-code decoder.

| Symptom | Fix |
|---|---|
| `wsl -l` doesn't show the distro | You're not in the brain's Windows session - re-run under `runas /user:<brain_name>`. |
| `docker` "cannot connect" in the distro | rootless daemon not up: `systemctl --user start docker` (as brain); ensure linger: `loginctl enable-linger <brain_name>`. |
| `:8000` not reachable from Windows | container down (`docker compose up -d`), or WSL networking - `wsl --shutdown` then retry. |
| Update rolled back (see log `update_rollback`) | the new Chroma failed health-check; you're safely on the prior version. Investigate the image, then re-run `~/bin/chroma-update.sh`. |
| Need to restore data | section 6. |

## 9. Packaging notes (future)

This brain was built by hand-run staged scripts (see the workshop journal). To
"packagize": the staged provisioning scripts become an installer, and the engine
artifact (this `.tar`) becomes the shippable image. `onboard.py` remains the
admin-run host-prep seam. The compose file and the whole model are portable to a
native Linux host unchanged (rootless Docker in the brain's home).
