# deploy/ - brain deployment (installer phase-split, driven by the orchestrator)

> **Entry point is the orchestrator, not these scripts directly.** Run
> `windows_deploy_brain.py deploy --brain <name>` (Windows) or
> `linux_deploy_brain.py deploy --brain <name>` (Linux). It drives the full ADR-0015
> onboarding end-to-end (below). The two installers here are the **engine-import phase
> split** it calls; `residency.py` is the boot-keepalive it registers. `../onboard.py` is
> **DEPRECATED** (Docker-Desktop-era host prep) — do not use it.

## What the orchestrator does end-to-end
1. **Preflight** + **create-brain** (OS user/group/workspace/keystore password).
2. **Stage the code** — COPIED straight from the factory source tree into `brains/<brain>/`
   (no tarball, no build step): code + `brain_etc.example/` + neuron/impulse/knowledge
   scaffolds. (`deploy --package <tar>` deploys a prebuilt snapshot instead — opt-in.)
3. **Engine**: **build from scratch by default** (download base Debian → provision → import);
   `--engine-tar <path>` restores a prebuilt one, `--from-scratch` forces a fresh rebuild.
4. **installer_1 (admin)** → **installer_2 (brain)**: `wsl --import` under the brain
   account, first-boot, stack up; **residency** boot task registered (keepalive layers the
   `*_EXPOSE` port overlays so ports survive every boot).
5. **Config-exposure seam**: `brain_truths.py provision` + seed `brain_etc/` from the
   `brain_etc.example/` template (`__BRAIN_NAME__` substituted) + RO mount at
   `/opt/brain_truths`.
6. **Gateway**: set port/bind + firewall, mint the bootstrap reader+writer tokens into
   `brain_etc/gateway/token_registry`, then **`reapply_brain_configs.py`** lays the full
   ADR-0015 path-router stack (chroma + ollama + gateway + fail2ban).
7. **Neuron bundles**: mount the code-in seams (`neurons_mount.py`), render bundles
   (`add_neuron_bundle.py`), build + start the `<brain>-input_neurons` / `-action_neurons`
   images. Skipped with a notice when only the TEMPLATE scaffold is present.
8. **Verify**: no-token 403 / reader-token 200 / reset 403 through the gateway, per-service
   liveness (gateway + chroma fatal; ollama + neuron bundles reported), residency running.

## The two installers (the phase split the orchestrator calls)
This is separate from *building* the engine image (that's the one-time
`../provision/` recipe).

## What gets staged into place
`deploy` copies the code member set from the factory source tree into `brains/<brain>/`
(`_stage_from_source`) — no tarball is unzipped on the default path:
```
<brain>/
  .brain_provision.json                     # brain identity (name)  [protected: never clobbered]
  system/brain_bin/deploy/brain_installer_1_admin.py
  system/brain_bin/deploy/brain_installer_2_brain.py
  system/brain_bin/DEPLOYMENT.md                  # ops/architecture reference
  brain_etc.example/                       # config-seam template
  knowledge/                               # data-in seam, zoned (created if absent):
    brain_ro/                              #   read-only source content (holds inbox/) → neuron :ro
    brain_rw/chroma/                       #   brain-written data (the vector DB)  [protected]
  wsl/<brain>_engine.tar                   # TRANSIENT — built from scratch at deploy, then DELETED
```
The `knowledge/` zones are fenced by `system/brain_bin/knowledge_lock.py` (default LOCKED on the inbox).
The engine is **built from scratch by default** (download base Debian → provision → `wsl
--export`); the resulting `<brain>_engine.tar` exists only as the medium for the Windows
cross-account import hop and is DELETED after a successful deploy (`--export-engine` keeps or
relocates it for backup). It carries a fully provisioned distro: Debian + rootless Docker +
the pre-pulled stack images (chroma, ollama, nginx/gateway, fail2ban) + the base stack + the
auto-update/backup maintenance layer. The per-brain neuron-bundle images + the full
path-router config are laid at deploy (steps 5–7), not baked in.

## Deploy (two runs)
Presumes the brain OS account already exists with correct permissions.

1. **Admin console:**
   ```
   python brain_bin\deploy\brain_installer_1_admin.py
   ```
   Grabs the brain password from the host credential helper when one is configured
   (`$BRAIN_CRED_HELPER`; prompts if absent or unset),
   ensures WSL2 is present/updated, ACLs the brain folder, then launches phase 2 as
   the brain. Use `--no-launch` to do host prep only and print the `runas` command.

2. **(auto, or manual) brain user:**
   ```
   runas /user:<brain> "python brain_bin\deploy\brain_installer_2_brain.py --brain <brain>"
   ```
   Creates the file structure, `wsl --import`s the engine **under the brain's Windows
   account** (so per-user WSL hides it from the owner), brings Chroma up, verifies
   heartbeat + brain-owned data.

That's the whole deploy. Everything else (updates, backups) is automatic thereafter.

## Regenerating the engine image
The shippable `engine.tar` is produced by the brain (it owns the distro):
```
wsl -d brain-<brain> -- bash -lc "cd ~/docker && docker compose down"   # quiesce
wsl --export brain-<brain> <path>\<brain>_engine.tar                     # run as the brain
```
(For a generic template image, run `../provision` against a fresh Ubuntu-24.04 and
export before first data is written.)

## Build-from-scratch vs prebuilt engine (a real choice)
- **From scratch (the DEFAULT):** deploy runs the `../provision` recipe on the target
  (needs internet + a few minutes) to build the engine in place, then discards the tar.
  Nothing multi-GB ships or is tracked. `--from-scratch` forces this even if a distro/tar
  already exists.
- **Prebuilt (opt-in `--engine-tar <path>`):** restore a pre-exported `<brain>_engine.tar`
  (~3 GB): deploy = import + up. Offline, fastest deploy, biggest artifact. Same entry
  points front either.

Long silent operations (base-image download, multi-GB export/import, Ollama model pulls)
print a periodic "… <label> — still running (Ns elapsed)" heartbeat, so a live deploy never
looks hung.

## Portability
On a native Linux host there is no WSL/import step: phase 2 collapses to `docker
load` (or pull) + `docker compose up` as the brain user in its home - the same
compose file, rootless. The two-script split (admin prep / brain run) still holds.
