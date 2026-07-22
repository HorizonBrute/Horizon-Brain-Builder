---
type: project_plan
title: "Project 001 — Unify the brain deployer (Current Orientation)"
description: The read-instead-of-a-handoff cold-start orientation for Project 001.
tags: [project-plan, orientation, deployer]
timestamp: 2026-07-21
status: draft
---

# Project 001 — Current Orientation

> Read this instead of a handoff to resume Project 001 cold. Short by design.

## What this project is
Replace the two platform brain deployers (`windows_deploy_brain.py`, `linux_deploy_brain.py`) with a
single `deploy_brain.py`. One shared build→deploy→verify process; a thin `PlatformBackend` handles
only the OS-forced steps. Full detail: `001_detail-unify_deploy_brain.md`.

## The one thing to understand first
The Windows deployer already runs the correct process end-to-end; the Linux one diverged and broke
(gateway TLS cert never generated — a false-green from a bad `gen-cert.sh` call). So unification is
**subtraction toward the Windows process**, not a merge of two half-different flows. Only five things
are genuinely OS-forced (engine host+snapshot, identity switch, seam mount, residency, firewall);
everything else is shared. See NOTE 001-1 for the one real open design question.

## Where the project stands
**Sections 1 & 6 have landed** in `deploy_brain.py`: the `PlatformBackend` interface + Linux/Windows
backends foundation (identity switch, account probes, naming, OS dispatch; engine/seam/residency/
firewall are section-tagged stubs), and the shared TLS-cert stage done right (no-arg gen-cert, typed
SAN only for server, the posture word rejected fatally, hard rc + cert-existence check). The
`selftest` verb proves the cert contract green — **BUG-001-1 is closed at the contract level**.
All design questions are resolved: the Linux engine artifact is `docker save` images + ollama-volume
tar + config/cert bundle (NOTE 001-1, confirmed). Nothing is BLOCKED.
Remaining code: Section 2 (shared build-engine), 3 (Linux snapshot/restore), 4 (shared deploy:
create-account/seam/gateway/models/neurons/verify + residency/firewall), 5 (full CLI), 7 (retire the
two old drivers + docs), 8 (rebuild dev_brain via the unified path — clears the live outage, NOTE 001-3).
Recommended next: Section 2 → 3 → 4. Live `dev_brain` stays down by design until Section 8.

## What to read, in order
1. This file.
2. `001_status-unify_deploy_brain.md` — per-item status + `NOTE 001-K` decisions.
3. `001_detail-unify_deploy_brain.md` — the full plan, verbatim brief, traced code map.
4. `001_bugs_and_technical_debt-unify_deploy_brain.md` — BUG-001-1 (the cert false-green) + debt.

## Standing rules for keeping THIS project current (do these without being asked)
1. **Keep the status doc live** — update each item's status as work lands; add a `NOTE 001-K` for
   every decision (`grep "NOTE 001-"`).
2. **Archive verified sections** — move `VERIFIED` blocks into
   `001_status_archive-unify_deploy_brain.md`; leave a one-line stub.
3. **Keep this orientation current** — a couple of plain lines when the shape or next step changes.
4. **Log every meaningful action** — append ONE line to `001_action_log-unify_deploy_brain.md`.

The full lifecycle rules live in `PROJECT_PLAN_GUIDE.md` (same folder).

## Related tracking
- Bugs & tech debt: `001_bugs_and_technical_debt-unify_deploy_brain.md`.
- Sibling tool already cross-platform: `brain_doctor.py` (diagnose/repair) — consumes whatever this
  deployer produces; keep its probes in sync (DEBT-001-1).
