---
type: project_plan
title: "Project 001 — Unify the brain deployer (Status)"
description: Live per-item status for Project 001, plus serialized objective notes and mini-decisions.
tags: [project-plan, status, deployer]
timestamp: 2026-07-21
status: draft
---

# Project 001 — Status

Plan detail: `001_detail-unify_deploy_brain.md`.
Orientation (read this instead of a handoff): `001_current_orientation-unify_deploy_brain.md`.

**Status legend:** `NOT STARTED` · `IN PROGRESS` · `BLOCKED` · `DONE` · `VERIFIED`.
When a whole Section reaches `VERIFIED`, move its block into
`001_status_archive-unify_deploy_brain.md` and leave a one-line stub.

---

> **PIVOT (NOTE 001-4):** the plan now extends `windows_deploy_brain.py` in place. The prior
> "DONE" work lived in a clean-room `deploy_brain.py` that is being **discarded** — those sections are
> reset to the trunk framing. What survives is the *knowledge* (the OS-forced touchpoint list and the
> cert rc-guard contract), not that file's code.

## Section 1 — Platform switch inside the trunk
**Status:** IN PROGRESS · Landed in `windows_deploy_brain.py`: (a) platform seam foundation — `_IS_WINDOWS`/`_IS_LINUX`, `require_supported_os()` (honest macOS refusal, wired into `main`), `require_admin()`→Linux `os.geteuid()==0`; (b) **identity-switch helper** `run_as_brain_argv(path, brain, cmd, *, wsl, root)` — Windows reproduces the staged-bridge argv byte-for-byte (asserted), Linux emits `sudo -u <brain> -H bash -lc <cmd>`, and `root=True` is REFUSED on Linux (no analog — Section 4 seam). Converted ALL pure "run-as-brain" identity sites to the helper: `_probe_gateway`, the 6 `verify()` curl/docker probes, and the neuron-compose `up`. Also landed the two remaining Section-1 platform branches: `_deliver_data_seams` now has a real Linux branch (root `cp -r` merge + `chown` directly — no drvfs dance; `run_as_brain_argv(root=True)` refused on Linux by design), and `user_exists()` branches to `id <brain>` on Linux. Compiles; helper contract asserted on both platform branches (incl. root argv byte-for-byte). **Section 1 identity work COMPLETE.** Two `run_as_brain` sites remain but are NOT identity concerns — they belong to other sections: `distro_imported_as_brain` (a WSL boot-gate = engine-host, **Section 3**) and `cmd_teardown`'s account-target WSL-verb call (**Section 7**, Linux teardown is `userdel`/`rm`). Next OS-forced concept: engine-host + snapshot (Section 2/3).

## Section 2 — Fold Linux path into trunk `build_engine`
**Status:** IN PROGRESS · `build_engine()` now dispatches to `_build_engine_linux(args)` on Linux (Windows `wsl --export` path untouched). `_build_engine_linux` runs as the real brain account (NOTE 001-6) and does the 6-stage native build: ensure rootless-docker runtime → pull runtime images (`_runtime_image_refs`) → **seed ollama models into `<brain>_ollama_models`** (NET-NEW, via a throwaway seed container) → build neuron images (if Dockerfiles present) → bake cert (no-arg gen-cert, fixes BUG-001-1) → snapshot. Reuses the OS-portable `_runtime_image_refs`/`_runtime_model_roster` verbatim. Command sequence asserted via compile + stubbed-run harness; dry-run short-circuits. **Live-validated at Section 8.** Remaining: v1 requires the brain account to pre-exist (Linux create-brain = Section 4, DEBT-001-4).

## Section 3 — Linux engine artifact
**Status:** IN PROGRESS · Layout landed (NOTE 001-7): `system/linux_engine/{images.tar (docker save of runtime+neuron images), ollama_models.tar (volume tar), cert/{cert.pem,cert.key}}` + `linux_engine_dir()` helper. Producer side done in `_build_engine_linux` (brain writes tars to its home, root relocates into `linux_engine/`). **Resolved:** engine = `docker save` images + ollama-volume tar + cert bundle (NOTE 001-1). Remaining: the CONSUME side — `ensure_engine`/`deploy_engine` Linux branches (`docker load` + volume restore + cert place) land with Section 4's deploy stages.

## Section 4 — Fold Linux branches into trunk `cmd_deploy`
**Status:** NOT STARTED · **Depends:** 1, 2

## Section 5 — CLI parity on the trunk
**Status:** NOT STARTED · **Depends:** 4

## Section 6 — gen-cert hardening (BUG-001-1)
**Status:** OPEN (contract carries from discarded `deploy_brain.py`) — the no-false-green cert contract (no-arg gen-cert for personal, typed SAN only for server, posture word rejected fatally, hard rc + cert-existence check) is REKNOWN, not RE-landed: the Windows trunk already calls no-arg `gen-cert.sh` correctly at `stage4_brain.sh:99`. This section now = ensure the folded-in Linux cert path uses that same correct call with an rc check. VERIFIED end-to-end at Section 8.

## Section 7 — Migrate, retire, document
**Status:** NOT STARTED · **Depends:** 5

## Section 8 — Validation: rebuild dev_brain via unified path
**Status:** NOT STARTED · **Depends:** 4, 6

---

# Objective Notes & Mini-Decisions (serialized)

Append-only, newest at the bottom. One `NOTE 001-K` per decision/update. Grep-able: `grep "NOTE 001-"`.

## NOTE 001-1 | 2026-07-21 | Linux engine artifact = docker save + config bundle (CONFIRMED)
- Status: RESOLVED (user-confirmed 2026-07-21)
- ADR: none (self-contained; repo keeps no ADRs)
- Sections: 3, 2
- Context: Windows exports a WSL rootfs via `wsl --export`; Linux has no distro to export. The user
  chose "build-an-engine on both", so provisioning live (candidate c) is ruled out.
- Decision/Update: RECOMMEND candidate (b): `docker save` the pinned image list + a rendered
  config/cert bundle + an ollama-volume tar as the Linux engine artifact. Rationale: `docker save`/
  `load` is the portable analog of baking images into the rootfs tar; avoids the UID/overlay-store
  fragility of tarring the rootless data-root (candidate a). Awaiting user confirm before §2's Linux
  path is built. Section 3 stays BLOCKED until then.

## NOTE 001-2 | 2026-07-21 | gen-cert hardening can land first
- Status: OPEN
- ADR: none (self-contained)
- Sections: 6
- Context: BUG-001-1 (the cert false-green) is one small, portable change and is the origin of this
  whole project. It does not depend on the backend refactor.
- Decision/Update: Section 6 may land as the first product change (no-arg gen-cert + rc check),
  independent of the larger unification, so the shared cert contract is correct before §2 wires it in.

## NOTE 001-3 | 2026-07-21 | dev_brain stays down by design until the unified path exists
- Status: OPEN
- ADR: none (self-contained)
- Sections: 8
- Context: The live dev_brain gateway is crash-looping on the missing cert. Per the user, we are NOT
  hand-patching `linux_deploy_brain.py:576`; the fix arrives via the unified deployer.
- Decision/Update: dev_brain remains down until Section 8 rebuilds it through `deploy_brain.py`.
  Accepted tradeoff — recorded so a fresh agent does not "helpfully" patch the old line.

## NOTE 001-8 | 2026-07-23 | Rogue session hand-patched linux_deploy_brain.py (against instructions)
- Status: RESOLVED (user-confirmed 2026-07-23)
- ADR: none (self-contained)
- Sections: 4, 6, 7
- Context: On 2026-07-23 a SEPARATE Claude session — **against the user's explicit instruction and NOTE
  001-3** ("do NOT hand-patch `linux_deploy_brain.py:576`; the fix arrives via the unified deployer") —
  committed three direct fixes to the being-retired driver: `115fb51` gen-cert takes SAN entries not the
  posture word (BUG-001-1), `28fb91c` verify chroma on its own surface port, `b610eaa` server cert SAN
  covers ALL global IPv4s. These were unwanted, but they got dev_brain healthy (user now handles it).
- Decision/Update: user confirmed "keep consolidating" (2026-07-23). Disposition of the rogue commits:
  **leave them in place** — deleting `linux_deploy_brain.py` at Section 7 supersedes them, and reverting
  now risks the live dev_brain that depends on them. Their LOGIC is nonetheless CORRECT behavior, so the
  Section 4 port folds it into the trunk's Linux path (no-arg/personal + typed-SAN-all-global-IPv4 server
  cert; chroma verify on its own port). If the user later wants the history cleaned, revert them then.
  dev_brain is user-handled → no unsupervised live rebuild from me; Section 8 is a supervised run later.

## NOTE 001-6 | 2026-07-22 | Linux build identity = the real brain account (v1); throwaway build-user is debt
- Status: RESOLVED (this session)
- ADR: none (self-contained)
- Sections: 2, 3
- Context: Windows `build_engine` runs in a throwaway scratch WSL distro `brain-build-<brain>` as an
  arbitrary in-distro user, so the build never touches the real brain account and the engine tar is
  fully portable. On native Linux there is no distro; rootless docker is inherently per-uid (needs a
  real user, `/run/user/<uid>`, subuid/subgid, linger). A throwaway build USER would mirror Windows but
  needs its own subuid/linger/rootless-setuptool + teardown — significant fragile machinery.
- Decision/Update: v1 builds the Linux engine **as the real brain account** (reusing the rootless-docker
  setup `linux_deploy_brain.py:provision_runtime` already performs). The portable artifact (tars) is
  still produced for other hosts; on the common same-host case the brain ends up warm, so `deploy` is a
  fast path. **Divergence from Windows:** Linux `build-engine` has an account side-effect Windows' build
  does not, and (v1) REQUIRES the brain account to pre-exist — Linux `create-brain` lands in Section 4.
  **DEBT (DEBT-001-4):** throwaway build-user isolation to match Windows' account-independence.

## NOTE 001-7 | 2026-07-22 | Linux engine artifact layout = system/linux_engine/{images.tar, ollama_models.tar, cert/}
- Status: RESOLVED (this session)
- ADR: none (self-contained)
- Sections: 3, 4
- Context: Windows engine = one `wsl --export` rootfs tar at `system/wsl_engine/<brain>_engine.tar`. Linux
  has no rootfs to export; the decided artifact (NOTE 001-1) is `docker save` images + ollama-volume tar
  + cert/config bundle. Needs a concrete on-disk layout + naming helper analogous to `wsl_runtime_dir`.
- Decision/Update: `LINUX_RUNTIME_REL = ("system","linux_engine")`, `linux_engine_dir(brain_dir)` =
  `<brain>/system/linux_engine/`, holding: `images.tar` (`docker save` of the pinned image refs +
  built neuron images), `ollama_models.tar` (tar of the `<brain>_ollama_models` volume mountpoint), and
  `cert/{cert.pem,cert.key}` (the no-arg-gen-cert bake, fixing the `linux_deploy_brain.py:576` posture
  bug at its new home). Build/deploy split per NOTE-synthesis: BUILD = pull images + seed models + build
  neurons + gen cert + snapshot; DEPLOY = create acct/rootless + `docker load` + volume restore + place
  cert + render config + `compose up --pull never` (Section 4). Model seeding is NET-NEW (the Linux
  deploy seeds no ollama models today).

## NOTE 001-5 | 2026-07-21 | Centralize OS-forced concepts behind helpers, not scattered inline `if`s
- Status: RESOLVED (this session)
- ADR: none (self-contained)
- Sections: 1, 2, 4
- Context: A full touchpoint trace of the 3247-ln trunk showed the OS-forced surface is DENSER than the
  handoff's five-item summary: the identity switch is one concept realized at ~15 `run_as_brain --wsl`
  call sites, the WSL engine/snapshot at ~20 `wsl` calls, plus Windows-only SUBSYSTEMS with no clean
  Linux analog — brain-profile/`.wslconfig`/registry (`ProfileList`, mirrored networking), Win32/ctypes
  profile-handle release in teardown, PowerShell `Get-LocalUser`, Credential-Manager keyring. Putting an
  `if _IS_LINUX` inline at each of ~40 sites would make the file a thicket and risk regressing Windows.
- Decision/Update: realize "only OS-forced steps branch" by CENTRALIZING each OS-forced concept behind
  ONE helper that branches internally (e.g. one `run_as_brain(...)` emitting `sudo -u <brain> -H …` on
  Linux vs the `run_as_brain.py --wsl …` staged tool on Windows). Call sites stay unchanged; the Windows
  path is the untouched `else`. This satisfies NOTE 001-4 (edit the trunk, keep Windows byte-for-byte)
  while keeping the branch surface small and reviewable. Windows-only subsystems (profile/registry/Win32
  teardown) get a single early `if _IS_LINUX: return`/skip guard, since Linux has no analog.

## NOTE 001-4 | 2026-07-21 | PIVOT — extend the Windows trunk, do not build a new file (CONFIRMED)
- Status: RESOLVED (user-confirmed 2026-07-21, this session)
- ADR: none (self-contained)
- Sections: ALL (re-frames the whole plan)
- Context: The prior architecture built a clean-room `deploy_brain.py` with a `PlatformBackend` ABC that
  re-implemented the deploy lifecycle (Sections 1+6 landed as `a11e713`, Section 2 as origin `13e8467`).
  The user rejected this end-of-last-session: the working `windows_deploy_brain.py` should be the trunk,
  Linux parity folded INTO it, not re-implemented alongside it.
- Decision/Update: (1) Base everything on `windows_deploy_brain.py`; branch inline only at the five
  OS-forced touchpoints; the Windows path stays byte-for-byte what works. (2) **Rename** the consolidated
  trunk to `deploy_brain.py` at Section 7 (`git mv`), after validation. (3) **Discard** the rejected
  clean-room `deploy_brain.py` (its cert rc-guard idea is carried by Section 6). (4) Retire
  `linux_deploy_brain.py` once its Linux realizations are folded in. Origin was reverted to baseline
  `30abc35` (force-push) and the stray `13e8467` dropped. All plan docs re-scoped to this framing.
