# chroma/ — SUPERSEDED (Docker-Desktop era)

> **DO NOT USE. This directory documents the abandoned Docker-Desktop model** (data bind-mounted
> to a Windows NTFS dir, a plaintext `http://…:8000` heartbeat, `docker compose up` on Docker
> Desktop, image pinned to `chromadb/chroma:1.5.0`). Every one of those commands **contradicts the
> current sealed-gateway model** and will mislead — they are intentionally removed from this file.

The live model:

- Chroma runs **sealed** inside the WSL2 distro on **rootless Docker** at `~/docker/`, with **no
  host port** — it is reachable only through the nginx TLS gateway (token-required; a plaintext
  `:8000` heartbeat returns `403`, not a `200`). Data lives at **`~/knowledge/brain_rw/chroma`**
  (the `brain_rw` zone of the `knowledge/` data-in seam), **not** a Windows bind mount.
- Server tuning knobs are the config seam `brain_etc/chroma/chroma.env`
  (see `../../brain_etc.example/chroma/README.md`).

For standing the brain up and operating the stack, see **`../DEPLOYMENT.md`** (engine + residency)
and **`../OPERATIONS.md`** (the ADR-0015/0017 operator model). This file is retained only as a
historical marker.
