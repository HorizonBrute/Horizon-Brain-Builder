#!/usr/bin/env python3
"""
neuron_schedule.py - install per-tag systemd --user timers for neuron ingest.
=============================================================================
Runs IN THE DISTRO as the brain (rootless docker under the brain's own `systemctl
--user`; linger keeps the timers alive across reboots without a login session).

It reads the schedule posture entirely from the SYNCED config seam - it invents
nothing. Everything comes from ~/docker/.env (rendered from brain.env by
brain_env.render_dotenv; config-flow Phase 5 retired ~/docker/neuron/sources.yaml
as the schedule source):
  * master switch  NEURON_SCHEDULE_ENABLE   (`off` removes every neuron-ingest timer + exits).
  * the tag->cron map   NEURON_SCHEDULE__<tag>=<cron>   (one per ACTIVE schedule - a cadence
                   >=1 input neuron of the DEFAULT bundle uses; brain_env.active_schedules).
  * the timer target    DEFAULT_INPUT_NEURON   (the default bundle's first input neuron =
                   the compose service name; replaces the stale `<bundle>_input_1` guess).

For every ACTIVE tag it installs a timer `neuron-ingest@<tag>.timer` (OnCalendar derived
from the tag's cron) that activates the template unit `neuron-ingest@.service`, which runs
the SAFE compose command (exposure overlays layered + `--no-deps`, so the gateway's
published ports are never stripped) with `--ingest-only --tags <tag>`.

Idempotent: re-run after ANY schedule/tag/enable change. Stale neuron-ingest timers
(tags no longer active) are disabled and removed. Every generated OnCalendar is
validated with `systemd-analyze calendar` before install - a bad cron aborts the run
rather than installing a mis-firing timer.

    python3 neuron_schedule.py [--dry-run]   # --dry-run: print units, install nothing
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
DOCKER_DIR = HOME / "docker"
ENV_FILE = DOCKER_DIR / ".env"
SCHEDULE_PREFIX = "NEURON_SCHEDULE__"             # NEURON_SCHEDULE__<tag>=<cron> in ~/docker/.env
UNIT_DIR = HOME / ".config" / "systemd" / "user"
SERVICE_UNIT = "neuron-ingest@.service"          # the one template all timers share
TIMER_PREFIX = "neuron-ingest@"                  # neuron-ingest@<tag>.timer

# cron day-of-week (0/7=Sun) -> systemd calendar day name.
_DOW = {"0": "Sun", "7": "Sun", "1": "Mon", "2": "Tue",
        "3": "Wed", "4": "Thu", "5": "Fri", "6": "Sat"}


def info(m: str) -> None:
    print(f"[neuron-schedule] {m}")


def die(m: str) -> "None":
    print(f"[neuron-schedule] ERROR: {m}", file=sys.stderr)
    raise SystemExit(1)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def read_env(path: Path) -> dict:
    env = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def compose_files(env: dict) -> list:
    """base + the exposure overlays for the two-zone model: layered when the gateway publishes
    (EXTERNAL_GATEWAY_ENABLE=on) and the surface is enabled (CHROMA_ENABLE / OLLAMA_ENABLE), plus
    the action overlay whenever the gateway publishes. Mirrors reapply_brain_configs.compose_files
    / the boot keepalive."""
    files = ["compose.yaml"]
    if env.get("EXTERNAL_GATEWAY_ENABLE", "on").lower() != "on":
        return files
    if env.get("CHROMA_ENABLE", "on").lower() == "on":
        files.append("compose.chroma-gateway.yaml")
    if env.get("OLLAMA_ENABLE", "on").lower() == "on":
        files.append("compose.ollama-gateway.yaml")
    files.append("compose.action-neuron-gateway.yaml")
    return files


# --------------------------------------------------------------------------- #
# cron (5-field) -> systemd OnCalendar
# --------------------------------------------------------------------------- #
def _num_part(tok: str, pad: bool) -> str:
    if tok == "*":
        return "*"
    if tok.startswith("*/"):            # cron step -> systemd 0/step
        return "0/" + tok[2:]
    if pad and tok.isdigit():           # zero-pad plain HH/MM
        return f"{int(tok):02d}"
    return tok                          # ranges/steps pass through, validated below


def _join(field: str, pad: bool = False) -> str:
    return ",".join(_num_part(t, pad) for t in field.split(","))


def cron_to_oncalendar(cron: str) -> str:
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError(f"cron must have 5 fields (min hour dom mon dow): {cron!r}")
    minute, hour, dom, mon, dow = parts
    dow_str = ""
    if dow != "*":
        days = []
        for d in dow.split(","):
            if "-" in d:
                a, b = d.split("-", 1)
                days.append(f"{_DOW.get(a, a)}..{_DOW.get(b, b)}")
            else:
                days.append(_DOW.get(d, d))
        dow_str = ",".join(days) + " "
    date = f"*-{_join(mon)}-{_join(dom)}"
    clock = f"{_join(hour, pad=True)}:{_join(minute, pad=True)}:00"
    return f"{dow_str}{date} {clock}"


def validate_calendar(oncal: str) -> None:
    r = subprocess.run(["systemd-analyze", "calendar", oncal],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"invalid OnCalendar {oncal!r}: {(r.stderr or r.stdout).strip()}")


# --------------------------------------------------------------------------- #
# Active cadence tags - sourced from ~/docker/.env (rendered from the brain.env zone).
# brain_env.active_schedules already narrowed to schedules >=1 input neuron USES, so every
# NEURON_SCHEDULE__<tag> here is active by construction (no separate "unscheduled" set).
# --------------------------------------------------------------------------- #
def load_active(env: dict):
    active = {}
    for k, v in env.items():
        if k.startswith(SCHEDULE_PREFIX) and v.strip():
            active[k[len(SCHEDULE_PREFIX):]] = v.strip()
    return active


# --------------------------------------------------------------------------- #
# Unit rendering
# --------------------------------------------------------------------------- #
def service_unit(env: dict) -> str:
    fflags = " ".join(f"-f {f}" for f in compose_files(env))
    # The SAFE ingest command: overlays layered + --no-deps (never strips gateway ports).
    # %i is the systemd instance = the cadence tag; %h = the brain home. Runs the DEFAULT
    # bundle's first INPUT neuron, whose compose SERVICE name == the neuron name (rendered
    # into DEFAULT_INPUT_NEURON by brain_env from the brain.env zone; config-flow Phase 5).
    # Multi-bundle timers (per bundle+tag) remain a deferred follow-up.
    input_neuron = env.get("DEFAULT_INPUT_NEURON", "").strip()
    if not input_neuron:
        die("DEFAULT_INPUT_NEURON is unset in ~/docker/.env (the brain.env zone declares no input "
            "neuron in the default bundle, or the render is stale) - cannot target the ingest timer.")
    cmd = (f"cd %h/docker && exec docker compose {fflags} "
           f"--profile neurons --profile gateway --profile ollama --profile fail2ban "
           f"run --rm --no-deps {input_neuron} --ingest-only --tags %i")
    return (
        "# GENERATED by neuron_schedule.py - do not hand-edit (a re-run overwrites).\n"
        "[Unit]\n"
        "Description=Neuron ingest for cadence tag '%i' (safe compose: overlays + --no-deps)\n"
        "After=docker.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "TimeoutStartSec=1800\n"
        f"ExecStart=/bin/bash -lc '{cmd}'\n"
    )


def timer_unit(tag: str, oncal: str) -> str:
    return (
        "# GENERATED by neuron_schedule.py - do not hand-edit (a re-run overwrites).\n"
        "[Unit]\n"
        f"Description=Neuron ingest timer for cadence tag '{tag}'\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar={oncal}\n"
        "Persistent=true\n"                 # catch up a run missed while the box was off
        "RandomizedDelaySec=30\n"
        f"Unit={TIMER_PREFIX}{tag}.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


# --------------------------------------------------------------------------- #
# systemctl --user helpers
# --------------------------------------------------------------------------- #
def sctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def existing_timers() -> list:
    out = []
    for p in UNIT_DIR.glob(f"{TIMER_PREFIX}*.timer"):
        tag = p.name[len(TIMER_PREFIX):-len(".timer")]
        out.append((tag, p))
    return out


def remove_timer(tag: str, path: Path) -> None:
    sctl("disable", "--now", f"{TIMER_PREFIX}{tag}.timer")
    path.unlink(missing_ok=True)
    info(f"removed stale timer '{tag}'")


def main(argv=None) -> int:
    dry = "--dry-run" in (argv or sys.argv[1:])
    env = read_env(ENV_FILE)
    enabled = env.get("NEURON_SCHEDULE_ENABLE", "off").lower() == "on"
    UNIT_DIR.mkdir(parents=True, exist_ok=True)

    if not enabled:
        info("NEURON_SCHEDULE_ENABLE is off - removing any neuron-ingest timers.")
        for tag, path in existing_timers():
            if dry:
                info(f"[dry-run] would remove timer '{tag}'")
            else:
                remove_timer(tag, path)
        (UNIT_DIR / SERVICE_UNIT).unlink(missing_ok=True) if not dry else None
        if not dry:
            sctl("daemon-reload")
        info("done (scheduling disabled).")
        return 0

    active = load_active(env)
    if not active:
        info("no active cadence tags (no input neuron carries a scheduled tag) - nothing to install.")
        for tag, path in existing_timers():
            remove_timer(tag, path) if not dry else info(f"[dry-run] would remove '{tag}'")
        if not dry:
            sctl("daemon-reload")
        return 0

    # Resolve + validate every schedule BEFORE touching the unit dir.
    resolved = {}
    for tag, cron in active.items():
        oncal = cron_to_oncalendar(cron)
        validate_calendar(oncal)
        resolved[tag] = oncal

    svc = service_unit(env)
    if dry:
        info(f"[dry-run] {SERVICE_UNIT}:\n{svc}")
        for tag, oncal in resolved.items():
            info(f"[dry-run] {TIMER_PREFIX}{tag}.timer  (cron {active[tag]} -> OnCalendar {oncal}):\n"
                 f"{timer_unit(tag, oncal)}")
        info(f"[dry-run] would enable {len(resolved)} timer(s): {', '.join(resolved)}")
        return 0

    (UNIT_DIR / SERVICE_UNIT).write_text(svc, encoding="utf-8")
    for tag, oncal in resolved.items():
        (UNIT_DIR / f"{TIMER_PREFIX}{tag}.timer").write_text(timer_unit(tag, oncal), encoding="utf-8")
        info(f"wrote timer '{tag}' (cron {active[tag]} -> OnCalendar {oncal})")

    # Drop timers whose tag is no longer active.
    for tag, path in existing_timers():
        if tag not in resolved:
            remove_timer(tag, path)

    sctl("daemon-reload")
    for tag in resolved:
        r = sctl("enable", "--now", f"{TIMER_PREFIX}{tag}.timer")
        if r.returncode != 0:
            die(f"failed to enable timer '{tag}': {(r.stderr or r.stdout).strip()}")
    info(f"installed + enabled {len(resolved)} timer(s): {', '.join(resolved)}")
    print(sctl("list-timers", "--all", f"{TIMER_PREFIX}*").stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
