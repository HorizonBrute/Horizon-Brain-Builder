# common_neuron_platform/input/ — the WRITE side of a neuron bundle

> This is the **shared platform image source** for every input neuron (built ONCE per role, not
> per neuron). It lives at `system/common_neuron_platform/input/`; the in-container mount target and
> docker build context stay `/opt/input_neurons` and the image tag stays `${BRAIN_NAME}-input_neurons`
> (contract-preserving — only the host source dir moved out of the brain root).

An **input neuron** turns a data source (files under `knowledge/brain_ro/`, a git repo, images)
into vectors in a Chroma collection. It reaches the backends ONLY through the gateway
(ADR-0015), carrying a scoped `chroma:writer` + `ollama:use` bearer and stamping
`X-Neuron-Bundle` / `-Role` / `-Name` on every hop (content-capture attribution).

## Contents
```
system/common_neuron_platform/input/
  Dockerfile            # builds ${BRAIN_NAME}-input_neurons (deps at build; NO internet at run)
  requirements.txt      # requests + PyYAML only
  neuron.py             # entrypoint — phase flags (--ingest-only / --deliver-only / --tags / --source)
  ingest_common.py      # env Config, gateway'd Chroma-v2-REST + Ollama clients, offline chunker, dedup state
  manifest.py           # runtime sources.yaml parser + bundle/tag resolution; scripted sources under IMPULSES_ROOT/<bundle>/<neuron>/
  ingest_docs.py        # the write pipeline: docs -> chunks -> embed -> upsert (+ dedup, prune)
  ingest_images.py      # image side (delegates to image_embed; captions ride the text path)
  image_embed.py        # image->text strategy: caption (Ollama vision) | clip (stub)
  delivery/
    neuron_deliver.sh   # git-delivery WRITE phase wrapper (injects the transient github token)
```

## How it is built + run
1. `system/brain_sbin/neurons_mount.py install` mounts this dir READ-ONLY into the distro at
   `/opt/input_neurons` (the code-in seam — the brain executes but cannot write it).
2. `docker compose --profile neurons build` builds `${BRAIN_NAME}-input_neurons` from
   `context: /opt/input_neurons`.
3. **Ingest (default):** `docker compose --profile neurons run --rm --no-deps input_neuron_example
   --ingest-only` reads the bundle's delivered sources read-only and writes vectors. Dedup
   (content-hash in `/state`) re-embeds only changed docs; removed docs are pruned. (The compose
   service is named for the neuron — `input_neuron_example` in the shipped example bundle.)
4. **Deliver (git sources):** `... run --rm --no-deps -e GITHUB_TOKEN=<transient> input_neuron_example
   /app/delivery/neuron_deliver.sh` clones/refreshes git sources into `brain_ro`, then stops.

## Design notes
- **Raw REST, not the chromadb/LlamaIndex clients** — a custom bearer routed through the
  neuron_net gateway alias is cleaner over the Chroma v2 REST surface (proven by
  `knowledge/testupload_through_gateway.py`) and keeps the image tiny + fully offline.
- **Offline at runtime** — the neuron sits on `neuron_net` only (no internet). Embeddings and
  captions come from Ollama through the gateway; chunking is pure-python (no tokenizer download).
- **Read-only-rootfs safe** — `PYTHONDONTWRITEBYTECODE` + pycache→`/tmp`; only `/state`
  (named volume) and `/tmp` (tmpfs) are written.

The source manifest and bundle registry are **no longer hand-authored** as `brain_etc/neuron/
{sources,bundles}.yaml` — they are RENDERED from the `===NEURONS===` zone of `brain_etc/brain.env`.
The input container still reads a runtime `sources.yaml` at `/etc/neuron/sources.yaml`, but that file
is now generated from the zone (`brain_env.render_sources_yaml`) — program and config stay apart.
