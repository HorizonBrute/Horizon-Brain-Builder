#!/usr/bin/env python3
r"""
gateway_port.py — assign a brain's read-access-gateway host port + firewall (brain_sbin).
=========================================================================================

Multi-brain hosting: each brain is its own WSL2 distro forwarding ITS loopback to the
ONE shared Windows host loopback, so two brains both on 127.0.0.1:8000 collide at the
host. This tool gives a brain a **distinct host port**, and — for Server posture
(bind 0.0.0.0) — a **matching per-brain Windows Defender inbound rule**.

Two identities, on purpose (see the co-session handoff
handoffs/2026-07-02_multibrain-port-firewall_to_gateway-agent.md):
  * HOST-ADMIN (this tool, elevated console) — collision check + firewall. These need
    real Administrator, NOT the brain, so they run here via PowerShell directly.
  * BRAIN-in-distro — the `~/docker/.env` edit + `docker compose up -d gateway`. This
    is dispatched through the co-session's `run_as_brain.py` (become the brain → wsl),
    so we never re-plumb runas/wsl/credentials.

Run this in an ELEVATED console.

    gateway_port.py show
    gateway_port.py set --port 8001                       # Personal (loopback), removes any fw rule
    gateway_port.py set --port 8001 --bind server         # Server (0.0.0.0) + subnet fw rule
    gateway_port.py release                                # drop this brain's fw rule + registry row

A port-only change does NOT touch the cert (only the *bind* → LAN affects the SAN); switching
to Server warns you to re-run gen-cert.sh with a LAN SAN so off-box clients can validate.
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Windows consoles default to cp1252 stdout; this tool's status lines carry en/em dashes
# and a → in the firewall/gateway summaries. Force UTF-8 so a Server-posture apply never
# dies on an encode error AFTER firewall_apply already created the rules (which would leave
# the registry save unreached and the on-host state half-written).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent          # <brain>/brain_sbin
BRAIN_DIR = HERE.parent.parent                          # <brain>
RUN_AS_BRAIN = HERE / "run_as_brain.py"          # the co-session's dispatcher
# Host-scoped registry of brain→port (this tool is inherently multi-brain/host, so an
# install-root-relative home is acceptable here — unlike the portable brain tools).
# It lives at the install root itself, NOT under brains/: the harden model makes brains/
# read-only to human operators (an inherited DENY-write for the human-operators group),
# and the deploy runs elevated AS a human — a brains/ home would deny the registry write.
# The root grants the human-operators group Full while still denying the brains group.
# The root is NEVER guessed: $AIOS_INSTALL_ROOT, else the Horizon.AIOS platform's
# $HORIZON_ROOT, else die (see _install_root). The deployer exports $AIOS_INSTALL_ROOT to
# its children; a MANUAL invocation on a Horizon.AIOS host inherits it from $HORIZON_ROOT.
_AIOS_INSTALL_ROOT_ENV = os.environ.get("AIOS_INSTALL_ROOT")
_HORIZON_ROOT_ENV = os.environ.get("HORIZON_ROOT")

BIND_ALIASES = {"personal": "127.0.0.1", "server": "0.0.0.0",
                "127.0.0.1": "127.0.0.1", "0.0.0.0": "0.0.0.0"}


def info(m): print(f"  {m}")
def die(m): print(f"[gateway_port] ERROR: {m}", file=sys.stderr); sys.exit(1)


def _install_root():
    """The install root (the folder that holds brains/). $AIOS_INSTALL_ROOT, else the platform's
    $HORIZON_ROOT, else die — never guessed, never walked: a wrong root writes the port registry
    somewhere no other brain will read. On a Horizon.AIOS install $HORIZON_ROOT IS the install
    root (the folder that holds brains/), so it is the correct fallback for a manual invocation."""
    root = _AIOS_INSTALL_ROOT_ENV or _HORIZON_ROOT_ENV
    if not root:
        die("$AIOS_INSTALL_ROOT is unset and this is not a Horizon.AIOS install ($HORIZON_ROOT "
            "unset). Set $AIOS_INSTALL_ROOT to the install root (the folder that holds brains/) "
            "so the host-scoped gateway port registry has one agreed home.")
    return Path(os.path.abspath(root))


def registry_path():
    return _install_root() / "gateway_ports.json"


def require_admin():
    try:
        if not ctypes.windll.shell32.IsUserAnAdmin():
            die("run from an Administrator console (firewall + collision check need admin).")
    except Exception:
        die("could not verify elevation; run from an Administrator console.")


def ps(script):
    """Run a PowerShell snippet; return (rc, stdout, stderr)."""
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def brain_name(args):
    if args.brain:
        return args.brain
    prov = BRAIN_DIR / ".brain_provision.json"
    if prov.is_file():
        try:
            return json.loads(prov.read_text(encoding="utf-8"))["brain_name"]
        except Exception:
            pass
    return BRAIN_DIR.name


# ---- registry (best-effort; the live-binding check is the real guarantee) -----------
def load_registry():
    reg_path = registry_path()
    if reg_path.is_file():
        try:
            return json.loads(reg_path.read_text(encoding="utf-8"))
        except Exception:
            info(f"[WARN] registry unreadable, ignoring: {reg_path}")
    return {}


def save_registry(reg):
    try:
        reg_path = registry_path()
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        reg_path.write_text(json.dumps(reg, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as e:
        info(f"[WARN] could not write registry ({e}); live-binding check still enforced.")


# ---- collision check ----------------------------------------------------------------
def port_listener(port):
    """Return a short description of whatever is LISTENING on the host port, or ''."""
    rc, out, _ = ps(
        f"$c = Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue | "
        f"Select-Object -First 1; if ($c) {{ $p = Get-Process -Id $c.OwningProcess "
        f"-ErrorAction SilentlyContinue; \"$($c.LocalAddress):$($c.LocalPort) $($p.ProcessName)\" }}")
    return out if rc == 0 else ""


def assert_port_free(brain, port, reg, force=False):
    # (a) another brain reserved it? — NEVER bypassable, not even with --force.
    for b, row in reg.items():
        if b != brain and int(row.get("port", 0)) == port:
            die(f"port {port} is reserved by brain '{b}' (registry {registry_path()}). "
                f"Pick another port (auto-allocation is deferred).")
    # (b) something is actively listening on it, and it isn't this brain already on it?
    #     --force skips this: the listener may be THIS brain's own gateway already on
    #     :port when it isn't yet in the registry (re-asserting the current port).
    already = int(reg.get(brain, {}).get("port", 0)) == port
    listener = port_listener(port)
    if listener and not already and not force:
        die(f"port {port} is already in use on the host by [{listener}]. Pick a free port, "
            f"or pass --force if that listener is THIS brain's own gateway already on :{port}.")


# ---- in-distro: edit .env + recreate the gateway (via run_as_brain) -----------------
def apply_in_distro(brain, port, bind):
    if not RUN_AS_BRAIN.is_file():
        die(f"run_as_brain.py not found at {RUN_AS_BRAIN} (co-session tool). Cannot reach the distro.")
    # This distro-side work is a NON-TRIVIAL shell script (a function, sed, conditionals,
    # multiple variable refs). It CANNOT ride the run_as_brain `--` bridge INLINE: every bare
    # $VAR parameter-expansion marshals to EMPTY across the bridge (proven live — only $(...)
    # command-substitution and a bare-word ~ survive). So an inline `f=~/docker/.env` /
    # `upsert{ …$1…$2…$f… }` silently collapses. Ratified contract: deliver non-trivial shell
    # as a SCRIPT, never inline `--`. The script now lives in the wsl_in_distro_scripts seam,
    # mounted read-only in-distro at /opt/brain_wsl_in_distro_scripts, and we run it BY PATH,
    # passing port/bind as positional args (both bridge-safe simple values — no spaces, no $).
    setport = "/opt/brain_wsl_in_distro_scripts/gateway_set_port.sh"
    cmd = [sys.executable, str(RUN_AS_BRAIN), "--brain", brain, "--wsl", "--",
           "bash", setport, str(port), bind]
    info(f"in-distro: set CHROMA_PORT={port} GW_BIND_ADDRESS={bind} + recreate gateway (via run_as_brain)")
    if subprocess.run(cmd).returncode != 0:
        die("in-distro .env edit / gateway recreate failed (see output above).")


# ---- host firewall (per-brain, GW-config-driven, idempotent by stable name) ----------
# A Server-posture brain LAN-exposes MULTIPLE gateway surfaces. The rule set is DERIVED from
# brain_etc/brain.env — the ONE control panel (posture source of truth) — never a static list.
# The exposure model:
#   * LAN exposure happens ONLY when BRAIN_POSTURE=server AND EXTERNAL_GATEWAY_ENABLE=on AND the
#     single GW_BIND_ADDRESS is 0.0.0.0 (the host NIC). Workstation posture / loopback bind = no
#     rules (host-only).
#   * Each built-in backend opts in with <SVC>_PUBLISH_TO_LAN=YES (chroma→CHROMA_PORT,
#     ollama→OLLAMA_PORT), and must be enabled (<SVC>_ENABLE=on).
#   * Action surfaces are PER-NEURON (the YAML neuron zone): every action neuron with
#     publish_to_lan: yes opens each of its publish_to_lan_ports.
# This mirrors gateway_config's published-surface enumeration so the firewall never drifts.
LEGACY_RULE = "brain-{brain}-gateway"       # the old single chroma-only rule; migrated away on sight


def fw_rule_name(brain, surface):
    return f"brain-{brain}-gw-{surface}"


def _is_yes(v):  return str(v).strip().lower() in ("yes", "on", "true", "1")
def _is_on(v):   return str(v).strip().lower() == "on"


def _lan_eligible(env):
    """True when the brain's posture permits ANY LAN exposure: server posture, host publishing
    enabled, and the single published-listener bind is the host NIC."""
    return (env.get("BRAIN_POSTURE", "server").strip().lower() == "server"
            and _is_on(env.get("EXTERNAL_GATEWAY_ENABLE", "on"))
            and env.get("GW_BIND_ADDRESS", "0.0.0.0").strip() == "0.0.0.0")


def exposed_surfaces(bd=BRAIN_DIR):
    """Enumerate the LAN-exposed gateway surfaces from brain.env → [(surface, port)].
    Nothing is exposed unless the posture is LAN-eligible (see _lan_eligible). Then each built-in
    backend opts in via <SVC>_PUBLISH_TO_LAN=YES, and each action neuron via publish_to_lan +
    publish_to_lan_ports (read from the YAML neuron zone). Reuses the canonical brain.env parser +
    the neuron loader so the firewall stays in lock-step with the generator."""
    from gateway_config import read_env, brain_env_path      # canonical flat-zone parser (reuse)
    import brain_env as be                                    # the two-zone loader (neuron zone)
    envp = brain_env_path(bd)
    env = read_env(envp)
    out = []
    if not _lan_eligible(env):
        return out
    # built-in backends (flat zone)
    if _is_on(env.get("CHROMA_ENABLE", "on")) and _is_yes(env.get("CHROMA_PUBLISH_TO_LAN", "NO")):
        out.append(("chroma", env.get("CHROMA_PORT", "8000").strip()))
    if _is_on(env.get("OLLAMA_ENABLE", "on")) and _is_yes(env.get("OLLAMA_PUBLISH_TO_LAN", "NO")):
        out.append(("ollama", env.get("OLLAMA_PORT", "11434").strip()))
    # action neurons (YAML neuron zone): per-neuron publish + declared ports
    try:
        neurons = be.load_neurons(envp)
    except be.BrainEnvError as e:
        info(f"[WARN] neuron zone unreadable, no action firewall rules: {e}")
        neurons = {"neuron_bundles": []}
    for _bundle, n in be.iter_neurons(neurons, "action"):
        if not _is_yes(n.get("publish_to_lan", "no")):
            continue
        for p in (n.get("publish_to_lan_ports") or []):
            out.append((f"action-{n.get('name')}-{p}", str(p)))
    # keep only plain numeric ports
    clean = []
    for surface, port in out:
        if str(port).isdigit():
            clean.append((surface, str(port)))
        else:
            info(f"[WARN] {surface}: port '{port}' is not numeric — skipping firewall rule.")
    return clean


def _existing_gw_rules(brain):
    """The names of this brain's currently-installed gateway inbound rules (brain-<brain>-gw-*)."""
    rc, out, _ = ps(f"Get-NetFirewallRule -Name 'brain-{brain}-gw-*' -ErrorAction SilentlyContinue | "
                    f"Select-Object -ExpandProperty Name")
    return [ln.strip() for ln in out.splitlines() if ln.strip()] if rc == 0 else []


def _fw_set_rule(name, port):
    """Create (or re-point + enable) a subnet-scoped inbound TCP allow rule."""
    rc, _, err = ps(
        f"$n='{name}'; if (Get-NetFirewallRule -Name $n -ErrorAction SilentlyContinue) {{ "
        f"Set-NetFirewallRule -Name $n -LocalPort {port} -Enabled True }} else {{ "
        f"New-NetFirewallRule -Name $n -DisplayName $n -Direction Inbound -Action Allow "
        f"-Protocol TCP -LocalPort {port} -Profile Private,Domain -RemoteAddress LocalSubnet | Out-Null }}")
    if rc != 0:
        die(f"firewall rule apply failed for '{name}': {err}")


def _fw_del_rule(name):
    """Remove a rule if it exists; return True iff one was actually removed."""
    rc, out, err = ps(
        f"$n='{name}'; if (Get-NetFirewallRule -Name $n -ErrorAction SilentlyContinue) {{ "
        f"Remove-NetFirewallRule -Name $n; 'removed' }}")
    if rc != 0:
        info(f"[WARN] could not remove firewall rule '{name}': {err}")
        return False
    return out.strip() == "removed"


def firewall_apply(brain, bd=BRAIN_DIR):
    """Reconcile this brain's Windows Defender inbound rules to the gateway surfaces that
    brain.env actually LAN-exposes (GAP A). Adds a subnet-scoped per-surface rule for every
    exposed surface and removes the rule for every surface that is loopback-only / not exposed.
    The legacy single `brain-<brain>-gateway` rule is retired on sight. brain.env is the source
    of truth: a surface's *_BIND here (not this tool's --bind, which only sets chroma's distro
    .env) governs whether its port is opened."""
    from gateway_config import brain_env_path                # reuse the path helper
    envp = brain_env_path(bd)
    if not envp.is_file():
        info(f"[WARN] {envp} not found — leaving firewall rules unchanged "
             f"(cannot derive the exposed gateway surfaces).")
        return
    desired = {fw_rule_name(brain, s): p for s, p in exposed_surfaces(bd)}   # rule name -> port
    applied, removed = [], []
    for name, port in desired.items():
        _fw_set_rule(name, port)
        applied.append(f"{name.split('-gw-', 1)[-1]}:{port}")
    # remove any of THIS brain's gw rules that are no longer desired (surfaces turned off / ports changed)
    for name in _existing_gw_rules(brain):
        if name not in desired and _fw_del_rule(name):
            removed.append(name.split("-gw-", 1)[-1])
    _fw_del_rule(LEGACY_RULE.format(brain=brain))            # migrate off the old chroma-only rule
    if applied:
        info(f"firewall: inbound TCP allowed (subnet-scoped, Private/Domain) → {', '.join(applied)}")
    else:
        info("firewall: no LAN-exposed gateway surfaces in brain.env — no inbound rules opened.")
    if removed:
        info(f"firewall: removed rules for non-exposed surfaces → {', '.join(removed)}")


def firewall_release(brain):
    """Teardown: remove EVERY gateway rule for this brain (brain-<brain>-gw-*) plus the legacy rule."""
    removed = []
    for name in _existing_gw_rules(brain):
        if _fw_del_rule(name):
            removed.append(name.split("-gw-", 1)[-1])
    if _fw_del_rule(LEGACY_RULE.format(brain=brain)):
        removed.append("gateway(legacy)")
    info(f"firewall: released rules → {', '.join(removed)}" if removed
         else f"firewall: no rules to release for {brain}.")


# ---- commands -----------------------------------------------------------------------
def cmd_set(args):
    brain = brain_name(args)
    bind = BIND_ALIASES.get(args.bind, "127.0.0.1")
    reg = load_registry()
    assert_port_free(brain, args.port, reg, force=args.force)

    apply_in_distro(brain, args.port, bind)          # brain identity (run_as_brain)
    firewall_apply(brain)                             # host-admin: reconcile fw rules to brain.env

    reg[brain] = {"port": args.port, "bind": bind,
                  "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    save_registry(reg)

    print(f"\n  OK: {brain} gateway -> {bind}:{args.port} (TLS).")
    print(f"  verify (as the brain): curl --cacert ~/gateway/gateway_out/cert.pem https://127.0.0.1:{args.port}/api/v2/heartbeat")
    if bind == "0.0.0.0":
        print("  NOTE (Server): the cert SAN covers only localhost/127.0.0.1. For off-box clients to")
        print("  validate, re-run gen-cert.sh with a LAN SAN, e.g.:")
        print(f"    run_as_brain.py --brain {brain} --wsl -- bash -lc 'cd ~/docker && ./gen-cert.sh DNS:<host>.lan IP:<lan-ip>'")


def cmd_show(args):
    reg = load_registry()
    print(f"\nGateway port registry ({registry_path()}):")
    if not reg:
        print("  (empty)")
    for b, row in sorted(reg.items()):
        live = port_listener(row.get("port"))
        mark = "  [listening]" if live else "  [not up]"
        print(f"  {b:<22} {row.get('bind')}:{row.get('port')}{mark}  updated={row.get('updated','?')}")
    print()


def cmd_release(args):
    brain = brain_name(args)
    firewall_release(brain)                           # teardown: remove ALL of this brain's gw rules
    reg = load_registry()
    if reg.pop(brain, None) is not None:
        save_registry(reg)
        info(f"released registry row for {brain}")
    else:
        info(f"no registry row for {brain}")


def main():
    ap = argparse.ArgumentParser(description="Assign a brain's gateway host port + firewall (elevated).")
    ap.add_argument("--brain", help="brain name (default: from .brain_provision.json or folder)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("set", help="set the gateway host port + bind (+ firewall on Server)")
    s.add_argument("--port", type=int, required=True)
    s.add_argument("--bind", choices=tuple(BIND_ALIASES), default="personal",
                   help="personal|127.0.0.1 (loopback, default) or server|0.0.0.0 (host NIC + fw rule)")
    s.add_argument("--force", action="store_true",
                   help="skip the live-listener check (use only when the listener is THIS "
                        "brain's own gateway already on the port); never bypasses another brain's reservation")

    sub.add_parser("show", help="list brain→port assignments + whether each is listening")
    sub.add_parser("release", help="remove this brain's firewall rule + registry row")

    args = ap.parse_args()
    require_admin()
    {"set": cmd_set, "show": cmd_show, "release": cmd_release}[args.cmd](args)


if __name__ == "__main__":
    main()
