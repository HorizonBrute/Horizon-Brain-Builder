#!/usr/bin/env python3
"""
log_seam.py - install the centralized log seam (ADR-0018): a knob-driven logrotate
config + a systemd --user timer that rotates every source into one flat per-brain
log root.
=============================================================================
Runs IN THE DISTRO as the brain (rootless docker + the brain's own `systemctl
--user`; linger keeps the timer alive across reboots). The twin of
neuron_schedule.py - it invents nothing, reading its whole posture from the synced
config seam (~/docker/.env, from brain.env):

  * master switch  BRAIN_ENABLE_LOGGING   -> `off` removes the timer + config, exits.
  * BRAIN_LOG_SOURCES          which of gateway/wsl/chroma/ollama to capture.
  * BRAIN_LOG_ROTATE_WHEN      daily | size | daily+size.
  * BRAIN_LOG_ROTATE_SIZE      size threshold (e.g. 100M).
  * BRAIN_LOG_RETENTION_DAYS   prune rotated files older than N days (0 = keep).
  * BRAIN_LOG_DATESTAMP        logrotate dateformat body (e.g. %Y%m%d).
  * BRAIN_LOG_WSL_FILTER       extra journalctl selector for the wsl source (empty=all).

One flat log root IS the seam:  ~/logs/<source-file>-<DATESTAMP>[.gz]
  gateway   the nginx access/error/inspect files (USR1 reopen, no truncate).
  chroma    logrotate on the Docker json-file path (copytruncate); a lastaction
  ollama    renames the id-based dated file to <source>-<date>.log (ADR-0018 §Resolved-1).
  wsl       a cursor-checkpointed journald export to ~/logs/wsl.log, then rotated.

logrotate is the rotation engine (ADR-0018); we accept its NATIVE dated naming.
Rotation runs from a systemd --user timer (hourly when size is a trigger, else
daily); residency re-runs `log_seam.py` every boot so a knob edit takes effect.

    python3 log_seam.py [--dry-run]   # --dry-run: print config + units, install nothing
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
DOCKER_DIR = HOME / "docker"
ENV_FILE = DOCKER_DIR / ".env"
LOG_ROOT = HOME / "logs"
GW_LOG_DIR = LOG_ROOT / "gateway"                       # nginx bind-mount target (/var/log/nginx)
WSL_LOG = LOG_ROOT / "wsl.log"                          # the journald export sink
STATE_DIR = HOME / ".local" / "state" / "brain-logrotate"
STATE = STATE_DIR / "logrotate.state"
WSL_CURSOR = STATE_DIR / "wsl.cursor"
CONF_DIR = HOME / ".config" / "brain-logrotate"
CONF = CONF_DIR / "brain.conf"
UNIT_DIR = HOME / ".config" / "systemd" / "user"
SERVICE = "brain-logrotate.service"
TIMER = "brain-logrotate.timer"
ALL_SOURCES = ("gateway", "wsl", "chroma", "ollama")


def info(m: str) -> None:
    print(f"[log-seam] {m}")


def die(m: str) -> "None":
    print(f"[log-seam] ERROR: {m}", file=sys.stderr)
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


def sources(env: dict) -> list:
    raw = env.get("BRAIN_LOG_SOURCES", ",".join(ALL_SOURCES))
    picked = [s.strip() for s in raw.split(",") if s.strip()]
    unknown = [s for s in picked if s not in ALL_SOURCES]
    if unknown:
        info(f"NOTE: ignoring unknown BRAIN_LOG_SOURCES entries: {', '.join(unknown)}")
    return [s for s in ALL_SOURCES if s in picked]     # canonical order


# --------------------------------------------------------------------------- #
# docker helpers (resolve a compose service -> container id + its json LogPath)
# --------------------------------------------------------------------------- #
def _docker(*args: str) -> str:
    r = subprocess.run(["docker", *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def container_for(service: str) -> str:
    """The running container id for a compose service (label-based, name-agnostic)."""
    return _docker("ps", "-q", "--filter", f"label=com.docker.compose.service={service}")


def json_log_path(service: str) -> str | None:
    cid = container_for(service)
    if not cid:
        return None
    p = _docker("inspect", "--format", "{{.LogPath}}", cid)
    return p or None


# --------------------------------------------------------------------------- #
# logrotate config rendering
# --------------------------------------------------------------------------- #
def _when_lines(env: dict) -> list:
    when = env.get("BRAIN_LOG_ROTATE_WHEN", "daily+size").lower()
    size = env.get("BRAIN_LOG_ROTATE_SIZE", "100M")
    out = []
    if "daily" in when:
        out.append("    daily")
    if "size" in when:
        # maxsize = "rotate at the period OR when big"; size = "purely on size".
        out.append(f"    maxsize {size}" if "daily" in when else f"    size {size}")
    if not out:                                        # neither -> sane default
        out.append("    daily")
    return out


def _retention_lines(env: dict) -> list:
    days = env.get("BRAIN_LOG_RETENTION_DAYS", "30").strip()
    out = ["    rotate 10000"]                          # count is not the bound; age is
    if days and days != "0":
        out.append(f"    maxage {days}")
    return out


def _common(env: dict) -> list:
    date_body = env.get("BRAIN_LOG_DATESTAMP", "%Y%m%d")
    return [
        "    missingok",
        "    notifempty",
        "    dateext",
        f"    dateformat -{date_body}",
        f"    olddir {LOG_ROOT}",                       # rotated files land in the FLAT seam root
        "    compress",
        "    delaycompress",
        "    nomail",
        *_when_lines(env),
        *_retention_lines(env),
    ]


def _stanza(paths: str, body: list) -> str:
    return paths + " {\n" + "\n".join(body) + "\n}\n"


def render_conf(env: dict, srcs: list) -> str:
    blocks = [
        "# GENERATED by log_seam.py - do not hand-edit (a re-run overwrites).",
        f"# Seam root: {LOG_ROOT}  (ADR-0018)",
        "",
    ]
    common = _common(env)

    if "gateway" in srcs:
        # nginx keeps its file open by name; USR1 makes it reopen the fresh file.
        reopen = ("        cid=$(docker ps -q --filter "
                  "label=com.docker.compose.service=gateway); "
                  "[ -n \"$cid\" ] && docker kill -s USR1 \"$cid\" >/dev/null 2>&1 || true")
        blocks.append(_stanza(f"{GW_LOG_DIR}/*.log",
                              common + ["    sharedscripts",
                                        "    postrotate", reopen, "    endscript"]))

    if "wsl" in srcs:
        # The journald export sink (populated by the service ExecStartPre). copytruncate
        # so a concurrent append does not lose lines to the rename.
        blocks.append(_stanza(str(WSL_LOG), common + ["    copytruncate"]))

    for svc in ("chroma", "ollama"):
        if svc not in srcs:
            continue
        lp = json_log_path(svc)
        if not lp:
            blocks.append(f"# {svc}: container not running at render time - "
                          f"stanza deferred (residency re-runs this on boot).")
            continue
        base = Path(lp).name                            # <id>-json.log
        date_body = env.get("BRAIN_LOG_DATESTAMP", "%Y%m%d")
        # After rotation the dated file is olddir/<base>-<date>[.gz]; rename it to the
        # source name (ADR-0018 §Resolved-1: id->service mapping via docker inspect).
        rename = (f"        for f in {LOG_ROOT}/{base}-*; do [ -e \"$f\" ] || continue; "
                  f"mv -f \"$f\" \"{LOG_ROOT}/{svc}-$(echo \"$f\" | sed 's/.*{base}-//').log\" "
                  f"2>/dev/null || true; done")
        blocks.append(_stanza(lp, common + ["    copytruncate",
                                            "    lastaction", rename, "    endscript"]))
    return "\n".join(blocks) + "\n"


# --------------------------------------------------------------------------- #
# systemd --user units
# --------------------------------------------------------------------------- #
def service_unit(env: dict, srcs: list) -> str:
    pre = ""
    if "wsl" in srcs:
        flt = env.get("BRAIN_LOG_WSL_FILTER", "").strip()
        # Cursor-checkpointed export: only new entries since last run, native journal JSON.
        exp = (f"journalctl --cursor-file={WSL_CURSOR} -o json {flt} >> {WSL_LOG} 2>/dev/null || true")
        pre = f"ExecStartPre=/bin/bash -lc '{exp}'\n"
    return (
        "# GENERATED by log_seam.py - do not hand-edit (a re-run overwrites).\n"
        "[Unit]\n"
        "Description=Brain log seam - export + rotate all sources into ~/logs (ADR-0018)\n"
        "After=docker.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"{pre}"
        f"ExecStart=/usr/sbin/logrotate -s {STATE} {CONF}\n"
    )


def timer_unit(env: dict) -> str:
    when = env.get("BRAIN_LOG_ROTATE_WHEN", "daily+size").lower()
    # Size-triggered rotation only happens when logrotate RUNS, so poll hourly when size
    # is a trigger; otherwise a single daily pass at 00:10 is enough.
    oncal = "*-*-* *:00:00" if "size" in when else "*-*-* 00:10:00"
    return (
        "# GENERATED by log_seam.py - do not hand-edit (a re-run overwrites).\n"
        "[Unit]\n"
        "Description=Brain log seam rotation timer (ADR-0018)\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar={oncal}\n"
        "Persistent=true\n"
        "RandomizedDelaySec=30\n"
        f"Unit={SERVICE}\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def sctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def remove_all() -> None:
    sctl("disable", "--now", TIMER)
    (UNIT_DIR / TIMER).unlink(missing_ok=True)
    (UNIT_DIR / SERVICE).unlink(missing_ok=True)
    CONF.unlink(missing_ok=True)
    sctl("daemon-reload")


def main(argv=None) -> int:
    dry = "--dry-run" in (argv or sys.argv[1:])
    env = read_env(ENV_FILE)
    enabled = env.get("BRAIN_ENABLE_LOGGING", "off").lower() == "on"

    if not enabled:
        info("BRAIN_ENABLE_LOGGING is off - removing the log-seam timer + config.")
        if not dry:
            remove_all()
        return 0

    srcs = sources(env)
    if not srcs:
        info("BRAIN_LOG_SOURCES is empty - nothing to capture; removing the timer.")
        if not dry:
            remove_all()
        return 0

    for d in (LOG_ROOT, GW_LOG_DIR, STATE_DIR, CONF_DIR, UNIT_DIR):
        d.mkdir(parents=True, exist_ok=True)
    conf = render_conf(env, srcs)
    svc = service_unit(env, srcs)
    tmr = timer_unit(env)

    if dry:
        info(f"[dry-run] sources: {', '.join(srcs)}")
        info(f"[dry-run] {CONF}:\n{conf}")
        info(f"[dry-run] {SERVICE}:\n{svc}")
        info(f"[dry-run] {TIMER}:\n{tmr}")
        return 0

    CONF.write_text(conf, encoding="utf-8")
    (UNIT_DIR / SERVICE).write_text(svc, encoding="utf-8")
    (UNIT_DIR / TIMER).write_text(tmr, encoding="utf-8")
    sctl("daemon-reload")
    r = sctl("enable", "--now", TIMER)
    if r.returncode != 0:
        die(f"failed to enable {TIMER}: {(r.stderr or r.stdout).strip()}")
    info(f"installed log seam for: {', '.join(srcs)}")
    print(sctl("list-timers", "--all", TIMER).stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
