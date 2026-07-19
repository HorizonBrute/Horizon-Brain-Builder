#!/usr/bin/env python3
"""
brain_service.py — legible operator verbs over the brain's services.
=====================================================================

Brain-owned. Gives an operator plain commands like ``brain_service chroma restart``
instead of hand-driving ``wsl -d brain-<brain> -- bash -lc "cd ~/docker && docker
compose ..."`` across the Windows→WSL boundary and getting the environment
(rootless Docker's XDG_RUNTIME_DIR) or the service name wrong every time.

It rides on run_as_brain: every verb is a short shell command executed IN the
brain's runtime environment AS the brain uid (rootless — never root; the daemon
and containers all run as the brain, per the security model). So this module
never touches credentials or platform dispatch itself — run_as_brain owns that.

THE SERVICES (three layers, bottom-up)
  docker   — the rootless dockerd daemon (user systemd unit). The layer beneath
             the containers. start/stop/restart/status.
  chroma   — the Chroma vector-store container (compose service 'chroma').
  gateway  — the nginx read-access gateway (compose service 'gateway'); adds
             'reload' for TLS-cert rotation without dropping connections.
  ollama   — the sealed embedding/LLM model server (compose service 'ollama').
             Lifecycle only here; MODEL administration (which models the store
             holds) is the declarative roster tool `ollama_models.py`.
  stack    — the whole ~/docker compose stack at once (up/down/restart/status/logs).

Rootless Docker needs XDG_RUNTIME_DIR pointed at the user runtime dir; a
non-login WSL shell does not always set it, so every command exports it first
(matches provision/stage3_brain.sh).

USAGE
    brain_service.py [--brain NAME] [--dry-run] <service> <action>

  Examples:
    brain_service.py chroma status
    brain_service.py chroma restart
    brain_service.py gateway reload        # after a TLS cert swap
    brain_service.py docker status
    brain_service.py stack up
    brain_service.py --dry-run stack down  # print the resolved command only
"""
import argparse
import sys

import run_as_brain  # sibling in system/brain_sbin/

# Rootless Docker's per-user runtime dir (see provision/stage3_brain.sh). Prefixed
# to every command so `docker` / `systemctl --user` find the rootless socket.
XDG = "export XDG_RUNTIME_DIR=/run/user/$(id -u)"
STACK = "cd ~/docker"  # the deployed compose stack lives at ~/docker (brain HOME)

# heartbeat: gateway publishes :8000 as TLS (self-signed) once deployed; a
# gateway-less brain serves plain http. Try TLS-insecure first, then http.
_HEARTBEAT = ("(curl -sk https://127.0.0.1:8000/api/v2/heartbeat "
              "|| curl -s http://127.0.0.1:8000/api/v2/heartbeat); echo")

# service -> action -> in-distro shell command (run as the brain uid).
SERVICES = {
    "docker": {
        "start":   "systemctl --user start docker",
        "stop":    "systemctl --user stop docker",
        "restart": "systemctl --user restart docker",
        "status":  "systemctl --user is-active docker; "
                   "systemctl --user status docker --no-pager -l | head -20",
    },
    "chroma": {
        "start":   f"{STACK} && docker compose up -d chroma",
        "stop":    f"{STACK} && docker compose stop chroma",
        "restart": f"{STACK} && docker compose restart chroma",
        "status":  f"{STACK} && docker compose ps chroma && {_HEARTBEAT}",
    },
    "gateway": {
        "start":   f"{STACK} && docker compose up -d gateway",
        "stop":    f"{STACK} && docker compose stop gateway",
        "restart": f"{STACK} && docker compose restart gateway",
        # graceful reload: re-read config/certs without dropping live connections.
        "reload":  f"{STACK} && docker compose exec -T gateway nginx -s reload",
        "status":  f"{STACK} && docker compose ps gateway",
    },
    "ollama": {
        "start":   f"{STACK} && docker compose up -d ollama",
        "stop":    f"{STACK} && docker compose stop ollama",
        "restart": f"{STACK} && docker compose restart ollama",
        "status":  f"{STACK} && docker compose ps ollama",
        "logs":    f"{STACK} && docker compose logs --tail 50 ollama",
    },
    "stack": {
        "up":      f"{STACK} && docker compose up -d",
        "down":    f"{STACK} && docker compose down",
        "restart": f"{STACK} && docker compose restart",
        "status":  f"{STACK} && docker compose ps",
        "logs":    f"{STACK} && docker compose logs --tail 50",
    },
}


def _usage_services():
    lines = ["services and actions:"]
    for svc, actions in SERVICES.items():
        lines.append(f"  {svc:<8} {', '.join(actions)}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        prog="brain_service",
        description="Operator verbs over the brain's services (docker/chroma/gateway/ollama/stack).",
        epilog=_usage_services(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--brain", help="brain name (default: from .brain_provision.json or folder)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the resolved in-distro command without executing")
    ap.add_argument("service", choices=list(SERVICES), help="which service")
    ap.add_argument("action", help="the action (see the list below)")
    args = ap.parse_args()

    actions = SERVICES[args.service]
    if args.action not in actions:
        ap.error(f"unknown action '{args.action}' for '{args.service}'. "
                 f"valid: {', '.join(actions)}")

    brain = run_as_brain.brain_name(args)
    command = f"{XDG}; {actions[args.action]}"
    print(f"brain_service: {brain} {args.service} {args.action}")
    rc = run_as_brain.run(brain, [command], target="runtime", dry_run=args.dry_run)
    sys.exit(rc)


if __name__ == "__main__":
    main()
