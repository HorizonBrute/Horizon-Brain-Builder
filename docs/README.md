# Brain Factory — canonical brain-build tooling

**This repo is the single source of truth for the brain-build/deploy tooling.**
It is the CODE ("factory"), not any running brain. New brains are instantiated FROM
here; a live brain's `system/brain_bin/` / `system/brain_sbin/` is a staged *instance*
of this, never the master.

## The three tiers this keeps separate

| Tier | What | Where | Policy |
|------|------|-------|--------|
| 1. Canon source | the tooling CODE | `factory/source/system/brain_bin`, `.../brain_sbin` | tracked/versioned; secrets excluded |
| 2. Per-brain instance | tier-1 code COPIED straight from the source tree + parametrized (`BRAIN=<name>`) | `<install-root>/<brain>/system/...` | staged direct-from-source (no tarball) |
| 3. Runtime state | live WSL disk (`<brain>/wsl/`), the vector store (`knowledge/brain_rw/chroma`), live `.env`, tokens, certs | per-brain | git-ignored, never flows upstream |

`deploy` stages the per-brain instance by **copying the code member set straight from
the source tree you are running from** — the delivered artifact IS the git repo. There is
no build step and no tarball: canon → instance is a direct copy.

## Where a brain gets installed

There is **no default install root and no search.** The deploy takes it from, in order:

1. `--install-root <dir>`
2. the `AIOS_INSTALL_ROOT` environment variable

If neither is set, the deploy stops with a message telling you to set one. `<install-root>`
throughout these docs means that directory — the one that contains `<brain>/`.

## Layout
- `windows_deploy_brain.py` — **the Windows/WSL2 deploy orchestrator** (host-side; see below).
- `linux_deploy_brain.py` — **the native-Linux deploy orchestrator** (systemd + rootless
  Docker + bind-mount seam, no VM). Run with `sudo`; see its module docstring.
- `factory/source/` — the brain image: exactly the tree a deployed brain gets.
- `factory/source/system/brain_bin/`, `.../brain_sbin/` — tier-1 canon (code only; runtime +
  secrets excluded).
- `docs/` — these operational docs.

> **Which files reach a brain, and when?** There is **no build/repackage step**. The
> orchestrator (`windows_deploy_brain.py`) runs in place, AND the payload under
> `factory/source/system/**` is **copied from the source tree at deploy time** — so a source
> edit anywhere in this repo is live on the next `deploy`. See **"How a source edit reaches a
> brain"** in [`DEPLOYMENT.md`](DEPLOYMENT.md) (§0).

## The orchestrator — one entry → working brain
Host-side tooling (NOT staged inside the per-brain instance — it runs *before* the brain
exists and copies the code into it). It sequences every building block into one converging
deploy and its inverse. Pick the orchestrator for the host OS:

```
# Windows / WSL2 (run from an elevated shell):
python windows_deploy_brain.py deploy   --brain X --install-root DIR --posture personal|server [--port N] [--engine-tar T]
python windows_deploy_brain.py teardown --brain X [--purge --yes]
python windows_deploy_brain.py verify   --brain X
python windows_deploy_brain.py status   --brain X

# Native Linux (run with sudo):
sudo python3 linux_deploy_brain.py deploy   --brain X --install-root DIR --posture personal|server [--port N]
sudo python3 linux_deploy_brain.py teardown --brain X [--purge --yes]
sudo python3 linux_deploy_brain.py verify   --brain X
sudo python3 linux_deploy_brain.py status   --brain X
```

The two share the same verbs, vocabulary, and portable building blocks (the staged-from-source
code, the gateway compose stack, `run_as_brain`, the seam concept, the bootstrap-token mint).
They differ only where the OS forces it: **engine** (WSL distro tar vs native rootless-Docker
home), **residency** (Task Scheduler boot task vs systemd + linger), **seam mount** (drvfs 9p +
`icacls` vs bind mount + POSIX perms). See `linux_deploy_brain.py`'s docstring for the full
Windows↔Linux mapping.

Deploy stages (each idempotent — checks "already done?" and converges):
1. **Preflight** — elevation, brain-runnable Python (`brain_python_resolver preflight`),
   `keyring` importable, WSL2 present. Changes nothing; fails loud.
2. **Create brain** — provider seam: if the host supplies an external create-brain hook, the
   deploy calls it; otherwise it uses the bundled standalone `create_brain.py`. Skipped if the
   account already exists.
3. **Stage code** — `_stage_from_source` COPIES the code member set (`system/`,
   `brain_etc.example/`, and optional `impulses/`/`knowledge/`) straight from `factory/source/`
   into `<install-root>/<brain>/` **without** clobbering tier-3 runtime state.
4. **Ensure engine** — a fresh deploy **builds the engine from scratch by default** (download
   base Debian → provision → import); a prebuilt tar is NEVER a required input. The transient
   `<brain>_engine.tar` is only the medium for the Windows cross-account hop (builder `wsl
   --export` → brain `wsl --import`) and is DELETED after a successful deploy. Opt-in:
   `--from-scratch` forces a fresh rebuild; `--engine-tar <path>` restores/deploys a prebuilt
   engine; `--export-engine [DIR]` keeps (bare) or moves (with DIR) the tar for backup.
5. **Deploy engine** — invokes the *staged* `brain_installer_1_admin.py` (which runs phase 2 +
   registers residency). The orchestrator calls the CLI; it does not reimplement it.
6. **Gateway** — staged `gateway_port.py set --port … --bind …`, then mints a bootstrap
   reader token via `run_as_brain --root --script gateway_token.py -- create …`.
7. **Verify** — TLS heartbeat (200) + `reset` (403, write-sealed) through the gateway CA.

`teardown` (default) stops the distro + deletes the residency task + releases the gateway
port (**non-destructive** — data preserved, deploy rebuilds). `--purge --yes` unregisters the
distro (deletes data), removes the account, deletes the folder — the "rebuild from nothing"
lever. Design rule: **calls block CLIs, never their internals** — the stable command line is
the contract, so the orchestrator stays decoupled from installer/gateway churn.

## `create_brain.py` — the standalone provisioner
Creates the brain's OS account, its per-brain runtime group (`<brain>_group`), the brain
folder + its ACL, and an auto-generated password stored in the OS keyring under the
brain-owned namespace (service `brain:<brain>`, username `account_password` — the same
namespace `run_as_brain.py` reads).

If the host platform provides its own create-brain tool, the deploy calls that instead
(the provider seam, stage 2 above) and this script is not used. The seam is env-var driven:
no hook configured = standalone.

## The naming contract
A deployed brain owns these host-visible names — all derived from `<brain>`:

| Thing | Name |
|---|---|
| WSL distro | `brain-<brain>` |
| Residency task | `<brain>-docker-keepalive` |
| Containers | `<brain>-chroma`, `<brain>-gateway`, `<brain>-input_neurons`, `<brain>-action_neurons` |
| Firewall rules | `brain-<brain>-gw-<surface>` (one per exposed surface) |
| Keyring credential | service `brain:<brain>`, user `account_password` |
| Gateway token registry | `brain_etc/gateway/token_registry` (minted named tokens; scope grants `chroma:reader`/`chroma:writer`, `ollama:use`/`ollama:admin`, `action:call`) |

## Excluded from staging (never copied into a brain)
`_stage_from_source` excludes: live `.env` (only `.env.example` ships), token maps (`*.map` —
only `*.example`; EMPTY templates ship, a POPULATED map is refused as a leak), TLS material
(`certs/`, `*.pem`, `*.key`), the WSL engine dir (`wsl/`) + the vector store
(`knowledge/brain_rw/chroma`), `__pycache__`/`*.pyc`.

## Known gaps
- **Standalone `installer_1` keyring read.** `create_brain.py` stores the password under
  `brain:<brain>` / `account_password`, but `brain_installer_1_admin.py` still reads it via an
  external credential helper that only exists when a provider seam is configured → otherwise it
  falls to a prompt/die. `run_as_brain.py` already reads the brain-owned namespace;
  `installer_1` should too.
- **Server-posture operator tooling** has known automount-off breakage (`gateway_port` /
  `gateway_token` needing `/mnt`; `run_as_brain` ~1024-char cap). Treat as a parallel
  block-fix track the orchestrator consumes.
- **LAN reach under `--posture server`** is implemented but not yet confirmed by a live
  off-box test — see [`DEPLOYMENT.md`](DEPLOYMENT.md) §0 and
  [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) §D.
