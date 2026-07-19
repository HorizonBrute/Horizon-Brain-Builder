# Skills Index

**When adding a skill to skills_bin: add an entry here in the same commit.**

Two zones, split by who may write:

- `skills/brain_ro/` — read-only to the brain user. The admin/owner writes here.
- `skills/brain_rw/` — the brain user's own space. The brain user may write here.

This file is the rollup of both zones. The per-zone indexes are authoritative for their own zone.

## brain_ro — admin/owner writes

| Skill | Trigger | Model group | Purpose |
|---|---|---|---|
| `brain_skill_builder` | `/brain_skill_builder`, "create a skill", "add a skill", "scaffold a skill" | `#midcost` | Scaffold a new skill into the correct zone and register it in the indexes. |

## brain_rw — brain user writes

| Skill | Trigger | Model group | Purpose |
|---|---|---|---|
