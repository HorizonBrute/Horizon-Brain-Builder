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
**Status:** NOT STARTED · **Depends:** 1, 3

## Section 3 — Linux engine artifact
**Status:** NOT STARTED · **Resolved:** engine = `docker save` images + ollama-volume tar + config/cert bundle (NOTE 001-1, confirmed 2026-07-21)

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
