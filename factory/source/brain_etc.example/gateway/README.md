# gateway/ — nginx TLS gateway configuration

The one published surface. The gateway terminates TLS, enforces token-role authz (readers read,
writers write), runs the `:8443` **path-router** to the action neurons, suppresses server
identity, rate-limits, and **logs the content** of all traffic (blue-team inspection). It bridges
both `brain_net` (the sealed chroma/ollama interior) and `neuron_net`.

## Human-edited seams (source of truth)

- **`gateway.conf`** — gateway **tuning**: rate limits + fail2ban policy (posture — what is
  published, TLS, authz — is in `../brain.env`, not here).
- **`token_registry`** — the bearer-token registry. `system/brain_sbin/gateway_tokens.py` mints/rotates
  from it and renders the token maps. **Never** print a token value into a doc/log.
- **`route_registry`** — the path-router target list. Under `ACTION_ROUTE_ALLOW=any` (default)
  every `/{bundle}/{neuron}/` target routes and this file is only needed to pin a non-default
  internal serve port (default 8080). Under `ACTION_ROUTE_ALLOW=registry` it is a default-deny
  allow-list: only the `{bundle}/{neuron}` targets listed here route.

## Machine-rendered (do NOT hand-edit)

- **`nginx_auto_gen/`** — `nginx.conf.template`, `ratelimit.conf`, `chroma.conf`, `ollama.conf`,
  `action.conf`, `internal.conf`, `njs/inspect.js` — emitted by `system/brain_sbin/gateway_config.py`
  from `../brain.env` + `gateway.conf`. `action.conf` carries the `action_backend` upstream + the
  TLS `:8443` server that path-routes to the action neurons; `internal.conf` is the neuron-net
  internal listeners. (`inspect.js` is the njs response-body capture used by `request+response`.)
- **`token_maps_auto_gen/`** — `reader_tokens.map`, `writer_tokens.map`, `ollama_use.map`,
  `ollama_admin.map`, `action_tokens.map` — rendered from `token_registry` by `gateway_tokens.py`.
- **`fail2ban_autoconfigs/`** — jail/filter/action configs rendered from `gateway.conf`.

Edit a seam, then `system/brain_sbin/reapply_brain_configs.py` regenerates + syncs + recreates the
gateway. These land in the running stack at `~/docker/nginx/`.
