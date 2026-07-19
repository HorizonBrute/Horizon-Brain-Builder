---
name: brain_skill_builder
description: Build a new skill and place it in the correct permission zone (brain_ro or brain_rw), then register it in the zone index and the rollup index. Use when the user types /brain_skill_builder, asks to "create a skill", "add a skill", "scaffold a skill", "make a new skill", or "write a skill for X".
---

# Skill: /brain_skill_builder

**Model preference:** `#midcost` (generic group name; the group set is configurable — map it to whatever groups this deployment defines, and override with a prompt directive).

Scaffold a new skill, place it in the zone the caller is allowed to write, and register it.

---

## Zones

| Zone | Writable by | Contents |
|---|---|---|
| `skills/brain_ro/` | admin/owner only | skills shipped to the brain; read-only to the brain user |
| `skills/brain_rw/` | brain user | skills the brain user authors for itself |

Permission rule:

- **admin/owner** — may create in EITHER `brain_ro` or `brain_rw`.
- **brain user** — may create ONLY in `brain_rw`. Never `brain_ro`.

---

## Arguments

`/brain_skill_builder [skill_name] [--zone brain_ro|brain_rw]`

Both optional; ask for whatever is missing.

---

## Step-by-step execution

### Step 1 — Determine who is asking

Establish the caller's role: **admin/owner** or **brain user**. If it is not already unambiguous from session context, ask outright. Do not assume admin.

### Step 2 — Resolve the target zone

- Brain user → force `brain_rw`.
- Admin/owner → use `--zone` if given, else ask which zone.

**Refuse** if the brain user targets `brain_ro`. Stop and report verbatim:

```
Refused: brain_ro is read-only to the brain user. Only the admin/owner may create
skills there. Re-run targeting brain_rw, or ask the admin/owner to create it in brain_ro.
```

Do not scaffold, do not partially write, do not offer a workaround.

### Step 3 — Validate the name

- lowercase, `[a-z0-9_]` only.
- Must not already exist in EITHER zone — check both `skills/brain_ro/<name>/` and `skills/brain_rw/<name>/`. On collision, stop and report which zone holds it.

### Step 4 — Scaffold

Create `skills/<zone>/<skill_name>/SKILL.md` with YAML frontmatter:

```
---
name: <skill_name>
description: <what it does>. Use when the user types /<skill_name>, <trigger phrase>, <trigger phrase>.
---
```

Then the body:

- `# Skill: /<skill_name>`
- a `**Model preference:**` line naming a group
- one-line purpose
- `## Arguments` (omit if none)
- `## Step-by-step execution` — numbered, imperative steps
- `## Output` — what the caller gets back
- `## Failure modes` — what to do when a step fails

Keep it terse and implementer-focused. No rationale essays.

### Step 5 — Register it (REQUIRED — same commit)

The index header states the rule; this skill **enforces** it. A skill that is scaffolded but not registered is an incomplete run.

5.1 Append a row to `skills/<zone>/index.md`, matching the existing table shape exactly:

```
| Skill | Trigger | Model group | Purpose |
|---|---|---|---|
```

5.2 Refresh the rollup `skills/index.md` — add the same row under that zone's section.

5.3 Both index edits and the new `SKILL.md` go in **one commit**. Never commit the skill without its index rows.

### Step 6 — Verify

Confirm before reporting success:

- `skills/<zone>/<skill_name>/SKILL.md` exists and its frontmatter parses.
- `skills/<zone>/index.md` contains a row for `<skill_name>`.
- `skills/index.md` contains a row for `<skill_name>` under the correct zone section.

If any check fails, fix it — do not report success.

---

## Output

Report: the zone chosen, the path created, and the two index files updated.

---

## Failure modes

| Condition | Action |
|---|---|
| Brain user targets `brain_ro` | Refuse per Step 2. No files written. |
| Caller role unclear | Ask. Do not assume admin/owner. |
| Name collides in either zone | Stop; report the owning zone. |
| Index row missing at Step 6 | Add it before reporting success. |
