<!-- BEGIN horizon-brain-builder (installed feature — managed block, do not edit by hand) -->
# Brain Builder  (options package: horizon_brain_builder)
Builds and deploys **brains** — per-brain sealed RAG runtimes: ChromaDB + Ollama, both sealed
(no host port, bearer-token only) behind an nginx token gateway, in a dedicated WSL2 distro
(Windows) or rootless Docker (Linux). Runs **in place** from its clone at `[[CLONE_PATH]]`;
nothing is copied into the AIOS.
Deploy: `python deploy_brain.py deploy --brain <name> --install-root <dir> --posture personal|server`
— one cross-platform orchestrator (elevated shell on Windows, `sudo` on Linux). Also `build-engine`,
`verify`, `status`, `teardown [--purge --yes]`; day-2 via `brain_doctor.py {diagnose|repair}`.
Runs fully standalone without the AIOS: pass `--install-root <dir>` or set `$AIOS_INSTALL_ROOT`.
Docs: the clone's `README.md` + `docs/`; per-brain ops docs ship at
`<install-root>/brains/<brain>/system/brain_bin/{DEPLOYMENT,OPERATIONS,TROUBLESHOOTING,brain_security_model}.md`.
<!-- END horizon-brain-builder -->
