# [BRAIN_NAME] Brain Invariants

Hard rules for every agent operating in this brain. These files load into every
context — keep them terse. Add brain-specific invariants below the defaults.

> Canonical policy a new brain inherits at provision. #1–#3 are the platform defaults;
> #4–#7 are the brain-runtime security/reliability invariants. Full model + rationale:
> `system/brain_bin/brain_security_model.md`.

1. **Security invariants**: Never violate @system/brain_bin/brain_security_model.md.
2. **Stay in scope**: Operate only within this brain's working directories and the
   knowledge locations granted in `brain_core.md`. Do not seek access or data outside them.
3. **Context hygiene**: Files that load into every context (`agents.md`, `brain_core.md`,
   `local.agent_teams.md`) must stay terse. Do not add primitives or flags without a clear
   primitives-first justification.
4. **Brain owns its engine, services & data (SECURITY)**: Rootless Docker in a per-user
   WSL2 distro registered under the brain's Windows account; daemon/containers run as the
   brain uid. Chroma + data live in-distro at `~/knowledge/brain_rw/chroma` — run the stack
   only as the brain, never as root/owner, never via a `/mnt/c` bind. `knowledge/` is the
   data-in seam: `brain_ro/` (read-only source + `inbox/`) vs `brain_rw/` (service-written);
   the inbox fence is `knowledge_lock.py` (default LOCKED).
5. **Resilient — auto-update + backups (RELIABILITY)**: Software stays current
   automatically; each bump snapshots + health-checks + auto-rolls-back, daily snapshots
   rotate in `~/backups` (logged to `~/logs/brain-maintenance.jsonl`). Do not disable them.
   The distro VHDX lives in the brain folder — backing up that folder captures engine + data.
6. **Least-privilege sandboxed runtime (SECURITY)**: The brain uid is non-privileged and
   cannot escalate; its only write surface is the scoped service API + its own scratch —
   never its code, policy, engine, or host. Prompt injection is assumed; the defense is
   incapability, not trust. Neuron containers run read-only-rootfs + tmpfs.
7. **Immutable code + policy via seams (SECURITY)**: The brain reads/executes its code
   (`common_neuron_platform/{input,action}` → `/opt/{input,action}_neurons`) and reads its
   policy (this file, `brain_core.md`, `agents.md`, `CLAUDE.md`) but writes neither — the
   writer identity is never the runner. Updates arrive only through the code-in/policy-in
   read-only-mount seams. Posture `personal` locks code+policy RO; `server` adds an egress
   allowlist + `/mnt/c` disable (`provision/stage7_harden.sh`).

<!-- Add brain-specific invariants below (e.g. versioning discipline, branch protection,
     branding rules, hashing/integrity requirements). -->
