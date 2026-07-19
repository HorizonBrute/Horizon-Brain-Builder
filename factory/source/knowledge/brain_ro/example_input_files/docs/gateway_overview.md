# The gateway is the brain's single front door

Sample doc 2 of 3 for the `example_neuron_bundle` smoke test.

Every neuron reaches its backends (Chroma, the vector store; Ollama, the model server)
ONLY through the **gateway**. The gateway is the one service a neuron cannot function
without — it is the monitoring chokepoint for all neuron traffic.

## Why route everything through the gateway

- **Inspection:** all traffic is logged, so every read and write is attributable to a
  bundle, role, and neuron.
- **Token scoping:** an input neuron carries a WRITE token; an action neuron carries a
  READ-ONLY token. The action side physically cannot write to the vector store.
- **Flexibility:** a neuron may point at internal OR external Chroma/Ollama endpoints and
  its traffic still flows through the gateway, so it is always monitored.

Chroma and Ollama are optional, replaceable backends. The gateway is not.
