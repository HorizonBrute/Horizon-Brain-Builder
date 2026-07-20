# Operational docs — `system/brain_bin/`

Operator/runtime documentation for a **deployed brain**. These files ship inside every brain
(staged from `factory/source/`), and both the deploy code and `brain_invariants.md` resolve them
at this path — so this is their authoritative home, not a copy.

## Guides
- [DEPLOYMENT.md](DEPLOYMENT.md) — deploy a brain's engine (import, residency, backup) and the end-to-end onboarding flow.
- [OPERATIONS.md](OPERATIONS.md) — day-2 operations: env-knob reference, port map, the config change-and-apply loop.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — symptom → diagnose → cause → fix for a live brain.
- [brain_security_model.md](brain_security_model.md) — the security & isolation model (the `brain_invariants.md` `@`-pointer target).

## Per-seam READMEs (co-located with the tooling they explain)
- [deploy/README.md](deploy/README.md) — the deploy seam: `brain_installer_1_admin.py` / `brain_installer_2_brain.py`, residency.
- [provision/README.md](provision/README.md) — the in-distro provisioning stage scripts.
- [gateway/README.md](gateway/README.md) — the nginx token-gateway seam.
- [chroma/README.md](chroma/README.md) — the Chroma vector-store seam.
