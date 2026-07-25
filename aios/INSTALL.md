# Installing the Brain Builder into Horizon.AIOS

This is the **optional** Horizon.AIOS wrapper. The builder in the repo root already works standalone with no
install — this layer registers it as a discoverable, sync-protected Options Package inside a Horizon.AIOS
instance. Unlike a skill package, **nothing is copied**: the builder is a heavy CLI toolchain, so
`install` registers it and injects a discovery pointer, and the toolchain runs **in place** from the
clone.

Installer: **`aios/install/horizon_brain_builder_package.py`** — cross-platform, standard-library only
(Python 3.8+). Subcommands: `install`, `uninstall`, `update`, `status`.

## Deployment model

A deployed package is a **git clone under `$HORIZON_SYSTEM/deployed_packages/<name>/`** (so it can pull
its own updates) plus a machine-local registry entry the Horizon.AIOS sync reads.

```
$HORIZON_SYSTEM/
  deployed_packages/
    horizon_brain_builder/                       ← git clone (this package; the toolchain runs here in place)
  ai_os_etc/
    horizon_deployed_packages.local.json         ← the deployed-packages registry (machine-local)
    horizon_aios_options_packages.local.md       ← +1 marker-delimited discovery pointer (machine-local,
                                                     git-ignored from OS canon, imported by root agents.md)
```

## What `install` does (idempotent)

1. **Migrates a stale block, if any.** If a marker-delimited block for this package is still present
   at the pre-retarget location (`$HORIZON_ROOT/projects/agents.md`), it is stripped there first, so an
   upgraded install never carries the block twice. No-op if that file doesn't exist or has no block.
2. **Injects a discovery-context pointer** — a marker-delimited block from `install/context_pointer.md`
   (clone path substituted) — into
   `$HORIZON_ETC/horizon_aios_options_packages.local.md`, creating that file (with a machine-local
   header comment) if it doesn't exist yet, so agents discover the builder and how to run it. Kept
   terse to respect the Horizon.AIOS terseness budget. The file is git-ignored via
   `.git/info/exclude` (never a tracked `.gitignore` entry), so the official OS sync lane never
   reverts it.
3. **Configures the clone pull-only** if it lives under `deployed_packages/` (a deployment mirror). The
   development canon (a checkout elsewhere, e.g. `projects/horizon_brain_builder`) is left push-enabled.
4. **Registers the package** in `$HORIZON_ETC/horizon_deployed_packages.local.json`: name, version
   (from `VERSION`), `clone_path`, git `remotes`, `upstream`, `role`/`pull_only`, `sync: true`, the
   `install_entrypoint`, and a `payload` manifest — including `context_block_file`, which now records
   `horizon_system/ai_os_etc/horizon_aios_options_packages.local.md` (the only reversible artifact).

**No toolchain copy.** The builder is invoked from the clone: deploy a brain by running the clone's
`deploy_brain.py` in place.

`uninstall` deregisters and strips the context block from the current target (and, defensively, any
stray block still at the legacy `projects/agents.md` location), **leaving the clone and every deployed
brain untouched**. `update` does `git fetch` + `reset --hard` to upstream, then re-registers (running
the same migration + injection as `install`). `status` prints the registry view plus the context-file
path, and flags a stale legacy block if one is still found.

## Registry ↔ sync integration

The Horizon.AIOS two-lane sync (`horizon_aios_sync.py`) reads this registry. Its **official lane** overwrites
everything except `projects/usrbin/brains` from upstream — which would otherwise clobber a package
living under the official-owned `horizon_system/`. The sync's `official_pathspec()` **also excludes
every registered clone with `sync != false`**, so a deployed package is protected from the overwrite
lane; the update pass runs `<entrypoint> update`; and the nightly nested-repo sync backs the clone up
to its own remote. Verify protection with:

```
python horizon_system/sbin/horizon_aios_sync.py --status
#   Deployed pkgs   : ... protected from official overwrite (horizon_system/deployed_packages/...)
```

## Run it

Clone the package to its deployed home, then run the installer from there:

```bash
git clone https://github.com/HorizonBrute/Horizon-Brain-Builder "$HORIZON_SYSTEM/deployed_packages/horizon_brain_builder"
python "$HORIZON_SYSTEM/deployed_packages/horizon_brain_builder/aios/install/horizon_brain_builder_package.py" install
```

Windows/PowerShell is identical — the same Python entry point:

```powershell
python "$env:HORIZON_SYSTEM\deployed_packages\horizon_brain_builder\aios\install\horizon_brain_builder_package.py" install
```

Options: `--horizon-root PATH` (default `$HORIZON_ROOT`), `--force` (refresh an existing registration).

Verify the context block landed:

```bash
grep -n "BEGIN horizon-brain-builder" "$HORIZON_SYSTEM/ai_os_etc/horizon_aios_options_packages.local.md"
```

## Uninstall

```
python .../aios/install/horizon_brain_builder_package.py uninstall
```

Removes the registry entry and the context block. **The clone and any brains already deployed are left
untouched** — each deployed brain is self-contained (it runs from its own staged copy).

## Notes

- The registry is `*.local.json` → machine-local: gitignored from OS canon (never rides the official
  lane) yet carried by the hourly personal backup sync (its name matches the `*local*` re-include).
- The installer writes only inside `$HORIZON_ETC` — the registry and a managed block in
  `horizon_aios_options_packages.local.md` (plus, transiently on migration, a strip pass over the
  legacy `projects/agents.md`). It does not touch privileged system dirs and copies no toolchain.
- The builder also runs with **zero Horizon.AIOS dependency**: standalone `create_brain.py` + the deployers
  resolve the install root from `--install-root`/`$AIOS_INSTALL_ROOT`, and read the brain password from
  the brain-owned OS-keyring namespace (`brain:<brain>`). No `HORIZON_*` is required outside this wrapper.
