#!/usr/bin/env python3
r"""
reapply_brain_configs.py - ONE tool to force the whole stack onto the on-disk config.
=====================================================================================

"1 tool to rule them all." Point it at a brain and it will, in order:

  1. REGENERATE all backend config from the human knob files (host-side):
       brain.env + gateway/gateway.conf + gateway/token_registry
         -> gateway/nginx_auto_gen/ (skeleton, chroma.conf, ollama.conf, ratelimit.conf)
         -> gateway/token_maps_auto_gen/ (the four nginx maps)
         -> gateway/fail2ban_autoconfigs/ (jail.d/filter.d/action.d)
     (this is exactly `gateway_config.py generate`).
  2. REGENERATE the mount->runtime copy manifest (wsl/apply.manifest).
  3. SYNC host config into the running stack and FORCE-RECREATE every service
     (chroma, ollama, gateway, fail2ban) so each container comes up on whatever
     config now sits on disk. Runs the seam apply primitive in-distro (which syncs
     mount->runtime FIRST, then runs the compose recreate, rolling back on failure).

The exposure OVERLAYS are layered automatically from brain.env: compose.chroma-gateway.yaml
when CHROMA_EXPOSE=on, compose.ollama-gateway.yaml when OLLAMA_EXPOSE=on. Images honor the
*_VERSION knobs (default :latest). Pull mode defaults to --pull never (use the baked-in
images: the runtime per-user WSL VM has no NIC under mirrored, so it cannot reach a registry);
--pull-always forces a :latest refresh on a networked host, --no-pull skips the pull pass.

Use it after ANY edit to a knob file, or just to slam the stack back to a known state.
"""
import argparse
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRAIN_DIR = HERE.parent.parent
RUN_AS_BRAIN = HERE / "run_as_brain.py"
APPLY_SH = "/opt/brain_wsl_in_distro_scripts/apply_brain_truths.sh"

import gateway_config
import brain_truths


def info(m): print(f"[reapply] {m}")


def _is_admin():
    """True iff this process is elevated (Windows). Firewall rule changes require it."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def reconcile_firewall(brain, bd):
    """Open/close the host's Windows Defender inbound rules to match the LAN surfaces brain.env
    exposes (chroma/ollama/action). The container recreate binds the ports INSIDE the stack, but a
    <SVC>_PUBLISH_TO_LAN surface stays unreachable OFF-box until a matching host firewall rule
    exists — so a bare recreate 'applies' a LAN flip that never reaches the LAN. Folding the
    reconciliation in here makes reapply one tool: flip the knob, run reapply, and the surface is
    both bound AND opened (or, when turned off, both unbound AND closed).

    gateway_port owns the exact rule model, derived from the SAME brain.env surface enumeration the
    config generator uses — so firewall and config never drift. No-op on non-Windows; SKIPPED with
    a loud warning when not elevated (the stack is already reapplied — we never abort that over a
    firewall step, but we never let a skipped firewall pass silently as 'done' either)."""
    if os.name != "nt":
        info("4/4 firewall reconciliation is Windows-only — skipped (stack IS reapplied).")
        return
    info("4/4 reconcile host firewall to brain.env LAN surfaces (chroma/ollama/action)")
    if not _is_admin():
        info("[WARN] not elevated — firewall NOT reconciled. Any *_PUBLISH_TO_LAN surface stays "
             "unreachable off-box until you re-run this from an Administrator console. "
             "The stack itself IS reapplied.")
        return
    import gateway_port
    try:
        gateway_port.firewall_apply(brain, bd)
    except SystemExit as e:
        info(f"[WARN] firewall reconciliation failed ({e}). Stack IS reapplied; check Windows "
             "Defender / re-run elevated.")


# The compose -f overlay set now lives in ONE place — gateway_config.compose_files (NOTE 001-58)
# — so every gateway-recreate site (here, gateway_tokens.apply_and_recreate, neuron_schedule, the
# boot keepalive) derives the SAME overlays from brain.env and none can recreate the gateway
# base-only. reapply calls it below as gateway_config.compose_files(env).


def main():
    ap = argparse.ArgumentParser(
        description="Regenerate all backend config from the knob files and force the whole "
                    "stack (chroma, ollama, gateway, fail2ban) onto the on-disk config.")
    ap.add_argument("--brain", default=BRAIN_DIR.name,
                    help=f"brain name (default from this tool's folder: {BRAIN_DIR.name})")
    ap.add_argument("--brain-dir", dest="brain_dir", default=None,
                    help="brain root (default: this tool's brain)")
    ap.add_argument("--no-pull", action="store_true",
                    help="do NOT pull any images (use the cached ones)")
    ap.add_argument("--pull-always", action="store_true",
                    help="force re-pull of every image (refresh :latest) — an explicit opt-in for a "
                         "networked host. Default is --pull never: use the baked-in images, never touch "
                         "the registry (the runtime per-user WSL VM has no NIC under mirrored). NEVER "
                         "default to always — on a fresh distro the full-layer download progress floods "
                         "stdout and OOMs a capturing caller (the deploy-stage reapply).")
    ap.add_argument("--services", nargs="*", metavar="SVC",
                    help="limit the recreate to these services (default: the whole stack)")
    ap.add_argument("--dry-run", action="store_true",
                    help="regenerate + write manifest, but print the recreate command instead of running it")
    args = ap.parse_args()

    bd = Path(args.brain_dir) if args.brain_dir else BRAIN_DIR
    brain = args.brain

    # 1. Regenerate the backend from the knob files (host-side).
    info("1/4 regenerating backend from brain.env + gateway.conf + token_registry")
    rc = gateway_config.cmd_generate(Namespace(brain_dir=str(bd)))
    if rc != 0:
        info("backend generation FAILED - aborting before touching the stack.")
        return rc

    # 2. Regenerate the mount->runtime manifest (LF bytes; a stray CR breaks the sync - obj 008).
    manifest = bd / "brain_etc" / "wsl" / "apply.manifest"
    manifest_text = brain_truths.build_manifest(brain, bd)
    manifest.write_bytes(manifest_text.encode("utf-8"))
    info(f"2/4 wrote wsl/apply.manifest ({manifest_text.count(chr(10))} entries)")

    # 3. Sync + force-recreate the stack in-distro (apply primitive rolls back on failure).
    env = gateway_config.read_env(gateway_config.brain_env_path(bd))
    files = gateway_config.compose_files(env)
    dockerdir = f"/home/{brain}/docker"
    fflags = " ".join(f"-f {dockerdir}/{f}" for f in files)
    # Images are baked into the engine at BUILD time by prefetch_images.sh — the brain's
    # per-user WSL VM has NO network interface under mirrored, so the runtime cannot pull.
    # Worse, `--pull missing` STILL contacts the registry to re-resolve a present *mutable*
    # tag (`:latest`) — only an immutable tag like nginx:1.27 is skipped — which fails with
    # no NIC. So the runtime default is `--pull never`: use the baked-in images, never touch
    # the registry. A rebuild (which re-runs prefetch) is how images update; --pull-always is
    # an explicit opt-in for a networked host that genuinely wants to refresh :latest.
    pull_policy = "always" if args.pull_always else "never"
    pull = f" --pull {pull_policy}"
    svcs = (" " + " ".join(args.services)) if args.services else ""
    compose_cmd = f"docker compose {fflags} up -d --force-recreate{pull}{svcs}"
    apply = f"bash {APPLY_SH} -- {compose_cmd}"
    info(f"3/4 sync + force-recreate  [overlays: {', '.join(files)}]"
         f"{pull or ' (no pull)'}{svcs or '  (whole stack)'}")

    if args.dry_run:
        info("dry-run - would run in-distro (via run_as_brain --wsl):")
        print("    " + apply)
        info("dry-run - would then reconcile the host firewall to brain.env LAN surfaces "
             "(chroma/ollama/action) via gateway_port.firewall_apply (elevated).")
        return 0

    if not RUN_AS_BRAIN.is_file():
        info(f"run_as_brain not found ({RUN_AS_BRAIN}); backend + manifest are written to the")
        info("seam and will apply on the next keepalive. Skipping live recreate.")
        return 0

    # 3a. PULL first, as its OWN seam-apply pass (bug 17). The exposure overlay compose files
    #     (compose.chroma-gateway.yaml, …) are placed in ~/docker/ by apply_brain_truths' SYNC —
    #     they do NOT exist there beforehand, so a pull that references them via -f must go THROUGH
    #     apply_brain_truths too (a bare pre-sync `docker compose … pull` dies "no such file:
    #     compose.chroma-gateway.yaml"). WHY split the pull off at all: a fresh-from-engine distro has
    #     EMPTY docker (the engine tar carries no images), so the recreate below would download the
    #     WHOLE stack INLINE and fold any transient registry blip into the compose rc that gates
    #     apply_brain_truths' rollback — rolling back a deploy whose containers actually came up healthy
    #     and reporting a FALSE deploy failure. (Proven on a warm distro: the identical recreate with
    #     images already cached returns rc 0.) This pull pass (`--policy {pull_policy}` — the same
    #     policy the recreate uses, `-q` = no progress flood, retried once for transients) front-loads
    #     the heavy download OUTSIDE the recreate, so the recreate's own `--pull` is a fast no-op whose
    #     rc reflects ONLY create/start. With the default `never` policy this pass touches no registry
    #     (baked images); it does real work only under `--pull-always` on a networked host. A rollback
    #     of THIS pass is harmless — it recreates no container; if it still fails after the retry we
    #     fall through and the recreate's inline `--pull` becomes the fallback. Warm distro = fast no-op.
    if not args.no_pull:
        pull_cmd = f"docker compose {fflags} pull --policy {pull_policy} -q{svcs}"
        pull_apply = f"bash {APPLY_SH} -- {pull_cmd}"
        info(f"3a/4 pull pass ({pull_policy}, quiet) — front-load the download so the recreate is a fast no-pull recreate")
        prc = 1
        for attempt in (1, 2):
            prc = subprocess.run([sys.executable, str(RUN_AS_BRAIN), "--brain", brain, "--wsl",
                                  "--", pull_apply]).returncode
            if prc == 0:
                break
            info(f"    pull pass attempt {attempt} rc={prc}"
                 + ("; retrying once..." if attempt == 1 else
                    f"; continuing — the recreate's `--pull {pull_policy}` will fetch anything still "
                    "absent (and fail loudly if it truly can't)."))
        if prc == 0:
            info("    images present.")

    # 3b. DOWN first (whole-stack reapply only), to DETERMINISTICALLY release host ports
    #     BEFORE the recreate rebinds them. Rootless Docker's port forwarder (rootlesskit)
    #     tears down a removed container's host-port publish ASYNCHRONOUSLY, so a whole-stack
    #     `up -d --force-recreate` races: the new gateway's AddPort(0.0.0.0:11434) can fire
    #     before the old gateway's forward is released -> "bind: address already in use" ->
    #     the recreate fails, apply_brain_truths rolls back, and a stack that would have come
    #     up healthy reports a FALSE deploy failure (root-caused on the first clean
    #     from-scratch deploy, 2026-07-13 — the ollama overlay's :11434 publish is the one
    #     that loses the race). A synchronous `down` removes every container + its port
    #     forward and WAITS, so the following `up` binds a clean host. Named volumes persist
    #     (down without -v) — no data loss. SKIPPED for a partial --services reapply (downing
    #     the whole stack to refresh one service would be destructive); those keep the plain
    #     force-recreate below. Routed THROUGH apply_brain_truths like the pull pass so the
    #     overlay -f files it references already exist in ~/docker (the sync places them; a
    #     bare `down -f overlay.yaml` on a fresh distro dies "no such file"). Best-effort: on
    #     a fresh distro with nothing up, `down` is a harmless no-op.
    if not svcs:
        down_cmd = f"docker compose {fflags} down --remove-orphans"
        down_apply = f"bash {APPLY_SH} -- {down_cmd}"
        info("3b/4 down pass — release host ports before recreate (rootless port-race guard)")
        subprocess.run([sys.executable, str(RUN_AS_BRAIN), "--brain", brain, "--wsl",
                        "--", down_apply])

    rc = subprocess.run([sys.executable, str(RUN_AS_BRAIN), "--brain", brain, "--wsl",
                         "--", apply]).returncode
    if rc != 0:
        info(f"FAILED (rc={rc}) - apply_brain_truths rolled the config back (see output above).")
        info("skipping firewall reconciliation — the stack did not reapply, so opening/closing "
             "host ports to it would not match the running reality.")
        return rc
    info("OK - stack reapplied to the on-disk config.")

    # 4. Reconcile the host firewall to the LAN surfaces brain.env exposes. Only after a clean
    #    recreate: opening ports to a rolled-back stack would advertise a surface that isn't there.
    reconcile_firewall(brain, bd)
    return rc


if __name__ == "__main__":
    sys.exit(main() or 0)
