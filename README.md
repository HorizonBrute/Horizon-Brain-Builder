# Brain Builder

**Build and deploy _brains_ — per-brain sealed RAG runtimes — from one CLI toolchain.**
A "brain" is a self-contained retrieval stack: **ChromaDB + Ollama behind an nginx token
gateway**, running in a dedicated **WSL2 distro** on Windows or **rootless Docker** on Linux.
Everything is token-gated and network-sealed; the gateway is the only door.

> **Dual identity — read this first.**
> This repo is a **deployable module inside [Horizon AIOS](https://github.com/HorizonBrute/Horizon_AI_OS)**
> (an *Options Package* the OS can install, register, sync and update) **that is deliberately designed to
> run fully standalone** — on an ordinary host with **zero AIOS dependency**. The brain-building *core is
> identical* either way; the AIOS layer (`aios/`) only adds discovery + sync-protected updates on top. If
> you have no Horizon AIOS, you install nothing — you just clone and deploy.

This repo is the **code** (the build tooling), never a running brain. New brains are *instantiated from* it;
a deployed brain runs its **own staged copy** of this tree.

---

## Two audiences — start where you fit

### 🤖 If you are an AI agent: get installed and oriented correctly

**First decide which world you are in:**

| Signal | You are… | Do this |
|---|---|---|
| `$HORIZON_SYSTEM` / `$HORIZON_ROOT` are set and a `horizon_system/` tree exists | **inside a Horizon AIOS instance** | **Install as an Options Package** (below). It registers the builder and injects a discovery pointer — the toolchain still runs *in place* from the clone; **nothing is copied** into the OS. |
| No `HORIZON_*` env, no AIOS tree | **standalone host** | **Do not install anything.** Clone the repo and deploy a brain directly with the orchestrator, passing `--install-root <dir>` (or setting `$AIOS_INSTALL_ROOT`). |

**Install as a Horizon AIOS Options Package** (idempotent, stdlib-only, Python 3.8+):
```bash
git clone https://github.com/HorizonBrute/Horizon-Brain-Builder \
  "$HORIZON_SYSTEM/deployed_packages/horizon_brain_builder"
python "$HORIZON_SYSTEM/deployed_packages/horizon_brain_builder/aios/install/horizon_brain_builder_package.py" install
```
`install` → injects a discovery pointer into `projects/agents.md`, registers the package in
`horizon_deployed_packages.local.json` (so AIOS sync **protects, updates and backs it up**), and sets the
clone pull-only. `uninstall` reverses both and **leaves the clone and every deployed brain untouched**.
`update` = `git fetch` + reset-to-upstream. `status` prints the registry view.
**Full contract: [`aios/INSTALL.md`](aios/INSTALL.md).**

### 🧑 If you are a human: understand and use the tooling

You run one **orchestrator** per host OS; it sequences every step (OS account → WSL/Docker engine →
gateway → residency) into a single converging `deploy`, and its inverse:

```bash
# Windows / WSL2 — from an elevated shell:
python windows_deploy_brain.py deploy   --brain mybrain --install-root C:\brains --posture personal
python windows_deploy_brain.py verify   --brain mybrain
python windows_deploy_brain.py status   --brain mybrain
python windows_deploy_brain.py teardown --brain mybrain [--purge --yes]

# Native Linux — with sudo:
sudo python3 linux_deploy_brain.py deploy   --brain mybrain --install-root /srv/brains --posture personal
sudo python3 linux_deploy_brain.py teardown --brain mybrain [--purge --yes]
```
`--posture personal` binds the gateway to the host loopback only; `--posture server` exposes it on the
LAN (see the security model before you do). A plain `deploy` re-stages code onto an existing brain;
`--from-scratch` rebuilds the engine from a base image. **Full options + the walkthrough:
[`DEPLOYMENT.md`](factory/source/system/brain_bin/DEPLOYMENT.md).**

---

## How the two modes actually differ

| | Standalone | As a Horizon AIOS Options Package |
|---|---|---|
| **Install step** | none — clone and run | `aios/install/horizon_brain_builder_package.py install` |
| **Install root** | `--install-root <dir>` or `$AIOS_INSTALL_ROOT` (explicit — no default, no guess) | same; inside AIOS it can also fall back to `$HORIZON_ROOT` |
| **Brain password** | OS keyring, brain-owned namespace `brain:<brain>` | same (no AIOS keystore dependency in the core) |
| **`HORIZON_*` env** | **not required anywhere in the core** | read only by the `aios/` wrapper, never by the brain-build core |
| **Updates** | `git pull` yourself | AIOS sync runs `update` and backs the clone up to its remote |

The standalone seam is real code, not a stub: `factory/create_brain.py` is the standalone brain-provisioning
provider (`--install-root` aware, brain-owned keyring), and the deployers resolve the install root
explicit-or-die. **The `aios/` wrapper is the _only_ place that is allowed to know about Horizon AIOS.**

---

## Repository layout

```
README.md                     ← you are here (the front door)
VERSION                       ← package version (semver); the installer reads it
LICENSE                       ← AGPL-3.0
windows_deploy_brain.py       ← Windows/WSL2 deploy orchestrator (host-side; run elevated)
linux_deploy_brain.py         ← native-Linux deploy orchestrator (systemd + rootless Docker; run with sudo)
factory/                      ← build tooling + brain image (dir keeps its historical name; the deploy staging root)
  create_brain.py             ← standalone brain-provisioning provider (OS account/group/ACL/keyring)
  source/                     ← THE BRAIN IMAGE: exactly the tree a deployed brain gets (copied at deploy)
    system/brain_bin/         ← tier-1 canon: operator tooling + the operational docs (ship into each brain)
    system/brain_sbin/        ← tier-1 canon: config generator, gateway, port/token tooling
    system/common_neuron_platform/  ← the neuron bundle images (input = write side, action = read side)
    brain_etc.example/        ← the config-seam template (brain.env, gateway, docker, tls, …)
docs/                         ← package documentation (index, gateway auth, network diagrams)
aios/                         ← the OPTIONAL Horizon AIOS Options-Package wrapper (installer + INSTALL.md)
```

---

## Documentation map

- **[`docs/README.md`](docs/README.md)** — the canon → instance → runtime **three-tier model** and the
  orchestrator reference (what `deploy` copies, and when).
- **[`docs/index.md`](docs/index.md)** — full documentation index.
- **Operational docs (they ship inside every deployed brain, at `system/brain_bin/`):**
  [`DEPLOYMENT.md`](factory/source/system/brain_bin/DEPLOYMENT.md) ·
  [`OPERATIONS.md`](factory/source/system/brain_bin/OPERATIONS.md) ·
  [`TROUBLESHOOTING.md`](factory/source/system/brain_bin/TROUBLESHOOTING.md) ·
  [`brain_security_model.md`](factory/source/system/brain_bin/brain_security_model.md)
- **Gateway authorization:** [`docs/gateway_bearer_auth_SOP.md`](docs/gateway_bearer_auth_SOP.md) and the
  by-hand [`gateway_auth_verification_matrix.md`](docs/gateway_auth_verification_matrix.md).
- **Installing as an AIOS module:** [`aios/INSTALL.md`](aios/INSTALL.md).

---

## Requirements

- **Windows:** Windows 10/11 with **WSL2**; run the orchestrator from an **elevated** shell.
- **Linux:** **systemd** + **rootless Docker**; run the orchestrator with **sudo**.
- **The AIOS installer** (`aios/install/…`) is **standard-library only**, Python 3.8+ — no third-party deps.

## Version & license

`VERSION` = **0.2.0**. Licensed **AGPL-3.0** (see [`LICENSE`](LICENSE)).
Namespace for the AIOS package is `horizon_brain_builder_*` — never `horizon_aios_*` (that is OS core).
