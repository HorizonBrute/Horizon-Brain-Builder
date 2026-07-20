<!-- BEGIN horizon-brain-factory (installed feature — managed block, do not edit by hand) -->
# Brain Factory
Build and deploy **brains** (per-brain sealed RAG runtimes: ChromaDB + Ollama behind an nginx token
gateway, in a WSL2 distro on Windows / rootless Docker on Linux). The factory is a CLI toolchain that
runs **in place** from its clone at `[[CLONE_PATH]]` — nothing is copied into the AIOS. Deploy a brain
with that clone's `windows_deploy_brain.py` (Windows) or `linux_deploy_brain.py` (Linux); see the
clone's `README.md` + `docs/`. It also runs fully standalone (no AIOS): pass `--install-root <dir>` or
set `$AIOS_INSTALL_ROOT`.
<!-- END horizon-brain-factory -->
