# `system/brain_logs/` — the host-side logs door

This directory is the **read-only host "logs door"** for the brain's centralized
log seam (ADR-0018). It is the on-host, human-browsable window into the log root
that the running WSL distro writes at `/home/<brain>/logs/` in the deployed
runtime.

- **Source tree vs runtime.** This dir lives in the git **source tree** under
  `<brain>/system/`. The actual log files are **runtime artifacts** produced
  inside the distro (`/home/<brain>/logs/<source>-<YYYYMMDD>-<NNN>.log`, sources
  `gateway`/`wsl`/`chroma`/`ollama`) and are surfaced here read-only via the logs
  door mount. Log content is never committed (see `.gitignore`: `*.log`).
- **Posture.** RX-only for humans (mirrors `brain_truths.cmd_door`); white team is
  the primary consumer (operational/observability), blue team secondary
  (defensive).
- **Wiring.** Installed/rotated by the log-seam tooling on a systemd `--user`
  timer, re-applied from residency. See ADR-0018 and
  `projects/2ndbraindevelopment/decisions/0018-centralized-log-seam.md`.

Only this README (and a `.gitkeep`) are tracked here; everything else is runtime.
