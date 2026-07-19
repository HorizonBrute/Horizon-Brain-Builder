# ollama/ — Ollama server configuration

The sealed model server's tuning knobs. Ollama has **no** config file — its `OLLAMA_*`
environment variables *are* its config, so this is a **passthrough** env file: any `OLLAMA_*`
variable you add is handed to the server.

## Files

- **`ollama.env`** — active knobs (keep-alive, parallelism, max loaded models, queue depth)
  plus a documented set of further knobs commented to their defaults.
- **`models/`** — where pulled model blobs are kept for the sealed server (e.g. the default
  embed model `nomic-embed-text`).

> Do **not** set `OLLAMA_HOST` here — it stays the in-container default `0.0.0.0:11434`, safe
> because the container is sealed on `brain_net` with no host port. Publishing is the gateway's
> job (`OLLAMA_EXPOSE` in `../brain.env`), never a raw ollama bind.

Edit here, then `system/brain_sbin/reapply_brain_configs.py --services ollama` (a change needs a recreate).
