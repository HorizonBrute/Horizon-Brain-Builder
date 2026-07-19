# common_neuron_platform/action/ — the READ side of a neuron bundle

> This is the **shared platform image source** for every action neuron (built ONCE per role, not
> per neuron). It lives at `system/common_neuron_platform/action/`; the in-container mount target and
> docker build context stay `/opt/action_neurons` and the image tag stays `${BRAIN_NAME}-action_neurons`
> (contract-preserving — only the host source dir moved out of the brain root).

An **action neuron** READS a bundle's Chroma collection to answer queries: retrieve the
nearest chunks (embed the question with the SAME model the docs used — nomic-embed-text —
then a Chroma k-NN query) and synthesize a grounded answer with a small Ollama LLM
(llama3.2:1b). Like the input side it reaches chroma/ollama ONLY through the gateway
(ADR-0015), carrying a scoped `chroma:reader` + `ollama:use` token — so it physically cannot
write (the write-funnel invariant, enforced by the gateway).

## Contents
```
system/common_neuron_platform/action/
  Dockerfile            # builds ${BRAIN_NAME}-action_neurons (deps at build; NO internet at run)
  requirements.txt      # requests only (the query API uses the stdlib http.server)
  action.py             # entrypoint — --query "..." (one-shot) | --serve (query API)
  action_common.py      # env Config + gateway'd Chroma-v2-REST (query/get) + Ollama (embed/generate)
  retrieve.py           # the RAG core: embed -> chroma query -> synthesize (grounded, cited)
  serve.py              # the long-running query API (GET /health, GET|POST /ask)
```

## How it is built + run
- `neurons_mount.py install` → RO mount at `/opt/action_neurons`; `docker compose --profile
  neurons build` → the `${BRAIN_NAME}-action_neurons` image.
  A bundle may ship BOTH action shapes off this one image (the example bundle does):
- **One-shot (on-demand CLI):** the `action_neuron_cli` service runs
  `--query "how do I ...?" [--k 5] [--json]` and prints the answer (+ sources on stderr); it does
  not publish a port. Invoke via `docker compose --profile neurons run --rm --no-deps action_neuron_cli
  --query "..."`.
- **Query API (daemon):** the `action_neuron_api` service runs `--serve --host 0.0.0.0 --port 8080`.
  Publish it off-box with `publish_to_lan_ports: [8443]` in the neuron's zone block +
  `compose.action-neuron-gateway.yaml`; the gateway's :8443 path-router forwards
  `/{bundle}/{neuron}/ask` → `<neuron>:8080/ask` (prefix stripped), so the app serves plain root
  paths. POC: the :8443 surface is unauthenticated (inspected, reader-token-only) — securing it is
  a tracked follow-up.

Same read-only-rootfs posture and offline-at-runtime constraint as `common_neuron_platform/input/`.
