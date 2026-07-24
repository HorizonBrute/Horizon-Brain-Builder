---
type: project_plan
title: "Project 001 — Unify the brain deployer (Bugs & Technical Debt)"
description: Running list of open bugs and technical debt for Project 001; every item cleared or deferred-with-reason before close.
tags: [project-plan, bugs, technical-debt, deployer]
timestamp: 2026-07-21
status: draft
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

## DEBT-001-1 — Linux deploy is missing the ollama_models and neuron_bundles stages
1. **Decision/context:** `linux_deploy_brain.py cmd_deploy` (8 stages) has no `ollama_models` and no
   `neuron_bundles` stage; Windows has both (stages 8–9). A Linux brain never syncs its model roster
   or starts neuron bundles.
2. **Action needed:** the unified `deploy()` (Section 4) must include both stages for both OSes.
3. **Impact:** MEDIUM — a Linux brain's `/ask` 404s until models are pulled; neurons never run.
4. **Status:** PARTIALLY CLOSED (Section 4). **Models: CLOSED** — `_build_engine_linux` seeds the ollama
   roster into the volume at build and `_deploy_engine_linux` restores it, so a Linux brain ships with
   its models (net-new; the old driver seeded none). **Neurons: STILL OPEN** — `_cmd_deploy_linux` builds
   neuron images into the engine but does not yet START them (no neuron-bundle bring-up / data-seam
   delivery stage on Linux, matching the old driver). Deferred to a follow-up neuron stage; tracked here.

## DEBT-001-2 — Several Windows engine-build steps have no Linux equivalent
1. **Decision/context:** unattended-upgrades policy (`stage5_root.sh`), maintenance tools + timers
   (`stage6_root.sh`/`stage6_brain.sh`), the slir4netns port-driver pin (`stage3_brain.sh`), and the
   full harden posture (`stage7_harden.sh`) are absent from the current Linux path.
2. **Action needed:** the shared build-engine (Section 2) runs these portable stage scripts on both
   OSes, closing the gap for free.
3. **Impact:** MEDIUM — Linux brains lack auto-updates, maintenance timers, and full hardening.
4. **Status:** OPEN → absorbed by Section 2.

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
   remains. (a) STILL OPEN → re-verify `brain_doctor diagnose` against a brain deployed by
   `deploy_brain.py` at Section 8 (the live teardown/redeploy of dev_brain now in progress).

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
4. **Status:** OPEN → revisit after Section 4 (Linux create-brain) lands.
