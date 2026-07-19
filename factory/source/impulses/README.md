# impulses/ — outbound query clients (TEMPLATE SCAFFOLD)

> **TODO (factory scaffold):** placeholder only. Ship your own impulse clients here.

`impulses/` holds **YOUR code**, in two shapes:
- **`<bundle>/<neuron>/`** — the per-neuron code you own: input provider scripts (scripted sources,
  mounted read-only into the input container at `IMPULSES_ROOT=/impulses`) AND action apps
  (`query.py`). The config-flow refactor added this per-bundle layout; the shipped
  `example_neuron_bundle/` demonstrates it (`input_neuron_example/`, `action_neuron_cli/`,
  `action_neuron_api/`). Brain-managed RUNTIME dirs live separately under `neurons/<bundle>/<neuron>/
  {input,action}/`.
- **`query_client/`** — an operator-seat client that queries the brain through the gateway (the
  read/ask side, as opposed to the in-container action neuron).

```
impulses/
  <bundle>/<neuron>/     # YOUR per-neuron code (input providers + action apps)
  query_client/          # a token'd HTTPS client that hits the gateway RAG routes
```

Nothing brain-private is packaged from the factory.

These are convenience clients, not part of the running stack — safe to add/remove without
touching the engine.
