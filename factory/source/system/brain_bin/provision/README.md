# provision/ - how the brain engine was built (the recipe)

These are the staged scripts used to build the rootless-Docker-in-WSL2 engine, kept
for reproducibility and as the basis for a future packaged installer. They were run
by hand against a fresh `Ubuntu-24.04` WSL distro. See `../DEPLOYMENT.md` for the
architecture and for deploying the exported engine (the normal path - you do NOT
re-run these to deploy; you `wsl --import` the engine image).

## Build order

| # | script | run as | does |
|---|--------|--------|------|
| 0 | (Windows) `wsl --install Ubuntu-24.04 --no-launch` | admin | register base distro, no OOBE |
| 1 | `provision_stage2.sh` | root | brain Linux user, subuid/subgid, `/etc/wsl.conf` (systemd + default user), Docker CE + rootless extras, linger |
| 2 | (Windows) `wsl --terminate` | admin | restart so systemd + default user apply |
| 3 | `stage2b_root.sh` | root | verify systemd, disable the rootful docker daemon, confirm linger, `/etc/profile.d` rootless-Docker env (reachable by non-interactive login shells / the bridge) |
| 4 | `stage3_brain.sh` | brain | rootless Docker setup + systemd user service + DOCKER_HOST |
| 5 | `stage4_brain.sh` | brain | base Chroma stack (`~/docker`), data `~/knowledge/brain_rw/chroma`, up + verify |
| 6 | `stage5_root.sh` | root | `unattended-upgrades` (OS + Docker auto-update) |
| 7 | `stage6_root.sh` | root | `jq`, `zstd` (maintenance tools) |
| 8 | `stage6_brain.sh <scratch>` | brain | install maintenance scripts + systemd user timers |
| 8b | `stage7_harden.sh [posture]` | root | **hardening (standard shape)**: enforce non-sudo runtime, `/opt/input_neurons` execute-only + brain-truths RO mount, posture screws (the legacy code-in sync timer is inert by default) |
| 9 | `cleanup_brain.sh` | brain | pre-export cleanup (pristine image) |
| 10 | (Windows) `wsl --export` -> import under brain account | admin/brain | package + re-home (see DEPLOYMENT.md sec 3) |

## Installed maintenance scripts (shipped inside the engine at `~/bin/`)
- `brain-jlog.sh` - shared JSON-lines logger (schema in the file header).
- `chroma-backup.sh` - consistent rotated snapshot -> `~/backups`.
- `chroma-update.sh` - auto-update to newest stable, pre-backup + healthcheck + rollback.

## Hardening (stage 7) — the standard shape
Every brain is built toward a **least-privilege sandboxed runtime** (`../brain_security_model.md`,
invariants #6/#7). `stage7_harden.sh [posture]` enforces it:
- runtime brain uid is non-sudo (the recipe never grants sudo; stage 7 asserts it);
- `/opt/input_neurons` (+ `/opt/action_neurons`) is the neuron code the brain **executes but
  cannot write** — at DEPLOY these are RO drvfs mounts of the host shared platform image sources
  `system/common_neuron_platform/input/` / `.../action/` (via `system/brain_sbin/neurons_mount.py`),
  and the neuron images are built
  from them (ADR-0015: neurons are container bundles, not a `/opt/neurons` code sync);
- the brain-truths RO mount (`/opt/brain_truths` ← host `brain_etc`, path templated via
  `BRAIN_ETC_HOST`) exposes config read-only;
- the legacy git/rsync `neurons-sync.sh` timer is retained but **inert by default**
  (`TRANSPORT=none`), superseded by the `neurons_mount.py` host-dir mount.

**Posture profiles** (one artifact, a dial — secure by default, no lax tier):
`personal` (code+policy locked read-only) · `server` (+ `/mnt/c` automount off + egress
allowlist). The brain never writes its own code in any posture.

## The `knowledge/` data-in seam (zoning)
The corpus mount is **zoned by write-posture**, fenced by `system/brain_bin/knowledge_lock.py`:
- **`knowledge/brain_ro/`** — read-only source content the brain ingests; the owner→brain drop point
  (the old `inbox/` subdir was removed in the config-flow refactor); mounted `:ro` into the input
  neuron at `/knowledge`.
- **`knowledge/brain_rw/`** — brain-produced data a service writes; holds **`chroma/`** (the
  vector DB, `~/knowledge/brain_rw/chroma`). Granting the brain write here is the deliberate
  exception. See `knowledge/README.md` + brain invariant #4.

## Helper
- `verify_engine.sh` (brain) - one-shot rootless-engine health/ownership check
  (reports `/opt/input_neurons` + `/opt/action_neurons` posture).

## Deploy-time config (ADR-0015)
The base engine these stages build stops at chroma + rootless Docker. The full path-router
stack (chroma + ollama + gateway + fail2ban + neuron bundles) is laid at DEPLOY, not baked:
the orchestrator seeds `brain_etc/` from the packaged `brain_etc.example/` template and runs
`system/brain_sbin/reapply_brain_configs.py` (= `gateway_config generate` + manifest + seam apply +
force-recreate). So `stage4_brain.sh` intentionally builds only the base engine.

## Notes for packaging
- Brain identity is **parametrized**: pass `BRAIN=<name>` (or `BRAIN_NAME=<name>`) — there is
  **no baked default** anymore, so a forgotten name fails loud instead of silently
  provisioning the prototype. Brain-run stages default to `$(id -un)`. E.g.
  `BRAIN=my_brain bash provision_stage2.sh`.
  Still TODO: collapse stages 1-9 into one idempotent installer that takes the name as a param.
- The `wsl --export` image is the shippable artifact. `../onboard.py` is **DEPRECATED**
  (Docker-Desktop-era host prep) — the current entry point is the orchestrator
  (`windows_deploy_brain.py` / `linux_deploy_brain.py`).
- Everything is portable to a native Linux host (rootless Docker in the brain's home, same
  compose); `linux_deploy_brain.py` is the Linux orchestrator (static-validated, run live TBD).
