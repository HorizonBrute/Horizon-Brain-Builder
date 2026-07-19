#!/usr/bin/env python3
"""
residency.py - shared per-brain BOOT residency task (Task Scheduler).

WHY THIS IS ITS OWN MODULE
--------------------------
A freshly-imported WSL2 distro is idle-shutdown by the host when nothing holds it,
so the brain's Chroma TLS gateway is only intermittently reachable between calls.
The fix is a per-brain BootTrigger task that holds the distro resident and brings the
stack up at every boot.

Creating that task is an *admin* concern, NOT a brain-phase one:
  - a BootTrigger "run whether logged on or not" task requires elevation to create; the
    brain account (phase 2) is NOT elevated, so it can never self-register one; and
  - registering it to run as the brain wants a stored credential (Password logon). S4U
    (no password) additionally needs SeTcbPrivilege, which a plain admin does NOT hold
    (`schtasks` returns "Access is denied"), so **Password logon is the default**.

So `brain_installer_1_admin.py` (elevated, and the only place that holds the brain
password) owns registration; this module is the shared implementation it calls. Phase 2
does NOT register residency.
"""
import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

# The boot keepalive, shipped INTO the distro as a FILE (see write_keepalive) and run by the
# task as `bash -l <path>` — deliberately NOT an inline `wsl.exe -- bash -lc "…"` action.
# WHY A FILE: the raw wsl.exe round-trip mangles any quoting the action carries — the
# Task-Scheduler <Arguments> string is re-split by wsl.exe AND again by bash, so a nested
# `bash -lc "…$(seq 1 30)…"` was double-wrapped, its `$( )` spliced newlines, `for` split on
# them, and bash died `syntax error near '2'` (exit 2) BEFORE it ever reached the sleep — the
# distro then idle-shut and the gateway vanished. A script file has no such round-trip.
# WHY NO LOOP: `restart: unless-stopped` on the compose services already auto-starts both
# containers at boot, so the keepalive only has to nudge the stack up once (in case it isn't)
# and then `exec sleep infinity` to hold the WSL VM resident (WSL idle-shuts a distro when
# nothing holds it open). The old `for i in $(seq 1 30)` retry loop was both redundant and the
# exact fragile part; it is gone.
# NOTE: keep this content pure ASCII — it is piped through a Windows->WSL stdin bridge into a
# boot-critical file, so no smart-quotes/em-dashes (encoding surprises have no place here).
KEEPALIVE_SCRIPT = """\
#!/usr/bin/env bash
# Brain boot keepalive - hold the WSL distro resident and ensure the stack is up.
# Managed by residency.py (shipped in by deploy phase 2); do not edit by hand - a
# redeploy overwrites it. The boot task runs this via `bash -l <this file>`.
# Re-assert host-authored config (brain truths) BEFORE starting the stack, so the
# host source of truth wins on every boot (and any brain tampering of the ext4
# working copy is overwritten). The apply primitive rides in on the RO mount itself.
# Non-fatal if the mount is not up yet (run with bash - drvfs may not carry +x).
# CANONICAL apply-primitive path: the wsl_in_distro_scripts seam mount (matches
# reapply_brain_configs.py, gateway_tokens.py, brain_truths.APPLY_REMOTE). The old
# /opt/apply_brain_truths.sh location is retired - do not reintroduce it.
bash /opt/brain_wsl_in_distro_scripts/apply_brain_truths.sh || echo "keepalive: brain-truths apply skipped (not installed yet)"
cd "$HOME/docker" || exit 0
# Layer the exposure overlays whose *_EXPOSE knob is on (mirrors
# reapply_brain_configs.compose_files). A base-only `docker compose up -d` recreates
# the gateway from base - which publishes NO ports - stripping 8000/11434/8443 on
# EVERY boot. THIS was the recurring port-loss loop. The DEFAULT is now carried by
# COMPOSE_FILE in .env (so every compose invocation layers the overlays); this explicit
# -f layering is boot-time defense-in-depth in case .env is stale/absent.
knob() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]'; }
gw=$(knob EXTERNAL_GATEWAY_ENABLE); [ -z "$gw" ] && gw=on
ce=$(knob CHROMA_ENABLE); [ -z "$ce" ] && ce=on
oe=$(knob OLLAMA_ENABLE); [ -z "$oe" ] && oe=on
FILES="-f compose.yaml"
if [ "$gw" = "on" ]; then
  [ "$ce" = "on" ] && FILES="$FILES -f compose.chroma-gateway.yaml"
  [ "$oe" = "on" ] && FILES="$FILES -f compose.ollama-gateway.yaml"
  FILES="$FILES -f compose.action-neuron-gateway.yaml"
fi
docker compose $FILES up -d
# Reconcile the per-tag ingest schedule timers from the (freshly-synced) config, so a
# schedule/tag edit on the host takes effect on boot (host source of truth wins). Reads
# the runtime ~/docker config; harmless when NEURON_SCHEDULE_ENABLE=off (it removes the
# timers). Lives in the live-mounted in-distro seam. Non-fatal.
python3 /opt/brain_wsl_in_distro_scripts/neuron_schedule.py || echo "keepalive: neuron schedule reconcile skipped"
# Reconcile the centralized log seam (ADR-0018) from the freshly-synced config: install
# (or, when BRAIN_ENABLE_LOGGING=off, remove) the knob-driven logrotate + systemd --user
# rotation timer. Idempotent; lives in the live-mounted in-distro seam. Non-fatal.
python3 /opt/brain_wsl_in_distro_scripts/log_seam.py || echo "keepalive: log seam reconcile skipped"
exec sleep infinity
"""


def task_name(brain):
    # <brain>-docker-keepalive: it keeps the docker stack (Chroma + gateway) resident.
    # (Renamed from the old <brain>-residency; a fresh deploy registers the new name.
    # An already-deployed brain keeps its old task until re-deployed — see DEPLOYMENT.md.)
    return f"{brain}-docker-keepalive"


def keepalive_path(brain):
    """Absolute in-distro path of the keepalive script. The brain's Linux user is the brain
    name (uid 1000, home /home/<brain>) by provisioning construction, so this matches what
    write_keepalive drops via `$HOME` and what the boot task's `bash -l <path>` executes."""
    return f"/home/{brain}/keepalive.sh"


def write_keepalive(distro, info=lambda m: None):
    """Ship KEEPALIVE_SCRIPT into <distro> as ~/keepalive.sh (owned by the distro's default
    user = the brain, uid 1000). MUST run in a context that can SEE the brain's distro — i.e.
    AS THE BRAIN (deploy phase 2), NOT an elevated admin session, whose per-user WSL namespace
    does not include the brain-owned distro. That namespace split is why the *script* is
    written here (phase 2) while the boot *task* is registered by installer_1 (admin).

    The script content goes via STDIN, and the `cat`/`chmod` command carries no quotes, `$()`,
    or parens — so nothing survives a shell/wsl.exe round-trip to be mangled (the whole point).
    Returns True on success.

    CRITICAL — ship the script as BYTES, not text, and strip CR at write time. On Windows,
    `subprocess.run(..., text=True)` wraps stdin in a TextIOWrapper that translates every `\n`
    to `\r\n`, so the file lands in the distro with CRLF line terminators. That is fatal for a
    boot-critical script: `exec sleep infinity\r` makes `sleep` reject the argument `infinity\r`
    ("invalid time interval") and, because it is `exec`, the whole `bash -l keepalive.sh` process
    exits 1 — the boot task then shows LastResult 1 and never holds the distro (the gateway idles
    away). Passing bytes (no text mode) sends the raw LF-only content untouched; the `tr -d '\r'`
    is defense-in-depth so this boot-critical file is CRLF-free regardless of any future caller."""
    p = subprocess.run(
        ["wsl", "-d", distro, "--", "bash", "-c",
         "tr -d '\\r' > $HOME/keepalive.sh && chmod +x $HOME/keepalive.sh"],
        input=KEEPALIVE_SCRIPT.encode("utf-8"), capture_output=True)
    if p.returncode == 0:
        info("keepalive.sh installed in distro (~/keepalive.sh)")
        return True
    err = (p.stderr or p.stdout).decode("utf-8", "replace").strip()
    info(f"[WARN] could not install keepalive.sh: {err}")
    return False


def xml_path(brain_dir):
    # system/wsl_engine — WSL runtime state (ADR-0019 amended). MUST match
    # windows_deploy_brain.WSL_RUNTIME_REL and both installers.
    return Path(brain_dir) / "system" / "wsl_engine" / "residency_task.xml"


def build_xml(brain, distro, logon_type):
    """Task Scheduler v1.2 XML: BootTrigger, run-as-brain, unlimited execution time
    (the keepalive runs forever), restart-on-failure. The action runs the keepalive SCRIPT
    FILE (`bash -l <path>`) — no inline shell, no quotes/`$()` for the round-trip to mangle
    (see KEEPALIVE_SCRIPT). `escape()` is a harmless no-op on the current argument string but
    kept so a future distro/path change can't inject raw XML."""
    arguments = f"-d {distro} -- bash -l {keepalive_path(brain)}"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Brain {brain}: hold the brain WSL distro resident and keep the Chroma TLS gateway up across reboots.</Description>
    <URI>\\{task_name(brain)}</URI>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled></BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{brain}</UserId>
      <LogonType>{logon_type}</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RestartOnFailure><Interval>PT1M</Interval><Count>5</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>wsl.exe</Command>
      <Arguments>{escape(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _manual_oneliner(brain, path):
    return (f'schtasks /create /tn "{task_name(brain)}" /xml "{path}" '
            f'/ru {brain} /rp <brain-password> /f')


def _logon_rights_helper():
    """OPTIONAL host LSA logon-rights helper, named by $BRAIN_LOGON_RIGHTS_HELPER (a path
    to a python module exposing holds(brain) -> bool and grant(brain) -> (ok, detail)).
    A host platform that provisions brain accounts usually ships one. None when unset or
    absent — the grant then falls back to a printed manual remedy. Named by env, never
    guessed: the factory carries no knowledge of any host's layout."""
    env = os.environ.get("BRAIN_LOGON_RIGHTS_HELPER")
    if env:
        cand = Path(env)
        return cand if cand.is_file() else None
    # No host helper wired: on Windows fall back to the bundled helper co-located here
    # (staged into every brain), so the factory self-grants SeBatchLogonRight rather than
    # registering a Password-logon boot task that can never launch (267011). Windows-only —
    # the Linux residency path uses systemd linger, not a batch-logon task, so it needs no
    # such grant and gets no fallback.
    if os.name == "nt":
        bundled = Path(__file__).with_name("logon_rights_helper.py")
        if bundled.is_file():
            return bundled
    return None


def _grant_batch_logon(brain, info):
    """Grant SeBatchLogonRight ("Log on as a batch job") to the brain — BASELINE for every
    brain, because residency is baseline. A Password-logon scheduled task's principal CANNOT
    launch without it: the task sits inert at LastResult 267011 (0x41303, has-not-run) forever
    with no error surfaced. This is exactly why sorcerypunk_dev's residency task was dead on
    arrival — the right used to be gated behind `create_brain --automation scheduled` (default
    `none`), so a normal deploy never granted it. Granting it HERE, co-located with the task
    that needs it, makes "created a batch-logon task but forgot the right" unrepresentable.

    Uses the host's LSA helper when one is configured (surgical + additive —
    LsaAddAccountRights, NOT a secedit policy reimport). Without a helper, warns + prints
    the remedy rather than silently registering a task that can never run. Returns True
    iff the right is held after."""
    helper = _logon_rights_helper()
    if helper is None:
        info("[WARN] no SeBatchLogonRight helper configured ($BRAIN_LOGON_RIGHTS_HELPER) — the "
             "boot task cannot launch until the brain holds 'Log on as a batch job'. Grant it "
             "manually (Local Security Policy -> Local Policies -> User Rights Assignment -> "
             "'Log on as a batch job' -> add the brain account):")
        info(f"    secpol.msc   (add '{brain}' to 'Log on as a batch job')")
        return False
    try:
        sys.path.insert(0, str(helper.parent))
        import importlib
        lr = importlib.import_module(helper.stem)
        if lr.holds(brain):
            info("SeBatchLogonRight already held (Log on as a batch job)")
            return True
        ok, detail = lr.grant(brain)
        if ok:
            info("SeBatchLogonRight granted (Log on as a batch job) — required for the "
                 "Password-logon boot task to launch")
        else:
            info(f"[WARN] SeBatchLogonRight grant failed: {detail} — the boot task may not launch")
        return ok
    except Exception as e:
        info(f"[WARN] SeBatchLogonRight grant errored: {e} — the boot task may not launch")
        return False


def register(brain, distro, password, brain_dir, info, cred_hint=None):
    """Register (+ start) the per-brain boot residency task. MUST be called from an
    ELEVATED context (installer_1). Uses Password logon when `password` is provided
    (the default, and the only mode a plain admin can register); falls back to S4U only
    when no password is available, and prints the elevated one-liner if creation fails.

    Grants SeBatchLogonRight to the brain first (baseline — the Password-logon task is inert
    without it). Presumes the keepalive script is already in the distro (deploy phase 2 ships
    it via write_keepalive; the elevated admin session here cannot reach the brain's distro).

    Returns True iff the task was created.
    """
    name = task_name(brain)
    path = xml_path(brain_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Baseline privilege grant — do this BEFORE creating/starting the task so the immediate
    # `schtasks /run` below can actually launch (and so the boot trigger works too).
    _grant_batch_logon(brain, info)

    logon = "Password" if password else "S4U"
    path.write_text(build_xml(brain, distro, logon), encoding="utf-16")

    cmd = ["schtasks", "/create", "/tn", name, "/xml", str(path), "/ru", brain, "/f"]
    if password:
        cmd += ["/rp", password]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode == 0:
        info(f"residency task '{name}' registered ({logon} logon).")
        run = subprocess.run(["schtasks", "/run", "/tn", name], capture_output=True, text=True)
        if run.returncode == 0:
            info("residency keepalive started now (also fires at every boot).")
        else:
            info(f"registered; will start at next boot (start now: schtasks /run /tn \"{name}\").")
        return True

    err = (p.stderr or p.stdout).strip()
    info(f"could not register residency: {err}")
    if logon == "S4U":
        info("S4U needs SeTcbPrivilege (a plain admin lacks it) — provide the brain password "
             "for Password logon instead.")
    info("The XML is written and ready. From an ELEVATED console, run:")
    print()
    print(f"    {_manual_oneliner(brain, path)}")
    print()
    if cred_hint:
        info(cred_hint)
    return False
