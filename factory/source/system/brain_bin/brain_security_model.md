# Brain Security Model — the Standard Shape

Authoritative design for how a Horizon.AIOS brain is hardened. A brain is a
**sandboxed, least-privilege agent runtime**: an autonomous thing that can do its
job and *nothing else*, assembled from architectures operators already know
(a least-privilege service account + a read-only application deploy + admin-owned
config + a sandboxed VM). This file is the north star the provisioning recipe
(`provision/`) and deploy path (`deploy/`, `onboard.py`) build toward.

---

## 1. Threat model (why this shape exists)

An LLM agent takes instructions from **untrusted content** — the RAG corpus, user
prompts, tool output. Prompt injection is not a bug to patch; it is a permanent
property of the medium. So we do not try to make the agent *trustworthy* — we make
it **incapable**, by giving its runtime identity a tiny blast radius. Concretely we
defend against: a compromised/injected agent (1) rewriting its own **code**,
(2) rewriting its own **policy/rules** (unclipping its own leash and persisting the
change), (3) reaching the **host** filesystem, (4) making **arbitrary network**
calls (exfiltration / SSRF), (5) **escalating** to root.

## 2. The one rule

**The identity that WRITES an artifact is never the identity that RUNS it.** The
running brain executes; a *separate* identity (a deployer, or the admin) writes.
Git/API/rsync is only the transport — swappable; the identity split is the invariant.

## 3. The agent's only write surface is the service API

The agent mutates knowledge by talking to the vector store through the stack's service
API (the input neuron writes to `chroma:8000` on `brain_net` with a scoped writer token;
consumers read through the TLS gateway) — it never needs to write a file to do its job. So
"cannot write its code / policy / host" costs nothing functionally. Containment and function
do not fight.

## 4. Tiered ownership (the whole model in one view)

| Layer | Content | Brain runtime access | Written by |
|---|---|---|---|
| **Policy / identity** | `brain_invariants.md`, `brain_core.md`, `agents.md`, `CLAUDE.md` | **read-only** | admin (policy-in seam) |
| **Code — input (write)** | `common_neuron_platform/input/` (runtime mount `/opt/input_neurons`) | read + **execute** | deployer (code-in seam) |
| **Code — action (read)** | `common_neuron_platform/action/` (runtime mount `/opt/action_neurons`) | read + **execute** | deployer (code-in seam) |
| **Engine / infra** | `brain_bin`, `wsl`, `docker`, `chroma` | none / read | admin |
| **Knowledge (data)** | the corpus | via **gateway/service API**, plus own scratch | brain (ingest) + owner (`knowledge/brain_ro/` read-only door) |

Two *kinds* of code-in seam (input + action neuron bundles, ADR-0017), one policy-in seam, one
data-in seam — everything else read / execute / nothing, all state-writes through a service.

**The two code seams differ by network posture, and that difference is the point:**

- **`common_neuron_platform/input/` (write side)** runs a container on **`brain_net`**, resolving
  `chroma:8000` / `ollama:11434` by service name and talking to them **directly** (with a
  scoped writer token) — it never transits the read-access gateway. It is the trusted writer
  that turns delivered source content into vectors.
- **`common_neuron_platform/action/` (read side)** answers queries (RAG retrieval + a model). Its read path is
  **mediated by the gateway path-router** (`/{bundle}/{neuron}/…` on `:8443`) and it holds only
  a **reader** token — so it physically cannot write to the store. Any write must go back through
  an input neuron (the write-funnel invariant, enforced by the gateway, not by convention).

Both are code the brain **executes but cannot write**: the code is bind-mounted read-only from
the `/opt/…` seam, so a code edit needs no image rebuild.

## 5. The seams (how in-only updates work without giving the brain write)

Same shape as the existing `knowledge/brain_ro/` **data-in** seam, generalized:

- **Code-in seams** → `/opt/input_neurons` and `/opt/action_neurons`, owned `root:root`
  (or a `deployer`), mode `0755`. Brain uid = read+execute, **no write**. At deploy these are
  **read-only host-dir mounts** of the brain's `common_neuron_platform/input/` / `common_neuron_platform/action/` dirs (laid
  by `system/brain_sbin/neurons_mount.py`); the neuron images are built from them and the code is
  bind-mounted `:ro`, so a code edit needs no rebuild. (A legacy root force-sync timer
  — `git reset --hard origin/BRANCH`, *main always wins* — is retained but **inert by default**
  (`TRANSPORT=none`), superseded by the host-dir mount.) The neurons run as **container bundles**
  (ADR-0017), not a `/opt/neurons` code sync — see the `===NEURONS===` zone of `brain_etc/brain.env`
  + `add_neuron_bundle.py`.
- **Policy-in seam** → the context files are delivered **into the distro read-only**
  the same way (not read across `/mnt/c`, which the `server` posture disables). The
  Windows-side brain folder holds the admin-editable *source*; the in-distro copy the
  agent reads is root-owned, brain read-only.
- **Data-in seam** → the owner drops source content into the read-only corpus door
  `knowledge/brain_ro/` (the old `knowledge/inbox/` drop-point was removed in the config-flow
  refactor); the vector store lives behind the `knowledge/brain_rw/chroma` door.

Editing happens in a human-facing **edit copy** (a plain Windows folder or a git
checkout, admin/human RW, familiar tools) → the seam syncs it into the read-only
runtime. That is just *deploy*, expressed locally. It echoes cleanly to Linux/Mac
(no WSL boundary — edit-copy and run-copy are two dirs, or one dir with perms).

## 6. Posture profiles (personal-easy, server-hard — one artifact, a dial)

Hardening is a **setting, not a rebuild**. Same brain, tighter screws. **Secure by
default: there is no lax posture** — the old `dev` (brain-writable code) is removed.

| Screw | `personal` | `server` |
|---|---|---|
| runtime brain uid non-sudo | ✅ | ✅ |
| `/opt/input_neurons` + `/opt/action_neurons` code | read+execute only | read+execute only |
| context files (policy) | **locked** read-only | **locked** read-only |
| neuron container rootfs | `read_only: true` + tmpfs (journal 029) | `read_only: true` + tmpfs |
| host-fs bridge (`/mnt/c` automount) | on | **disabled** |
| network egress | open | **allowlist** (RAG + model endpoints + upstream only) |

The brain never writes its own code/policy in **any** posture. Iterate on neurons
through the code-in seam (deployer edits the host `common_neuron_platform/input/` / `common_neuron_platform/action/` dir,
the RO mount reflects it), not by unlocking the runtime. Every neuron container also runs with
a **read-only root filesystem** + tmpfs scratch (journal 029), baked into the generator
templates — even a compromised neuron process cannot persist to its own image. (This dial also drives the gateway exposure — `personal` = loopback
gateway, `server` = host-NIC gateway + firewall; see ADR 0002.)

## 7. Enforcement map (where each screw actually lives)

- **In-distro (Linux perms / wsl.conf / nftables):** non-sudo runtime, `/opt/input_neurons`
  + `/opt/action_neurons` ownership + RO host-dir mounts (`neurons_mount.py`), the
  `read_only`-rootfs neuron containers, policy-in RO mount (`/opt/brain_truths`), `/mnt/c`
  automount disable, egress allowlist. Neuron bundles are rendered by
  `system/brain_sbin/add_neuron_bundle.py` from the `===NEURONS===` zone of `brain_etc/brain.env`. Implemented by
  `provision/stage7_harden.sh [posture]`; asserted by `verify_engine.sh` (reports the
  `/opt/input_neurons` + `/opt/action_neurons` posture).
- **Windows-side (ACLs, defense-in-depth) — WIRED:** the edit-copy sources
  (`common_neuron_platform/input/` + `common_neuron_platform/action/` dirs + the policy/identity files `CLAUDE.md`, `agents.md`, `brain_core.md`,
  `brain_invariants.md`, **plus `.claude/settings*.json`** — the leash) are ACL'd
  read-only to the **per-brain runtime group `<brain>_group`** so that in postures where
  `/mnt/c` is still on, no account that runs as the brain can edit the source it runs.
  Implemented in `deploy/brain_installer_1_admin.py` (`lock_edit_source(brain, posture)`),
  which applies an explicit **Deny** of the write/delete class — the house
  `NOWRITE_MASK = (WD,AD,WEA,WA,DE,DC)` (mirrors `horizon_system/sbin/horizon_aios_harden.py`) —
  on `<brain>_group`. We deny the **group**, not just the brain user, because `<brain>_group`
  is the set of accounts that may run *as* the brain (one brain can be run by several
  accounts) — every brain-runner is treated the same. Deny beats the Full the group inherits
  from the brain-folder root; owner + Administrators keep Full, so the deployer code-in sync
  (root/owner, never the brain) is unaffected. Read+execute stay intact (a runner can still
  *read* its policy). Applied in every posture to match `stage7_harden.sh` (`personal`/`server`);
  secure by default — no posture skips the lock. `common_neuron_platform/input/` / `common_neuron_platform/action/` are created
  if absent so the deny is inheritable — future code is born locked.
- **Writer≠runner membership rule (consequence of the group deny):** a Deny on `<brain>_group`
  beats the Allow of *any* member, including when elevated (the group SID rides in the token
  regardless of UAC). So the **human operator must not be a member of the runtime group** — else
  applying `personal`/`server` makes that human read-only on the edit-source too. This is rule
  #2 (the writer is never the runner) enforced literally. `horizon_aios_create_brain.py` was
  updated (2026-07-02) so new **Windows** brains no longer add the human invoker to `<brain>_group`;
  the human keeps write via the explicit invoking-user Full ACE `create-brain` already grants on the
  brain folder. (Unix still adds the human — there the 770 group membership *is* the human's write
  path and there's no Unix edit-source lock yet; switch to an explicit `setfacl u:<user>:rwX` grant
  when one lands.) **Brains created before this fix** still have the human in the group — remove them
  (`Remove-LocalGroupMember -Group "<brain>_group" -Member "<domain>\<human>"`) before applying the
  lock.

## 8. Invariants this encodes

Brain invariants **#6** (least-privilege sandboxed runtime) and **#7** (immutable code
+ policy via seams). The shippable 7-invariant policy template a new brain inherits is
`factory/policy_templates/brain_invariants.md` (with #4 brain-owns-engine/services/data,
chroma at `~/knowledge/brain_rw/chroma`; #5 auto-update+backups; #6/#7 above).

**Seeding (both deploy paths).** All four brain-root context/policy files — `CLAUDE.md`,
`agents.md`, `brain_core.md`, `brain_invariants.md` — are materialized into the brain root
BEFORE installer_1's ACL lock, so the lock lands on real files. `brain_invariants.md` comes
from this rich `policy_templates/` policy; the other three from the shared `.aioscommon/`
templates. The deployers (`{windows,linux}_deploy_brain.py`) collect all four into the staged
`policy_templates/` and `seed_brain_context_files()` writes them (`[BRAIN_NAME]` substituted,
only-if-absent), then removes the staging dir; aios-mode `create_brain.py` seeds the same set
directly. (Historically PATH 2 seeded none of these — deployed brains shipped policy-less;
fixed 2026-07-14.)

## 9. Applying to an already-built brain

The current brain was built before this model. To bring it to `personal`/`server`:
run `stage7_harden.sh <posture>` in the distro (root), (re)establish the code-in mounts with
`system/brain_sbin/neurons_mount.py install`, and re-run the admin ACL step —
`python deploy/brain_installer_1_admin.py --posture <posture> --no-launch` applies the
Windows-side edit-source lock without re-running the phase-2 engine deploy. Non-destructive;
idempotent (re-adding a Deny ACE is a no-op).
