# brain_workshop Brain Invariants
1. **Security invariants**: Never violate @system/brain_bin/brain_security_model.md.
2. **Stay in scope**: Operate only within this brain's working directories and the
   knowledge locations granted in `brain_core.md`. Do not seek access or data outside them.
3. **Context hygiene**: Only modify agents.md, claude.md should only point to agents.md. Files that load into every context (`agents.md`, `brain_core.md`, `local.agents.md`) must stay terse. Do not add primitives or flags without a clear
   primitives-first justification.

<!-- TODO: Add brain-specific invariants (e.g. versioning discipline, branch protection,
     branding rules, hashing/integrity requirements). Delete this comment when done. -->
