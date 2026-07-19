# brain_etc.example — the config-exposure seam TEMPLATE

This is the **template** for a brain's `/etc`: every configuration surface its stack
uses, exposed on the host so an admin never has to reach into the distro to find it.
At deploy the orchestrator **seeds `brain_etc/` from this folder** (see
`system/brain_bin/deploy/README.md`), then it becomes the live config seam.

- You (admin) may **read and write** the seeded `brain_etc/` files.
- The brain sees them **read-only** at `/opt/brain_truths` inside the distro (mounted RO)
  and cannot modify them — this is the policy/config-in seam of the security model.
- After you edit, run **`system/brain_sbin/reapply_brain_configs.py`** (the apply primitive:
  regenerate → sync into the running stack → force-recreate the affected service). It
  lays your change in **first**, then recreates — so an edit can never be half-applied.
  `--services <svc>` scopes the recreate; `--no-pull` skips image pulls.

## Placeholder convention

The template ships **brain-neutral**; a brain's name is filled in at provision time:

- **`__BRAIN_NAME__`** — a literal token the provisioning step (`brain_truths.py provision`)
  text-substitutes with this brain's name when it seeds `brain_etc/` (e.g. in `brain.env`'s
  `BRAIN_NAME`/`COMPOSE_PROJECT_NAME`).
  Neuron **service names** are NO LONGER `__BRAIN_NAME__`-derived — the default bundle's compose
  blocks are generated from the `brain.env` neuron zone (neuron_compose.py), so service names come
  from the neuron object (e.g. `input_neuron_example`, `action_neuron_api`).
- **`${BRAIN_NAME}`** — a Compose/shell variable resolved at container start from
  `brain.env`'s `BRAIN_NAME` (used in `brain.env` / the compose files). Not text-substituted;
  it stays a variable.

## Folders (each has its own README)

- **`brain.env`** — the master **stack posture**: what runs, what is published through the
  gateway, on which bind/port, authz modes, the shared `CHROMA_TOKEN`, network segmentation,
  the `ACTION_*` / `ACTION_ROUTE_ALLOW` path-router knobs, and the inspection ladders.
- **`docker/`** — the Compose stack (base `compose.yaml` + the per-surface exposure overlays);
  also holds the machine-rendered `docker/neuron/sources.yaml` (generated from the `brain.env`
  neuron zone — not hand-authored).
- **Ingest config** now lives in the **`===NEURONS===` zone of `brain.env`** (sources, bundles, and
  neurons). The old hand-authored `neuron/{sources,bundles}.yaml` files are retired — the runtime
  `sources.yaml` is rendered from the zone by `brain_env.render_sources_yaml`.
- **`gateway/`** — nginx TLS gateway: tuning, token registry, the `route_registry`, generated nginx/token maps.
- **`chroma/`** — Chroma server env knobs.
- **`ollama/`** — Ollama server env knobs.
- **`tls/`** — the gateway TLS cert + key (the only PKI in the stack).
- **`github/`** — non-secret GitHub access posture for the delivery adapters (never a credential).
- **`wsl/`** — the apply manifest that drives config sync (machine plumbing).

> **`*_auto_gen/` is machine-rendered — do not hand-edit.** `gateway/nginx_auto_gen/` and
> `gateway/token_maps_auto_gen/` are emitted by `system/brain_sbin/gateway_config.py` /
> `gateway_tokens.py` from the human-named seams (`brain.env`, `gateway.conf`, `token_registry`).
> Edit the seam, then `reapply_brain_configs.py`.

> **Data lives elsewhere on purpose.** The vector store is a database; it stays on the distro's
> fast local disk at **`~/knowledge/brain_rw/chroma`** (the `brain_rw` zone of the `knowledge/`
> data-in seam), not on this config mount.
