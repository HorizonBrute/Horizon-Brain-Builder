<!-- BEGIN horizon-brain-builder (installed feature — managed block, do not edit by hand) -->
# Brain Builder
Build and deploy **brains** (per-brain sealed RAG runtimes: ChromaDB + Ollama behind an nginx token
gateway, in a WSL2 distro on Windows / rootless Docker on Linux). The builder is a CLI toolchain that
runs **in place** from its clone at `[[CLONE_PATH]]` — nothing is copied into Horizon.AIOS. Deploy a brain
with that clone's `windows_deploy_brain.py` (Windows) or `linux_deploy_brain.py` (Linux); see the
clone's `README.md` + `docs/`. It also runs fully standalone (no Horizon.AIOS): pass `--install-root <dir>` or
set `$AIOS_INSTALL_ROOT`.
<!-- END horizon-brain-builder -->
