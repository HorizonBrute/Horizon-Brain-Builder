# docker/ — the Compose stack

The Compose files that define the whole stack — the sealed `chroma` + `ollama` backends,
the nginx TLS `gateway`, the `fail2ban` sidecar, and the neuron bundle services — and how
they network across `brain_net` (sealed interior) + `neuron_net` (isolated neuron side).

## Files

- **`compose.yaml`** — the base stack: services, networks, volumes, the neuron bundle
  service blocks (a managed region rendered from the `===NEURONS===` zone of `../brain.env` —
  the default bundle by `neuron_compose.py`, additional bundles by `add_neuron_bundle.py`).
  The base file publishes **no host ports** — exposure is layered by the overlays below.
- **`compose.chroma-gateway.yaml`** — publishes the chroma `:8000` external listener (when `CHROMA_EXPOSE=on`).
- **`compose.ollama-gateway.yaml`** — publishes the ollama `:11434` external listener (when `OLLAMA_EXPOSE=on`).
- **`compose.action-neuron-gateway.yaml`** — publishes the **action query API** `:8443` path-router
  listener + mounts the rendered `action.conf` (when `ACTION_EXPOSE=on`).

> Each service's exposure is a **separate overlay** on purpose: a bare `compose … up`/`run`
> against the base file alone drops the published ports. Always layer the overlays (that is
> what `reapply_brain_configs.py` and the residency keepalive do).

These are copied into the running stack at `~/docker/` when config is applied. Edit here
(the source of truth); the copy in the distro is overwritten on the next apply / boot.
