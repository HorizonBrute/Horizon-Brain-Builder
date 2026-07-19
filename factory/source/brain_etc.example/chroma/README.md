# chroma/ — Chroma server configuration

Chroma's own server tuning knobs, `env_file`'d into the sealed `chroma` container at start
(a change needs a container recreate).

## Files

- **`chroma.env`** — a **passthrough** env file: any Chroma server environment variable you add
  here is handed to the container, so the whole Chroma config is yours (telemetry off by default,
  persistence, CORS, limits, OpenTelemetry — documented inline, mostly commented to defaults).

## What does NOT live here (by design)

- **`CHROMA_TOKEN`** and the auth provider — stack plumbing, injected by Compose from
  `../brain.env`. Do not set authn here.
- The **published bind/port/TLS** — that is gateway posture (`../brain.env`), because Chroma
  itself is sealed on `brain_net` with no host port; only the gateway is published.

The **data** (the vector DB) is not here — it lives on the distro's fast disk at
`~/knowledge/brain_rw/chroma`. Edit here, then `system/brain_sbin/reapply_brain_configs.py --services chroma`.
