# Docs index

Operational documentation for the brain factory. One line per document.

## Overview

- [README.md](README.md) — what the factory is: the canonical build/deploy tooling, and how a brain is instantiated from `source/`.

## Deploy and operate

- [DEPLOYMENT.md](DEPLOYMENT.md) — authoritative deployment guide for a brain's engine: import, residency, backup.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — symptom → diagnose → cause → fix notes for a deployed brain.

## Gateway authorization

- [gateway_bearer_auth_SOP.md](gateway_bearer_auth_SOP.md) — the bearer-token authorization model for the gateway: read and read/write roles end to end.
- [gateway_token_admin_howto.md](gateway_token_admin_howto.md) — operator how-to: create, rotate, revoke, and list gateway bearer tokens.
- [gateway_auth_verification_matrix.md](gateway_auth_verification_matrix.md) — copy-paste by-hand matrix to prove gateway auth is live (localhost / Chroma).
- [gateway_offbox_validation_cookbook.md](gateway_offbox_validation_cookbook.md) — off-box / external LAN validation from a separate host across all services (chroma :8000, ollama :11434, action :8443).

## Diagrams

- [default_brain_network_overview.svg](default_brain_network_overview.svg) — plain-language overview of how the default brain works: caller, documents, gateway, answer.
- [default_brain_network_detail.svg](default_brain_network_detail.svg) — network topology detail: consumer side, gateway ports, bind posture.
