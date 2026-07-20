# Docs index

Operational documentation for the brain factory. One line per document.

## Overview

- [README.md](README.md) — what the factory is: the canonical build/deploy tooling, and how a brain is instantiated from `source/`.

## Deploy and operate

These live with the code they describe, under `factory/source/system/brain_bin/` — the tree that
ships into a deployed brain and that the policy `@`-pointers resolve against — and are linked here:

- [DEPLOYMENT.md](../factory/source/system/brain_bin/DEPLOYMENT.md) — authoritative deployment guide for a brain's engine: import, residency, backup.
- [OPERATIONS.md](../factory/source/system/brain_bin/OPERATIONS.md) — day-2 operations: env-knob reference, port map, service lifecycle.
- [TROUBLESHOOTING.md](../factory/source/system/brain_bin/TROUBLESHOOTING.md) — symptom → diagnose → cause → fix notes for a deployed brain.
- [brain_security_model.md](../factory/source/system/brain_bin/brain_security_model.md) — the brain's security & isolation model (the `brain_invariants.md` `@`-pointer target).

## Gateway authorization

- [gateway_bearer_auth_SOP.md](gateway_bearer_auth_SOP.md) — the bearer-token authorization model for the gateway: read and read/write roles end to end.
- [gateway_token_admin_howto.md](gateway_token_admin_howto.md) — operator how-to: create, rotate, revoke, and list gateway bearer tokens.
- [gateway_auth_verification_matrix.md](gateway_auth_verification_matrix.md) — copy-paste by-hand matrix to prove gateway auth is live (localhost / Chroma).
- [gateway_offbox_validation_cookbook.md](gateway_offbox_validation_cookbook.md) — off-box / external LAN validation from a separate host across all services (chroma :8000, ollama :11434, action :8443).
- [testupload_through_gateway.py](testupload_through_gateway.py) — stdlib one-shot upload smoke test: pushes docs INTO the store through the gateway with a `chroma:writer` token (companion to the verification matrix §6).

## Diagrams

- [default_brain_network_overview.svg](default_brain_network_overview.svg) — plain-language overview of how the default brain works: caller, documents, gateway, answer.
- [default_brain_network_detail.svg](default_brain_network_detail.svg) — network topology detail: consumer side, gateway ports, bind posture.

## Documentation elsewhere in the repo

This `docs/` folder is the package documentation front. Other docs live **with the code they explain**;
each location below carries its own index or README:

- [`../README.md`](../README.md) — repo front door: install + orientation (AI-agent and human paths).
- [`../factory/source/system/brain_bin/index.md`](../factory/source/system/brain_bin/index.md) — the operational docs that ship inside every brain, plus the per-seam READMEs (deploy, provision, gateway, chroma).
- [`../factory/source/brain_etc.example/README.md`](../factory/source/brain_etc.example/README.md) — the config-seam template; per-seam READMEs for gateway, docker, chroma, ollama, tls, github.
- Neuron platform: [`../factory/source/system/common_neuron_platform/input/README.md`](../factory/source/system/common_neuron_platform/input/README.md) (write side) · [`../factory/source/system/common_neuron_platform/action/README.md`](../factory/source/system/common_neuron_platform/action/README.md) (read side).
- [`../factory/source/skills/index.md`](../factory/source/skills/index.md) — the skills scaffold a brain inherits.
- [`../aios/INSTALL.md`](../aios/INSTALL.md) — installing the factory as a Horizon AIOS Options Package.
