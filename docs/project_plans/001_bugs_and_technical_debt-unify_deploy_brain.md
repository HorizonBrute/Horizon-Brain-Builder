---
type: project_plan
title: "Project 001 — Unify the brain deployer (Bugs & Technical Debt)"
description: Running list of open bugs and technical debt for Project 001; every item cleared or deferred-with-reason before close.
tags: [project-plan, bugs, technical-debt, deployer]
timestamp: 2026-07-21
status: closed
---

# Project 001 — Bugs & Technical Debt

Every item must be **FIXED** or **explicitly DEFERRED with rationale and a destination** before the
project closes. Ids are stable: `BUG-001-K` / `DEBT-001-K`.

## BUG-001-1 — Linux gateway TLS cert never generated (false-green)
1. **Observed:** 2026-07-21. Live `dev_brain` gateway nginx crash-loops (`9+` restarts):
   `nginx: [emerg] cannot load certificate "/etc/nginx/certs/cert.pem": No such file or directory`;
   `~dev_brain/gateway/gateway_out/` empty. fail2ban stuck `Created` (shares nginx netns).
2. **Root cause:** `linux_deploy_brain.py:576` calls `gen-cert.sh <posture>` (e.g. `personal`), but
   `gen-cert.sh:26-30` treats positional args as extra SubjectAltName entries → malformed SAN
   (`...,personal`) → openssl exits nonzero → `set -euo pipefail` aborts before writing `cert.pem`.
   `provision_runtime:578` then prints "TLS cert generated" **without checking rc** (false-green).
   Windows calls the same script correctly with no arg (`stage4_brain.sh:99`).
3. **Severity/priority:** HIGH — takes the whole gateway down; must be cleared before close.
4. **Status:** FIXED in the trunk's Linux build (`windows_deploy_brain.py:_build_engine_linux`, Section
   2/3): the cert is baked by running `gen-cert.sh` with **no posture arg** (personal SAN), matching the
   Windows `stage4_brain.sh:99` call, and copied into the engine artifact `linux_engine/cert/`. The
   command sequence is asserted (compile + stubbed-run harness); end-to-end cert bake proven at Section 8.
   server-posture typed SANs land with the deploy-side cert placement (Section 4/6). Old
   `linux_deploy_brain.py:576` deliberately left unpatched (NOTE 001-3) — that driver is being retired.
   (Supersedes the earlier fix in the discarded clean-room `deploy_brain.py`, per NOTE 001-4.)

## BUG-001-2 — Linux `teardown --purge` false-greens when userdel fails (account not removed)
1. **Observed:** 2026-07-23, first live Section 8. `deploy_brain.py teardown --brain dev_brain --purge --yes`
   printed `[OK] account + home + brains/<brain> purged` and exited 0, but the account survived:
   `userdel: user dev_brain is currently used by process 1256`.
2. **Root cause:** `_cmd_teardown_linux` ran `userdel --remove` with `check=False` and then printed the
   "purged" OK line unconditionally — no rc check, no post-verify. The brain's `systemd --user` manager
   (kept alive by linger) still held the account, so `userdel` refused; the false-green hid it.
3. **Severity/priority:** MEDIUM — teardown reports success while leaving the account behind; a following
   redeploy then collides with a half-removed brain. Must be clear before close.
4. **Status:** FIXED (2026-07-23) — purge now tears the per-user session down first
   (`loginctl terminate-user` + `systemctl stop user@<uid>`), force-reaps stragglers (`pkill -KILL -u`),
   runs `userdel` with an rc check + one forced retry, and **verifies** both the account (`user_exists`)
   and the folder are actually gone — `die()`ing with a diagnostic otherwise. Compile-clean.
## BUG-001-3 — Linux deploy stage 3 crashes: Windows `icacls` invoked on Linux
1. **Observed:** 2026-07-23, first live Section 8 (after BUG-001-2 cleared). `deploy … --from-scratch`
   reached `[3/10] Stage code` then died: `FileNotFoundError` from `subprocess.run(["icacls", …])` in
   `_icacls_or_die` ← `_repair_staged_acls` ← `_stage_from_source`.
2. **Root cause:** `stage_package`/`_stage_from_source` is shared by both OSes, but line 1299 called the
   Windows-only ACL repair (`_repair_staged_acls`, which shells out to `icacls`) unconditionally. Linux
   has no `icacls`, and the copytree ran as root so the staged tree was left `root:root` — the brain
   (which runs via `sudo -u <brain>`) could not own/write its own code.
3. **Severity/priority:** HIGH — blocks every Linux deploy at stage 3. Must clear before close.
4. **Status:** FIXED (2026-07-23) — `_stage_from_source` now branches: `_repair_staged_acls` only on
   `_IS_WINDOWS`; on Linux it `chown -R <brain>:<brain>` the staged tree (the POSIX analog). Safe because
   `_provision_runtime_linux` (stage 5, after staging) re-locks `brain_etc` to `root:root` for the seam.
   Compile-clean; live-validated at the Section 8 re-run.

## BUG-001-4 — from-scratch Linux build can't resolve DNS in rootless containers (model seed + neuron build)
1. **Observed:** 2026-07-23, Section 8 (after BUG-001-3 cleared). `_build_engine_linux` stage 3 died:
   `ollama pull nomic-embed-text` → `lookup registry.ollama.ai … : connection refused` / `FAIL`. Probed:
   default-bridge containers cannot resolve (UDP/53 to `8.8.8.8` and `--dns 1.1.1.1` both fail); a
   **user-defined network resolves fine** (Docker embedded DNS `127.0.0.11`).
2. **Root cause:** the ollama-seed container (and neuron `docker build` RUN steps) ran on rootless
   Docker's **default bridge**, which has no embedded DNS — the container attempts plaintext UDP/53
   directly. This host **requires encrypted DNS (DoT/DoH) as a hardening control**, so plaintext UDP/53
   is intentionally unavailable; the container had no legitimate resolver path. `docker pull` (stages 2)
   works because that resolves **daemon-side** (host), not in a container.
3. **Severity/priority:** HIGH — blocks every rootless `--from-scratch` build at model-seed. Must clear
   before close.
4. **Status:** FIXED (2026-07-23) — `_build_engine_linux` creates a build-scoped **user-defined network**
   (`brain-build-net-<brain>`) and runs the seed container + neuron builds on it (`--network`). Containers
   then use Docker's embedded resolver (`127.0.0.11`), which forwards via the **daemon's host-side
   resolver = the host's encrypted DNS** — so containers emit NO plaintext DNS, honoring the hardening
   control (and closing the earlier container→LAN DNS-egress concern). Deliberately **no `--dns`** (that
   would bypass the control). Network is torn down after the neuron stage (idempotent pre-clean each run).
   Compile-clean; live-validated at the Section 8 re-run.

## BUG-001-5 — from-scratch create-brain omits `brains` group → brain can't traverse to its own tree
1. **Observed:** 2026-07-23, Section 8 (after BUG-001-4 cleared). Model seed succeeded; stage 4 neuron
   build died: `docker build … input` → `unable to prepare context: path "…/common_neuron_platform/input"
   not found`. The path exists and is `dev_brain:dev_brain`, but `sudo -u dev_brain ls …/input` →
   `Permission denied`. `id dev_brain` showed `groups=dev_brain` only — **not in the `brains` group**
   (before teardown it was `dev_brain,brains`).
2. **Root cause:** the AIOS ACL model (/harden) makes `$INSTALL_ROOT` and `brains/` root:root and grants
   the shared **`brains` group `--x` traverse** (verified via getfacl: `group:brains:--x`). A brain reaches
   its OWN folder (brain:brain 0750) only by traversing those parents as a `brains`-group member. But the
   unified deployer's inline `_create_brain_linux` runs `useradd` + subuid + linger and **never joins the
   brain to `brains`** (no brains-group reference existed in the create path). `docker build` runs as the
   brain, so it could not read its own build context. (The original dev_brain had the membership because
   the standalone `factory/create_brain.py` provisioner sets it; the minimal inline create dropped it.)
3. **Severity/priority:** HIGH — any brain operation that reads under `brains/<brain>/` as the brain fails;
   blocks neuron build (and is a latent trap for other brain-run reads). Must clear before close.
4. **Status:** FIXED (2026-07-23) — `_create_brain_linux` now `groupadd -f brains` and `usermod -aG brains
   <brain>` (idempotent; verifies via `id -nG`). **Live-validated:** adding the crashed account to `brains`
   flipped `sudo -u dev_brain` access to the neuron context from `Permission denied` → `TRAVERSE_OK`.
   Relates to DEBT-001-4 (inline v1 create vs the fuller standalone create_brain.py).

## BUG-001-6 — neuron `docker build --network <custom>` rejected by BuildKit
1. **Observed:** 2026-07-23, Section 8 (after BUG-001-5 cleared). Traversal fixed; neuron build then
   died: `failed to build: network mode "brain-build-net-dev_brain" not supported by buildkit`.
2. **Root cause:** BUG-001-4 added the build-scoped user-defined network to BOTH the ollama-seed
   `docker run` (valid) and the neuron `docker build` (invalid) — **BuildKit only accepts `--network`
   `default|none|host`**, not user-defined networks. The neuron Dockerfiles DO need build-time DNS
   (`RUN pip install -r requirements.txt`; their own comment: "Build HAS internet; runtime does NOT"),
   so simply dropping `--network` would fail the pip step under the encrypted-DNS control.
3. **Severity/priority:** HIGH — blocks neuron image build on every rootless from-scratch deploy.
4. **Status:** FIXED (2026-07-23) — the neuron `docker build` now uses `--network=host`. **Tested both
   candidates** on the brain's rootless docker: legacy builder (`DOCKER_BUILDKIT=0`) + custom net →
   `DNS_RESOLVED` but prints "legacy builder is deprecated"; **BuildKit + `--network=host` →
   `DNS_RESOLVED`, exit 0** — modern and resolves via the host's own (encrypted) resolver, so it honors
   the hardening control (plaintext-to-LAN stays blocked; a successful resolve proves the encrypted path
   was used). Build-time only; runtime containers stay on compose's isolated `neuron_net`. The seed
   CONTAINER keeps the more-isolated user-defined net (embedded DNS). Supersedes the neuron-build half of
   BUG-001-4. Compile-clean; DNS path live-tested.

## BUG-001-7 — seam apply fails: brain can't read root:root 0660 generated config on the RO seam
1. **Observed:** 2026-07-23, Section 8 (after BUG-001-6). Stage 8 reached `apply manifest rebuilt` then
   `[ERROR] seam apply + stack recreate FAILED (rc=1)` / `apply_brain_truths: source missing on mount:
   docker/.env.rendered`. The file existed on the mount but `sudo -u dev_brain` was DENIED
   (`apply_brain_truths.sh:64` tests `[ -r ]`, not `[ -f ]`). Probing the whole manifest: EVERY
   stage-8-generated source was denied (`.env.rendered`, all `nginx_auto_gen/`, `token_maps_auto_gen/`,
   `fail2ban_autoconfigs/`); the stage-7-seeded sources were fine.
2. **Root cause:** `gateway_config.py` (+ token mint) generate the seam's derived config as ROOT, AFTER
   the stage-7 `chmod -R go=rX` brain_etc lock. The token-bearing outputs are `0660 root:root`
   (deliberately not world-readable). But `apply_brain_truths` runs AS THE BRAIN over the RO seam and
   must read every source — and the brain is neither root nor in group root. Nothing re-normalized the
   generated files for brain read.
3. **Severity/priority:** HIGH — final deploy stage fails; the stack is never applied/recreated.
4. **Status:** FIXED (2026-07-23) — after the gateway generation, `_gateway_linux` sets brain_etc group
   to the **per-brain group** and strips group-write (`chgrp -R <brain>` + `chmod -R g-w`). This
   faithfully translates the generators' own bits: `0660 -> 0640` (brain-group READ, never write),
   `0600` (token_registry) stays root-only, owner stays root (seam still read-only to the brain), world
   untouched (**tokens never become world-readable**). **Live-validated:** all manifest sources flipped to
   brain-readable; `.env.rendered` = `0640 root:<brain>` and `nobody` is denied; `token_registry` stays
   `0600` root-only. Compile-clean.
   - **Related hardening note (NOT this bug):** the seam mountpoint `/opt/brain_truths` is `0755` and the
     stage-7 lock makes the SEEDED config world-readable — `nobody` can read those (non-secret) files.
     Pre-existing design ("world-readable seam"); flagged for the owner, not changed here. **Now fixed by
     BUG-001-8.**

## BUG-001-8 — config-exposure seam is world-readable (world/other-brain can read seeded config)
1. **Observed:** 2026-07-23, hardening pass on the config-exposure seam (the "Related hardening note" left
   open by BUG-001-7). The seam (`/opt/brain_truths`, a `bind,ro` mount of `brains/<brain>/brain_etc`) was
   reachable by any local user: the mountpoint was `0755 root:root` (world-traversable) and the stage-7
   `_seam_linux` brain_etc lock did `chown -R root:root` + `chmod -R u=rwX,go=rX` — the `go=rX` left the
   SEEDED config world-readable. Verified: `sudo -u nobody cat /opt/brain_truths/docker/compose.yaml`
   SUCCEEDED (an unrelated user could read seam files). BUG-001-7's token-bearing GENERATED files were
   already protected (`0640`/`0600`), but the seeded template was not.
2. **Root cause:** the seam was never scoped to the owning brain. World kept traverse (`o=rx`) on the
   mountpoint and read (`o=r`) on every seeded file, so any local user or other brain could read one
   brain's non-secret config.
3. **Severity/priority:** MEDIUM — no token leak (secrets were already off-world via BUG-001-7), but a
   least-privilege gap: one brain's config exposed to `world`/other local users/other brains.
4. **Status:** FIXED (2026-07-23) — the seam is now reachable ONLY by root + the owning **per-brain group**
   (same name as the brain), never world. Two changes in `_seam_linux`:
   - **Mountpoint** `/opt/brain_truths` → `0750 root:<brain>` (only root + brain-group may traverse). Set
     right after the mkdir; because a live `bind,ro` mount is read-only, an idempotent redeploy stops the
     active `opt-brain_truths.mount` first, sets the underlying perms, then `enable --now` remounts.
   - **Stage-7 brain_etc lock** → owner stays root, group = per-brain group, world dropped:
     `chown -R root:<brain>` + `chmod -R u=rwX,g=rX,o=` (was `root:root` + `go=rX`). Seeded dirs become
     `0750 root:<brain>`, files `0640 root:<brain>`.
   This composes cleanly with BUG-001-7's later `chgrp -R <brain>` + `chmod -R g-w` (no regression): seeded
   files are already `0640 root:<brain>` so `g-w` is a no-op on them; generated `0660` files still resolve
   to `0640 root:<brain>` (brain reads, tokens off world); `token_registry` stays `0600` root-only.
   **Live-validated on the running dev_brain (install-root `/Horizon.AIOS`)** after a full
   `deploy --from-scratch` re-run (DEPLOY COMPLETE): `brain_doctor diagnose` = **HEALTHY** (4/4 containers,
   seam ro, gateway sealed mode C on :8443); `sudo -u nobody cat /opt/brain_truths/docker/compose.yaml`
   **DENIED**; all **26/26** sources in `wsl/apply.manifest` still readable by `sudo -u dev_brain`; brain
   still **cannot write** the seam (`touch` denied); mountpoint = `drwxr-x--- root:dev_brain`;
   `token_registry` still `0600 root:dev_brain` (`nobody` denied). Cross-refs BUG-001-7's Related hardening
   note. Compile-clean.

## BUG-001-9 — manual `run_as_brain.py` can't resolve the platform credential (bare-shell keyring seam)
> **Logged after project close (2026-08-04).** Recorded here — not in a new tracker — because it is the
> SAME provider-keyring seam this project built (`_export_provider_keyring_seam`, and commit `c025f1c`
> which extended it to `cmd_verify`/`cmd_status`; see the "verify/status hangs" note in
> `brain_bin/TROUBLESHOOTING.md`). This is the third and last entry point on that seam. Filed FIXED in
> the same commit, so the project's closed state is unchanged and **no reopen is warranted**.

1. **Observed:** 2026-08-04 (known-but-unfixed since 2026-07-18, carried as a manual workaround in
   `handoffs/2026-07-18_211324_2ndbraindevelopment.md:62-63`). In a bare elevated operator shell with no
   `BRAIN_KEYRING_*` exported, on the live AIOS-hosted `sorcerypunk_dev`:
   `python …\brain_sbin\run_as_brain.py --wsl -- 'docker ps'` printed
   `credential: retrieved from OS keystore (namespace 'brain:sorcerypunk_dev')` and then died with
   `Exception calling "Start" with "0" argument(s): "The user name or password is incorrect"`
   (CreateProcessWithLogonW). Setting `$env:BRAIN_KEYRING_SERVICE='horizon_aios'` fixed it instantly.
2. **Root cause:** `run_as_brain._keyring_namespaces()` could learn the host's keystore namespace ONLY
   from `$BRAIN_KEYRING_SERVICE`. That env seam is exported by a PARENT process
   (`deploy_brain._export_provider_keyring_seam`), so the deployer's own children were fine — but a
   MANUAL/standalone invocation inherits nothing, the AIOS never exports it into an operator shell, and
   the lookup fell through to the brain-owned `brain:<brain>` namespace. On this brain that namespace
   held a **stale** entry from a prior standalone incarnation, so the read "succeeded" with the wrong
   password (a silent-wrong-credential failure, not a clean miss). The AIOS writes the real credential to
   service `horizon_aios`, user `brain_account:<brain>` (`horizon_aios_brain_credential.py`
   `KEYRING_SERVICE`/`_keyring_username`) — usernames already matched on both sides; only the SERVICE
   name failed to reach the tool. `brain_installer_1_admin.py` had already worked around this with an
   `.aios_provision.json`-presence marker that hardcodes `horizon_aios`; `run_as_brain.py` may not
   (its header contract: the tool "carries no knowledge of any particular platform's convention").
3. **Severity/priority:** HIGH — every manual brain operation through the tool fails or, worse,
   authenticates with a stale credential; `gateway_port.py` inherits it (it dispatches through
   `run_as_brain`). Repeated account-logon failures also risk lockout under a lockout policy.
4. **Status:** FIXED (2026-08-04) — the host now ADVERTISES its namespace **in data the brain carries**,
   keeping the tool platform-agnostic. `run_as_brain._declared_keyring_namespace()` reads any provisioning
   record in the brain dir (`.*_provision.json` — the brain's own `.brain_provision.json` plus any host's
   sibling, e.g. the AIOS's `.aios_provision.json`) for an unbranded `"keyring_service"` (+ optional
   `"keyring_user"`, `{brain}` allowed). Precedence is preserved and now works in a bare shell:
   `$BRAIN_KEYRING_SERVICE` when set → else the record-declared namespace → **brain-owned
   `brain:<brain>`/`account_password` LAST**, so a stale entry can never shadow the host's credential.
   The same reader was mirrored into `brain_installer_1_admin.py` ahead of its branded marker fallback
   (which is kept for records written before the field existed). `gateway_port.py` needs no change — it
   has no keyring logic of its own and is fixed transitively. **Live-validated on `sorcerypunk_dev`** in a
   shell with zero `BRAIN_KEYRING_*` set: the same command that failed above now prints
   `credential: retrieved from OS keystore (namespace 'horizon_aios')` and returns the running container
   table. Both files `py_compile`-clean.
   - **Companion change required OUTSIDE this repo (not fixed here):** the Horizon.AIOS provisioner
     `$HORIZON_SBIN/horizon_aios_create_brain.py` must emit `"keyring_service": "horizon_aios"` (+
     `"keyring_user": "brain_account:{brain}"`) into `.aios_provision.json`, and existing brains need that
     field backfilled. Until it does, only backfilled brains resolve without the env seam.
   - **Related, NOT fixed:** `deploy_brain._keyring_namespaces()` still infers the platform namespace from
     `is_provider_host()` and hardcodes `horizon_aios`. Not a defect (it is the provider-side tool and is
     allowed that knowledge) and it is never the broken path, but it could adopt the same record reader
     for consistency → see DEBT-001-6.

## DEBT-001-6 — two keyring-namespace resolvers still infer the platform brand instead of reading it
1. **Decision/context:** after BUG-001-9, three `_keyring_namespaces()` implementations must stay in sync
   (`deploy_brain.py`, `brain_bin/deploy/brain_installer_1_admin.py`, `brain_sbin/run_as_brain.py`). Only
   `run_as_brain` is now purely declarative. The other two still carry a hardcoded `horizon_aios` behind a
   brand inference — `is_provider_host()` (a `$HORIZON_ROOT` directory test) and an
   `.aios_provision.json`-presence marker respectively.
2. **Action needed:** once the AIOS provisioner emits `keyring_service` (the companion change in
   BUG-001-9) and deployed brains are backfilled, drop both branded fallbacks so all three resolvers read
   the advertised field only — one contract, no brand knowledge below the provider layer.
3. **Impact:** LOW — the fallbacks are correct on a Horizon.AIOS host and harmless elsewhere (they are
   guarded by a provider test); this is contract purity plus one less place for the three copies to drift.
4. **Status:** DEFERRED (2026-08-04) → **destination:** blocked on the out-of-repo provisioner change;
   revisit when `.aios_provision.json` carries `keyring_service` by construction. Logged post-close
   alongside BUG-001-9; does not reopen the project.

## DEBT-001-1 — Linux deploy is missing the ollama_models and neuron_bundles stages
1. **Decision/context:** `linux_deploy_brain.py cmd_deploy` (8 stages) has no `ollama_models` and no
   `neuron_bundles` stage; Windows has both (stages 8–9). A Linux brain never syncs its model roster
   or starts neuron bundles.
2. **Action needed:** the unified `deploy()` (Section 4) must include both stages for both OSes.
3. **Impact:** MEDIUM — a Linux brain's `/ask` 404s until models are pulled; neurons never run.
4. **Status:** ✅ **CLOSED (2026-07-23).** **Models: CLOSED** — `_build_engine_linux` seeds the ollama
   roster into the volume at build and `_deploy_engine_linux` restores it, so a Linux brain ships with
   its models (net-new; the old driver seeded none). **Neurons (DEBT-001-1b): NOW CLOSED** — added
   `_neuron_bundles_linux` as deploy stage 9 (renumber → 11 stages): scaffold-guard → `_deliver_data_seams`
   (already Linux-aware: copies impulses/ + knowledge/brain_ro into the brain home, chowns) → add
   `neurons` to `~/docker/.env` `COMPOSE_PROFILES` (idempotent — this also makes the residency unit's boot
   `docker compose up -d` start neurons, so persistence is free, no unit-template change) → `docker compose
   up -d --pull never` from the PRE-BAKED images (no runtime build/pull). `_verify_linux` now asserts
   neuron liveness (fatal only on `Exited (N≠0)`; one-shot input/CLI neurons' `Exited (0)` and the
   long-running API neuron's `running` are healthy), mirroring the Windows verify. **`brain_doctor.py`
   LinuxBackend made neuron-aware too** — its container probe now treats a `neuron` service that
   `Exited (0)` as one-shot-complete, not "down" (otherwise every neuron-bearing brain false-reports
   UNHEALTHY; relates to DEBT-001-3). **Live-validated on dev_brain:** DEPLOY COMPLETE (11/11), VERIFY
   PASSED with "neuron bundle(s) healthy: 3 container(s)", and `brain_doctor diagnose` = HEALTHY
   (`5/7 running … 2 one-shot done: action_neuron_cli, input_neuron_example`), `COMPOSE_PROFILES` carries
   `neurons`.

## DEBT-001-2 — Several Windows engine-build steps have no Linux equivalent
1. **Decision/context:** unattended-upgrades policy (`stage5_root.sh`), maintenance tools + timers
   (`stage6_root.sh`/`stage6_brain.sh`), the slir4netns port-driver pin (`stage3_brain.sh`), and the
   full harden posture (`stage7_harden.sh`) are absent from the current Linux path.
2. **Action needed:** the shared build-engine (Section 2) runs these portable stage scripts on both
   OSes, closing the gap for free.
3. **Impact:** MEDIUM — Linux brains lack auto-updates, maintenance timers, and full hardening.
4. **Status:** ✅ **CLOSED (2026-07-24, owner decision).** Re-examined per script: `stage2b/3/5/6/7` are
   **WSL-distro-OS provisioning** — they configure the per-brain *WSL distro's operating system*
   (disable rootful docker in the distro, install rootless docker in the distro, apt
   unattended-upgrades, `systemd --user` maintenance timers, harden the distro's wsl.conf/automount).
   On Linux there is **no per-brain OS image**: the brain is a user account + rootless docker on the
   shared host, and the "engine" is the docker artifacts (`images.tar` + `ollama_models.tar` + `cert/`),
   which `_build_engine_linux` already captures. Running host-level apt/hardening on every brain deploy
   would be wrong (mutating the shared host) — so these stages are **architecturally N/A on Linux**, not
   unfinished work. **One** piece was genuinely Linux-relevant — the real-client-IP requirement behind
   `stage3_brain.sh`'s port-driver pin (ADR-0012 §5: fail2ban can only ban an attacker if nginx logs the
   REAL source IP). Resolved as a **fail-closed assertion**, `_assert_real_client_ip` in
   `_provision_runtime_linux`: it inspects the brain's live rootlesskit and **refuses to deploy unless the
   networking provably preserves the source IP** — OK on `--net=pasta` (the modern rootless default, which
   preserves it) or `--port-driver=slirp4netns`; `die` on the masquerading `builtin` driver or anything it
   cannot prove. (Discovered live: this host runs **pasta**, not slirp4netns — pasta already preserves the
   IP, so an earlier "require slirp4netns" framing would have wrongly failed a correct host; the assertion
   targets the real requirement instead.) **Live-validated on dev_brain (pasta):** assertion passes,
   DEPLOY COMPLETE, VERIFY PASSED, brain_doctor HEALTHY. **Follow-up noted (not this item):** the shipped
   `fail2ban jail.d/gateway.conf` `ignoreip` still hard-codes slirp4netns's `10.0.2.0/24` host range —
   harmless under pasta (loopback + `172.16/12` cover the internal cases) but worth reconciling to pasta's
   range if/when a server-posture Linux brain is deployed → tracked as **DEBT-001-5**.

## DEBT-001-5 — fail2ban jail `ignoreip` hard-codes the slirp4netns host range (pasta hosts differ)
1. **Decision/context:** `factory/source/system/brain_bin/gateway/fail2ban/jail.d/gateway.conf` sets
   `ignoreip = 127.0.0.1/8 ::1 10.0.2.0/24 172.16.0.0/12`. The `10.0.2.0/24` is the **slirp4netns** host
   range; on a **pasta** host (the modern rootless default, e.g. this Fedora 44 box) host-local/forwarded
   traffic does not use that range. Surfaced 2026-07-24 while closing DEBT-001-2 (`_assert_real_client_ip`).
2. **Action needed:** make the internal-exempt range posture/backend-aware (detect pasta vs slirp4netns and
   set the correct host-local CIDR), or document the operator knob. Low urgency.
3. **Impact:** LOW — under personal posture it is moot (loopback only). Under server posture the stale
   `10.0.2.0/24` is merely an over-broad exemption of an unused range; loopback + `172.16/12` still cover
   the real internal cases, so it neither blocks bans of real attackers nor exempts them.
4. **Status:** DEFERRED (2026-07-24) → **destination:** revisit when a server-posture Linux brain is
   first deployed — verify real external IPs are logged + bannable under pasta, and reconcile the exempt
   range. Consciously carried past Project 001 close (LOW, inert under pasta/personal).

## DEBT-001-3 — brain_doctor probes must track the unified deployer's outputs
1. **Decision/context:** `brain_doctor.py` already dispatches per-OS, but its residency/seam/stack
   probes assume the current per-driver artifact names. If unification changes any (unit name, seam
   path, engine layout), the doctor's checks drift.
2. **Action needed:** (a) re-verify `brain_doctor diagnose` against a brain deployed by `deploy_brain.py`
   (Section 8) and adjust probes if needed; (b) **rewire `brain_doctor.py`'s LinuxBackend** to import
   `deploy_brain` instead of `linux_deploy_brain` and map its primitive calls (`ldb.as_brain`/`ldb.brain_sh`)
   onto the trunk's names (`run_as_brain_argv`/`_brain_sh`) — this is the LAST blocker to DELETING
   `linux_deploy_brain.py` (Section 7). The WindowsBackend is already repointed to `deploy_brain`.
3. **Impact:** LOW-MEDIUM — stale probes mis-report health; the lingering driver is a dead file kept only
   for the doctor's Linux import (deploy no longer uses it).
4. **Status:** (b) **FIXED** (2026-07-23) — `brain_doctor.py`'s LinuxBackend now does
   `import deploy_brain as ldb`; the three renamed primitives are mapped (`brain_sh`→`_brain_sh`,
   `_docker_ready`→`_linux_docker_ready`, `require_root`→`require_admin`); all others keep their
   names. Both files `py_compile`-clean; a smoke test confirms every `ldb.*` attribute resolves on
   `deploy_brain`. `linux_deploy_brain.py` **deleted** (`git rm`) — Section 7 complete; no importer
   remains. **(a) CLEARED (2026-07-23)** → the Section 8 live teardown + from-scratch redeploy of
   dev_brain completed (VERIFY PASSED), and the **rewired** `brain_doctor diagnose` (importing
   `deploy_brain`) reports **HEALTHY** against that deploy_brain-built brain — 4/4 containers, stack
   active/enabled, seam ro, gateway sealed mode C on :8443, matching the pre-teardown baseline. Probes
   did not drift. **DEBT-001-3 fully closed.**

## DEBT-001-4 — Linux engine build runs as the real brain account, not an isolated build user
1. **Decision/context:** Windows `build_engine` runs in a throwaway scratch distro, so the build never
   touches the real brain account and the engine tar is fully portable/account-independent. The v1 Linux
   build (`_build_engine_linux`, NOTE 001-6) runs as the **real brain account** against its rootless
   docker — simpler (reuses `provision_runtime`'s setup) but gives Linux `build-engine` an account
   side-effect Windows lacks, and (v1) REQUIRES the brain account to pre-exist.
2. **Action needed:** optionally add a throwaway build user (`brain-build-<brain>`: own subuid/subgid +
   linger + rootless daemon + teardown) to match Windows' isolation, once Linux `create-brain` (Section
   4) exists to factor the account/rootless setup out.
3. **Impact:** LOW — a functional artifact is produced either way; this is isolation purity + parity.
4. **Status:** ❌ **CLOSED — WON'T-DO (2026-07-23, owner decision).** The real-account build already
   produces a correct, account-independent engine artifact (docker `images.tar` + ollama-volume
   `ollama_models.tar` + baked `cert/` under `system/linux_engine/`), live-proven across the Section 8
   from-scratch redeploys. The throwaway-build-user isolation is optional purity that mirrors exactly the
   WSL scratch-distro isolation the owner has determined Linux does not need (the Linux "engine" is the
   preserved docker artifacts, not a whole OS image). The only real side-effect — the build touching the
   real brain's rootless docker — is benign (the account is created by deploy anyway, BUG-001-5) and is
   overwritten cleanly on each redeploy. Not worth the added `brain-build-<brain>` account lifecycle
   (subuid/subgid + linger + rootless daemon + teardown). Reopen only if a future requirement needs the
   build to run with zero real-account side-effects.
