#!/usr/bin/env python3
r"""
brain_truths.py - the per-brain CONFIG-EXPOSURE seam ("brain truths").

WHAT THIS IS (the one-paragraph model)
--------------------------------------
Every configuration surface the brain's stack uses lives *behind* the WSL
virtualization boundary (in the distro's ext4). An administrator on the host
should never have to `wsl` in to find "where is Chroma's config" or "which certs
are these". So we expose all of it on the host, in a **human-named** layout, and
we do it the way Unix already solved config vs data:

    system/brain_bin/   = /bin   — brain tools + the WSL runtime home        (brain-owned)
    system/brain_sbin/  = /sbin  — admin/superuser tools (this file lives here)
    brain_etc/   = /etc   — ALL configuration    (admin read-write, brain READ-ONLY)
    knowledge/            — the brain's knowledge domain: the source you feed it
                            (inbox/) PLUS a host door into the LIVE vector store

`brain_etc/` is the source of truth for config. It is mounted READ-ONLY into the
distro at /opt/brain_truths (drvfs, -o ro), and the running stack consumes a
working copy on ext4 that `apply` syncs FROM the mount. So:

  * the admin edits config on the host, in folders named for what they hold;
  * the brain can read it but CANNOT tamper with the source (RO mount + ACL);
  * "sync" is never a background clock — `apply` copies mount -> runtime as the
    FIRST step of any tool that needs fresh config (see apply_brain_truths.sh),
    then acts, then validates, so an admin edit + tool run can't race.

DATA is the deliberate exception. Chroma's vector store is a SQLite-backed store;
you never run that over a 9p host mount, so the bytes stay on ext4 (~/chroma_store,
brain-writable). The admin still gets REAL read/write access to the live store via
a host door under knowledge/ (knowledge/chroma_store) that resolves into the distro
(\\wsl.localhost\...). The door lives in knowledge/ on purpose: the vector store IS
the brain's knowledge (the indexed form of what you fed it), so it sits beside the
source you dropped in — not under ops/var plumbing.

THE CASCADE: adding a new configurable service is ONE entry in SEAM below. The
host tree, the per-folder READMEs, the copy manifest, and the mount all follow.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# The category table — the single source of the cascade. Names are HUMAN-FORWARD:
# an admin hunting for "Chroma's configuration" opens brain_etc/chroma/, not a
# nameless .env under ops/. `runtime` is where the working copy lives in-distro
# (None = admin/machine plumbing, not copied into the stack). `files` are the
# surfaces that category owns.
# --------------------------------------------------------------------------- #
class Category:
    def __init__(self, name, title, blurb, runtime, files):
        self.name, self.title, self.blurb = name, title, blurb
        self.runtime, self.files = runtime, files

# Top-level control panel (brain_etc/brain.env, NOT under a category folder). Two zones split
# at ===NEURONS===: the flat service panel is RENDERED to brain_etc/docker/.env.rendered (then
# synced to ~/docker/.env - see MANIFEST_PAIRS); the YAML neuron zone is read by the neuron
# tooling via brain_env.py. Handled specially in seed/readme.
BRAIN_ENV = "brain.env"

SEAM = [
    Category(
        "docker", "Docker Compose stack",
        "The Compose files that define the whole stack - the sealed Chroma container, the "
        "nginx TLS gateway, ollama, and fail2ban - and how they network together. Images "
        "track :latest (newest-always; there are NO version pins). compose.ollama-gateway.yaml "
        "compose.chroma-gateway.yaml / compose.ollama-gateway.yaml are the OPTIONAL, "
        "symmetric exposure overlays, each layered (-f) only when its *_EXPOSE knob is on "
        ".",
        runtime="~/docker", files=["compose.yaml", "compose.chroma-gateway.yaml",
                                   "compose.ollama-gateway.yaml", "compose.action-neuron-gateway.yaml"]),
    Category(
        "chroma", "Chroma configuration",
        "chroma.env is the vector store's OWN config (loaded as the chroma container's "
        "env_file): telemetry, CORS, persistence, limits. It is a PASSTHROUGH - the whole "
        "CHROMA_* surface is yours to set. NOT here: the shared CHROMA_MASTER_TOKEN_FOR_GW and the gateway "
        "posture (exposed/TLS/authz/bind/port) - those are stack posture and live in brain.env.",
        runtime="~/docker/chroma", files=["chroma.env"]),
    Category(
        "ollama", "Ollama model server",
        "The sealed embedding/LLM model server on brain_net (no host port). ollama.env holds "
        "the server tuning (keep-alive, parallelism, queue depth) - a PASSTHROUGH env_file, so "
        "the whole OLLAMA_* surface is yours. models is the AUTHORITATIVE roster this brain must "
        "have - `ollama_models.py sync` converges the store to it. Ollama has no config file and "
        "no 'active model'; administer the store here, not the server.",
        runtime="~/docker/ollama", files=["ollama.env", "models"]),
    Category(
        "gateway", "nginx TLS gateway",
        "The one published surface. You edit TWO files here: gateway.conf (TUNING - rate limits "
        "+ fail2ban policy) and token_registry (bearer tokens). "
        "Everything the running stack consumes is GENERATED from those two PLUS brain.env by "
        "gateway_config.py, into the backend subfolders nginx_auto_gen/ (nginx.conf.template, "
        "ratelimit.conf, ollama.conf), token_maps_auto_gen/ (the four nginx maps) and "
        "fail2ban_autoconfigs/ (jail.d/filter.d/action.d) - NEVER hand-edit those. The gateway's "
        "on/off + exposure + TLS + authz-mode + bind/port live in brain.env, not here.",
        runtime="~/docker/nginx", files=["gateway.conf", "token_registry"]),
    Category(
        "tls", "Gateway TLS certificate",
        "The gateway's TLS certificate and private key - the ONLY PKI in the stack. "
        "Chroma itself has no cert; it is reached in-network by token. To bring your "
        "own cert (Enterprise), drop cert.pem + cert.key here with the same names.",
        runtime="~/gateway/gateway_out", files=["cert.pem", "cert.key"]),
    Category(
        "wsl", "WSL / virtualization admin",
        "Machine plumbing for the virtualization boundary: apply.manifest, the mount->runtime "
        "copy list the apply primitive reads. (The apply primitive itself is a TOOL, not config "
        "- it lives in the brain_sbin wsl_in_distro_scripts seam and runs in-distro at "
        "/opt/brain_wsl_in_distro_scripts/apply_brain_truths.sh.) Kept out of the service config "
        "folders so those stay clean.",
        runtime=None, files=["apply.manifest"]),
]

# The AUTHORITATIVE mount(brain_etc)-relative src -> in-distro dst copy list. Explicit
# (not derived from SEAM) because the gateway category's generated backend fans out to TWO
# runtime roots (~/docker/nginx and ~/docker/fail2ban) and flattens the *_auto_gen prefixes.
# `~` resolves to the brain home. gateway.conf + token_registry are NOT synced: they are
# host-side generator INPUTS; only their generated output (below) reaches the stack.
MANIFEST_PAIRS = [
    # brain.env is now the TWO-ZONE control panel (flat service panel + a YAML neuron zone,
    # split at ===NEURONS===). Compose must never see the YAML zone, so we ship the GENERATED
    # flat-only render (brain_env.render_dotenv -> brain_etc/docker/.env.rendered), NOT brain.env.
    ("docker/.env.rendered",                                 "~/docker/.env"),
    ("chroma/chroma.env",                                     "~/docker/chroma/chroma.env"),
    ("ollama/ollama.env",                                     "~/docker/ollama/ollama.env"),
    ("ollama/models",                                         "~/docker/ollama/models"),
    ("docker/compose.yaml",                                   "~/docker/compose.yaml"),
    ("docker/compose.chroma-gateway.yaml",                    "~/docker/compose.chroma-gateway.yaml"),
    ("docker/compose.ollama-gateway.yaml",                    "~/docker/compose.ollama-gateway.yaml"),
    ("docker/compose.action-neuron-gateway.yaml",                    "~/docker/compose.action-neuron-gateway.yaml"),
    # sources.yaml is now RENDERED from the brain.env ===NEURONS=== zone (config-flow Phase 5,
    # brain_env.render_sources_yaml -> docker/neuron/sources.yaml); the hand-authored
    # brain_etc/neuron/sources.yaml is retired. We ship the GENERATED file, same runtime target.
    ("docker/neuron/sources.yaml",                            "~/docker/neuron/sources.yaml"),
    ("github/github.env",                                     "~/docker/github/github.env"),
    ("github/known_hosts",                                    "~/docker/github/known_hosts"),
    ("gateway/nginx_auto_gen/nginx.conf.template",            "~/docker/nginx/nginx.conf.template"),
    ("gateway/nginx_auto_gen/ratelimit.conf",                 "~/docker/nginx/ratelimit.conf"),
    ("gateway/nginx_auto_gen/chroma.conf",                    "~/docker/nginx/chroma.conf"),
    ("gateway/nginx_auto_gen/ollama.conf",                    "~/docker/nginx/ollama.conf"),
    ("gateway/nginx_auto_gen/action.conf",                    "~/docker/nginx/action.conf"),
    ("gateway/nginx_auto_gen/internal.conf",                  "~/docker/nginx/internal.conf"),
    ("gateway/nginx_auto_gen/njs/inspect.js",                 "~/docker/nginx/njs/inspect.js"),
    ("gateway/token_maps_auto_gen/action_tokens.map",         "~/docker/nginx/action_tokens.map"),
    ("gateway/token_maps_auto_gen/reader_tokens.map",         "~/docker/nginx/reader_tokens.map"),
    ("gateway/token_maps_auto_gen/writer_tokens.map",         "~/docker/nginx/writer_tokens.map"),
    ("gateway/token_maps_auto_gen/ollama_use.map",            "~/docker/nginx/ollama_use.map"),
    ("gateway/token_maps_auto_gen/ollama_admin.map",          "~/docker/nginx/ollama_admin.map"),
    ("gateway/fail2ban_autoconfigs/jail.d/gateway.conf",      "~/docker/fail2ban/jail.d/gateway.conf"),
    ("gateway/fail2ban_autoconfigs/filter.d/nginx-gateway.conf", "~/docker/fail2ban/filter.d/nginx-gateway.conf"),
    ("gateway/fail2ban_autoconfigs/action.d/seam-banlist.conf",  "~/docker/fail2ban/action.d/seam-banlist.conf"),
    ("tls/cert.pem",                                          "~/gateway/gateway_out/cert.pem"),
    ("tls/cert.key",                                          "~/gateway/gateway_out/cert.key"),
]

MOUNT_POINT = "/opt/brain_truths"          # RO view of host brain_etc, inside the distro
# The apply primitive is an admin TOOL, not config: it lives in the brain_sbin
# wsl_in_distro_scripts seam and rides that seam's read-only drvfs mount into the distro
# (installed by `wsl_scripts.py install`) - no base64 push needed. Every consumer runs it here.
APPLY_REMOTE = "/opt/brain_wsl_in_distro_scripts/apply_brain_truths.sh"   # in-distro path (scripts seam)
DATA_DOOR = ("brain_rw/chroma", "knowledge/brain_rw/chroma")   # knowledge/<door> -> live in-distro store (brain_rw zone)
LOGS_DOOR = ("live", "logs")                                   # system/brain_logs/<door> -> in-distro ~/logs (ADR-0018 log seam)


# --------------------------------------------------------------------------- #
# Paths / identity
# --------------------------------------------------------------------------- #
def distro_name(brain):        return f"brain-{brain}"
def brain_home(brain):         return f"/home/{brain}"
def host_etc(brain_dir):       return Path(brain_dir) / "brain_etc"
def host_knowledge(brain_dir): return Path(brain_dir) / "knowledge"
def host_logs(brain_dir):      return Path(brain_dir) / "system" / "brain_logs"

def _rt(brain, runtime):
    """Resolve a category's ~-relative runtime dir to an absolute in-distro path."""
    return runtime.replace("~", brain_home(brain), 1) if runtime else None


# --------------------------------------------------------------------------- #
# show — the human-facing map (what lives where, on host AND in the distro)
# --------------------------------------------------------------------------- #
def cmd_show(args):
    brain = args.brain
    etc = host_etc(args.brain_dir)
    print(f"\nbrain truths for '{brain}'  -  config exposed on the host, human-named\n")
    print(f"  HOST source of truth : {etc}   (admin read-write, brain read-only)")
    print(f"  distro (read-only)   : {MOUNT_POINT}   (drvfs -o ro; the stack syncs from here)\n")
    for c in SEAM:
        where = f"-> {_rt(brain, c.runtime)}" if c.runtime else "(admin plumbing - not copied into the stack)"
        print(f"  brain_etc/{c.name}/   {c.title}")
        print(f"      {', '.join(c.files)}   {where}")
    door, target = DATA_DOOR
    print(f"\n  knowledge/{door}/   live vector store (DATA - brain-writable; admin door)")
    print(f"      real read/write door into the live store: "
          f"\\\\wsl.localhost\\{distro_name(brain)}{brain_home(brain)}/{target}".replace('/', '\\'))
    print()
    return 0


# --------------------------------------------------------------------------- #
# The copy manifest — generated from SEAM so the in-distro shell primitive
# (apply_brain_truths.sh) stays dumb: it reads this and copies mount -> runtime.
# One line per file:   <mount-relative-src>\t<absolute in-distro dest>
# --------------------------------------------------------------------------- #
def build_manifest(brain, brain_dir=None):
    home = brain_home(brain)
    if brain_dir is None:
        brain_dir = _default_brain_dir(brain)
    pairs = MANIFEST_PAIRS
    # TLS cert/key are a BYO/Enterprise SEAM OVERRIDE, not a required sync. A self-signed
    # deploy generates the cert IN-DISTRO (~/gateway/gateway_out via gen-cert.sh) and the seam
    # ships only cert.pem.example — so listing tls/cert.pem unconditionally makes the in-distro
    # apply_brain_truths die "source missing on mount: tls/cert.pem" on EVERY fresh deploy
    # (then its rollback fails too). Sync the TLS pair ONLY when an admin has actually placed a
    # real cert in the seam (brain_etc/tls/cert.pem); otherwise leave the in-distro cert as-is.
    if not (host_etc(brain_dir) / "tls" / "cert.pem").is_file():
        pairs = [p for p in MANIFEST_PAIRS if not p[0].startswith("tls/")]
    lines = [f"{src}\t{dst.replace('~', home, 1)}" for src, dst in pairs]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# The systemd .mount unit — reproduces the proven `mount -t drvfs <host> <mp> -o ro`
# as a durable unit (survives reboot). Unit name MUST encode the mount point:
# /opt/brain_truths -> opt-brain_truths.mount
# --------------------------------------------------------------------------- #
def mount_unit_name():
    return MOUNT_POINT.strip("/").replace("/", "-") + ".mount"

def mount_unit_text(brain_dir):
    what = str(host_etc(brain_dir))    # Windows path; drvfs accepts it as-is
    return (
        "[Unit]\n"
        "Description=Brain truths - host brain_etc exposed read-only\n"
        "# no After=local-fs.target: mount units are implicitly Before=local-fs.target\n"
        "# (DefaultDependencies); an explicit After= creates an ordering cycle -> flapping.\n\n"
        "[Mount]\n"
        f"What={what}\n"
        f"Where={MOUNT_POINT}\n"
        "Type=drvfs\n"
        "Options=ro\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


# --------------------------------------------------------------------------- #
# READMEs — layout IS the documentation. One index + one per category.
# --------------------------------------------------------------------------- #
def _index_readme(brain):
    out = [f"# brain truths — configuration for `{brain}`", "",
           "This is the brain's **/etc**: every configuration surface its stack uses, "
           "exposed on the host so you never have to reach into the distro to find it.",
           "",
           "- You (admin) may **read and write** these files.",
           f"- The brain sees them **read-only** at `{MOUNT_POINT}` inside the distro and "
           "cannot modify them.",
           "- After you edit, run the matching `brain_sbin` tool (or redeploy). It copies "
           "your change into the running stack **first**, then reloads, then validates — "
           "so an edit can never be half-applied.",
           "", "## Folders", ""]
    for c in SEAM:
        out.append(f"- **`{c.name}/`** — {c.title}. {c.blurb}")
    door, target = DATA_DOOR
    out += ["",
            f"> **Data lives elsewhere on purpose.** Chroma's vector store is a database; "
            f"it stays on the distro's fast local disk (`~/{target}`), not on this host "
            f"mount. For direct read/write access to the live data, use `knowledge/{door}/` "
            f"— it opens the real store inside the distro. It sits in the **`brain_rw`** zone "
            f"of the data-in seam (`knowledge/`), because the store is brain-produced data a "
            f"service writes; the source you feed the brain goes in the read-only `brain_ro` "
            f"zone beside it. See `knowledge/README.md`.", ""]
    return "\n".join(out)

def _cat_readme(c, brain):
    rt = _rt(brain, c.runtime)
    out = [f"# {c.title}", "", c.blurb, "", "## Files", ""]
    for f in c.files:
        out.append(f"- `{f}`")
    if rt:
        out += ["", f"These are copied into the running stack at `{rt}/` when config is "
                "applied. Edit here (the source of truth); the copy in the distro is "
                "overwritten on the next apply / boot."]
    else:
        out += ["", "Admin/machine plumbing — not copied into the stack."]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# seed — bootstrap the host source of truth FROM a running distro. First deploy
# generates config + secrets in-distro (stage4); this copies them OUT to brain_etc
# so the host becomes authoritative. Idempotent: re-run to re-capture.
# --------------------------------------------------------------------------- #
import base64

def _rab(brain_dir):
    """Locate run_as_brain.py (the only thing that can reach the brain-registered
    distro): prefer the brain's staged copy, else the one beside this tool."""
    p = Path(brain_dir) / "system" / "brain_sbin" / "run_as_brain.py"
    return p if p.is_file() else (Path(__file__).resolve().parent / "run_as_brain.py")

def _wsl_read(brain, brain_dir, in_distro_path):
    """Read a file from inside the distro (bytes) THROUGH run_as_brain (the distro is
    registered to the brain account, invisible to a plain admin console). Transport is
    `base64 -w0` -> one line -> we take the last non-empty stdout line (run_as_brain
    prints a 'run_as_brain: ...' banner first) and decode. Returns None if absent."""
    p = subprocess.run([sys.executable, str(_rab(brain_dir)), "--brain", brain, "--wsl",
                        "--", "base64", "-w0", in_distro_path],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None
    lines = [ln for ln in (p.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        return base64.b64decode(lines[-1], validate=True)
    except Exception:
        return None

def cmd_seed(args):
    brain, etc = args.brain, host_etc(args.brain_dir)
    print(f"seeding host source of truth from distro {distro_name(brain)} -> {etc}")
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "README.md").write_text(_index_readme(brain), encoding="utf-8")
    captured = 0
    for c in SEAM:
        cdir = etc / c.name
        cdir.mkdir(exist_ok=True)
        (cdir / "README.md").write_text(_cat_readme(c, brain), encoding="utf-8")
        if not c.runtime:
            continue
        for f in c.files:
            data = _wsl_read(brain, args.brain_dir, f"{_rt(brain, c.runtime)}/{f}")
            if data is not None:
                (cdir / f).write_bytes(data)
                captured += 1
                print(f"  captured {c.name}/{f}")
            else:
                print(f"  [skip] {c.name}/{f} not present in distro yet")
    # The apply PRIMITIVE is a tool (brain_sbin), not config - it is NOT written into
    # brain_etc; it rides the wsl_in_distro_scripts seam mount (wsl_scripts.py install).
    wsl = etc / "wsl"
    # write_bytes (NOT write_text): on Windows write_text opens in text mode and
    # translates \n -> \r\n, and bash `read` would then leave a trailing \r on the last
    # field — every synced file lands at a phantom '<name>\r' path. Ship LF bytes.
    (wsl / "apply.manifest").write_bytes(build_manifest(brain, args.brain_dir).encode("utf-8"))
    print(f"  wrote wsl/apply.manifest ({len(MANIFEST_PAIRS)} entries)")
    print(f"seeded {captured} config file(s). Next: set ACLs, install the mount unit, "
          f"open the data door.")
    return 0


def cmd_manifest(args):
    sys.stdout.write(build_manifest(args.brain, args.brain_dir));  return 0

def cmd_mount_unit(args):
    sys.stdout.write(mount_unit_text(args.brain_dir));  return 0


# --------------------------------------------------------------------------- #
# acl — the FHS /etc posture on the host: admins read-write, the brain READ-ONLY.
#
# The brain must be able to READ this config through the RO drvfs (9p) mount, but
# never MODIFY the source on the host. The obvious way — grant RX then add a
# deny-write ACE — is a TRAP on WSL: drvfs opens a file for read with an access
# request that includes write-implying rights, so an explicit DENY(W) ACE for the
# mounting identity vetoes the whole open. The brain (and even root) then gets
# `Permission denied` reading the mount — the seam silently never syncs. (Root-caused
# 2026-07-04: `head`/`ls`/`cp` on the mount all fail while `stat` succeeds; removing
# the deny ACE fixes the read instantly. See objective 008.)
#
# So we enforce read-only WITHOUT a deny ACE: break inheritance (freezing the admin
# ACEs as explicit), strip the brain's inherited Full-control — from BOTH the brain
# user AND its per-brain group (the brain is a member, so the group's Full would
# re-grant write) — and re-grant plain Read+Execute. No write ALLOW remains, so the
# host side is read-only; the RO mount is the second (in-distro) write barrier.
# --------------------------------------------------------------------------- #
def _icacls(path, *rules):
    p = subprocess.run(["icacls", str(path), *rules, "/C"], capture_output=True, text=True)
    ok = p.returncode == 0
    if not ok:
        print(f"  [WARN] icacls {rules}: {(p.stderr or p.stdout).strip()}")
    return ok

def cmd_acl(args):
    brain, etc = args.brain, host_etc(args.brain_dir)
    if not etc.is_dir():
        print(f"[ERROR] {etc} does not exist yet — run `seed` first.");  return 1
    group = f"{brain}_group"                              # per-brain group (create_brain convention)
    print(f"applying /etc posture to {etc}: admins RW, {brain} read-only (no deny ACE — drvfs-safe)")
    _icacls(etc, "/inheritance:d")                        # freeze inherited ACEs as explicit (keeps admin Full)
    _icacls(etc, "/remove:g", brain, "/remove:g", group, "/T")   # drop the brain's inherited Full (allow)
    _icacls(etc, "/remove:d", brain, "/remove:d", group, "/T")   # drop any legacy deny ACE (the drvfs read-breaker)
    _icacls(etc, "/grant", f"{brain}:(OI)(CI)RX",
                 "/grant", f"{group}:(OI)(CI)RX", "/T")           # read + traverse only; NO write, NO deny
    print("  done (read-only via RX-only allow, no deny ACE; RO mount enforces it in-distro too)")
    return 0


# --------------------------------------------------------------------------- #
# door — knowledge/<door>/: the admin's REAL read/write access to the live vector
# store, which stays on the distro's ext4 (never run a SQLite store over 9p). A
# directory symlink to \\wsl.localhost\<distro>\... opens the actual files. It lives
# under knowledge/ (beside the source inbox) because the vector store IS the brain's
# knowledge — the indexed form of what was fed in — not ops/var plumbing.
# --------------------------------------------------------------------------- #
def cmd_door(args):
    brain = args.brain
    door_name, store = DATA_DOOR
    kdir = host_knowledge(args.brain_dir)
    link = kdir / door_name
    link.parent.mkdir(parents=True, exist_ok=True)   # ensure the brain_rw zone dir exists
    target = rf"\\wsl.localhost\{distro_name(brain)}{brain_home(brain)}/{store}".replace("/", "\\")
    # lexists (lstat, no follow), NOT exists: the door is a directory symlink into
    # \\wsl.localhost\<distro>\...; exists would follow it onto the 9p share and, when
    # the distro is down, raise WinError 64 instead of answering "is a door already here?".
    if os.path.lexists(link):
        print(f"  {link} already exists — leaving it");  return 0
    p = subprocess.run(["cmd", "/c", "mklink", "/D", str(link), target],
                       capture_output=True, text=True)
    if p.returncode == 0:
        print(f"  data door: {link} -> {target}")
        return 0
    print(f"  [WARN] could not create data door: {(p.stderr or p.stdout).strip()}")
    print(f"  (the distro must be running so {target} resolves; retry after deploy)")
    return 1


# --------------------------------------------------------------------------- #
# The read-only logs door (ADR-0018): a host-side window into the in-distro log
# seam root (~/logs). Mirrors cmd_door, but lands under system/brain_logs/ (ops
# plumbing, not knowledge) and is READ-ONLY to humans - the brain appends its own
# logs; the operator only reads. White team primary, blue team secondary.
# --------------------------------------------------------------------------- #
def cmd_logs_door(args):
    brain = args.brain
    door_name, store = LOGS_DOOR
    ldir = host_logs(args.brain_dir)
    link = ldir / door_name
    link.parent.mkdir(parents=True, exist_ok=True)   # ensure system/brain_logs/ exists
    target = rf"\\wsl.localhost\{distro_name(brain)}{brain_home(brain)}/{store}".replace("/", "\\")
    if os.path.lexists(link):                         # lexists, not exists (don't follow onto 9p)
        print(f"  {link} already exists — leaving it");  return 0
    p = subprocess.run(["cmd", "/c", "mklink", "/D", str(link), target],
                       capture_output=True, text=True)
    if p.returncode == 0:
        print(f"  logs door: {link} -> {target}  (read-only window into the seam)")
        return 0
    print(f"  [WARN] could not create logs door: {(p.stderr or p.stdout).strip()}")
    print(f"  (the distro must be running so {target} resolves; retry after deploy)")
    return 1


def cmd_provision(args):
    """One-shot host-side provisioning: seed the source of truth from the distro,
    apply the /etc ACL posture, and open the data + logs doors. (The RO mount unit +
    apply primitive are installed IN-distro by stage7_harden.sh on a fresh engine, or
    by `install-mount` on an existing/live distro.)"""
    rc = cmd_seed(args)
    cmd_acl(args)
    cmd_door(args)
    cmd_logs_door(args)
    return rc


# --------------------------------------------------------------------------- #
# In-distro install — enable the RO .mount unit in a RUNNING distro. Everything
# goes THROUGH run_as_brain (the distro is registered to the brain account, so a
# plain admin console can't see it). We push only the TINY mount unit, base64'd into
# ONE simple command (well under CreateProcessWithLogonW's ~1024-char cap and safe
# through the bridge — base64 is a single metachar-free token). The apply primitive is NOT
# pushed here: it rides the separate wsl_in_distro_scripts seam mount (`wsl_scripts.py
# install`) and runs by path. Works on engines that predate the seam.
# --------------------------------------------------------------------------- #
def _rab_run(brain, brain_dir, as_root, shell_cmd):
    args = [sys.executable, str(_rab(brain_dir)), "--brain", brain]
    args += ["--root"] if as_root else ["--wsl"]
    args += ["--", shell_cmd]
    return subprocess.run(args, text=True)

def cmd_install_mount(args):
    brain, brain_dir = args.brain, Path(args.brain_dir)
    unit_b64 = base64.b64encode(mount_unit_text(brain_dir).encode("utf-8")).decode("ascii")
    unit_path = f"/etc/systemd/system/{mount_unit_name()}"
    # ONE simple command list (ensure mountpoint; write unit from base64; reload; enable+mount).
    # mkdir -p, NOT `install -d -m 0755`: on a re-run the mountpoint may ALREADY be a live RO
    # mount, and `install` unconditionally chmods its target → "cannot change permissions:
    # Read-only file system" (EROFS) → the whole seam stage dies. mkdir -p is a no-op on an
    # existing dir (never chmods), keeping install-mount idempotent whether the RO mount is up
    # or not (mountpoint perms are cosmetic anyway — the mounted fs's own perms show through).
    cmd = (f"mkdir -p {MOUNT_POINT} && "
           f"echo {unit_b64} | base64 -d > {unit_path} && "
           f"systemctl daemon-reload && "
           f"systemctl enable --now {mount_unit_name()}")
    if len(cmd) > 1000:
        print(f"[ERROR] install command {len(cmd)} chars — exceeds the safe bridge cap");  return 1
    print(f"enabling brain-truths RO mount in {distro_name(brain)} (via run_as_brain --root)")
    p = _rab_run(brain, brain_dir, True, cmd)
    if p.returncode != 0:
        print("  [WARN] mount enable returned nonzero — check the distro is up and "
              "brain_etc is seeded (the mount source must exist).")
        return p.returncode
    print(f"  {MOUNT_POINT} mounted read-only")
    # The apply primitive is NOT installed here anymore - it rides the wsl_in_distro_scripts
    # seam mount. Ensure that seam is up too (`wsl_scripts.py install`) so APPLY_REMOTE resolves.
    print(f"  NOTE: apply primitive runs from the scripts seam ({APPLY_REMOTE});")
    print(f"        run `wsl_scripts.py install` if that mount is not yet enabled.")
    return p.returncode

def cmd_apply(args):
    """Run the in-distro apply primitive now (as the brain, via run_as_brain), syncing
    host config into the running stack. Handy for testing / after a host-side edit."""
    # Re-render brain.env's flat zone -> docker/.env.rendered FIRST, so a bare `apply` right
    # after a brain.env edit ships the fresh compose env (the manifest copies the render, not
    # brain.env). reapply does this via gateway_config.cmd_generate; do it here for the standalone
    # path too. Fail closed on a bad marker rather than sync a stale/missing .env.
    import brain_env
    try:
        brain_env.render_dotenv(args.brain_dir)
    except brain_env.BrainEnvError as e:
        print(f"[brain_truths] ERROR: cannot render .env from brain.env: {e}", file=sys.stderr)
        return 2
    return _rab_run(args.brain, args.brain_dir, False,
                    f"bash {APPLY_REMOTE}").returncode


# --------------------------------------------------------------------------- #
def _default_brain_dir(brain):
    # The install root is NEVER guessed: $AIOS_INSTALL_ROOT, else the platform's $HORIZON_ROOT,
    # else die. A wrong guess silently targets the wrong tree. abspath-anchor it so a
    # relative/name-only value can't yield a drive-less, CWD-relative brain-dir (the mangled-path
    # drift, NOTE 001-35). gateway_config.cmd_generate also fails closed if this doesn't resolve
    # to an existing dir. On a Horizon.AIOS install $HORIZON_ROOT IS the install root.
    env_root = os.environ.get("AIOS_INSTALL_ROOT") or os.environ.get("HORIZON_ROOT")
    if not env_root:
        raise SystemExit("[brain_truths] ERROR: --brain-dir was not given and neither "
                         "$AIOS_INSTALL_ROOT nor $HORIZON_ROOT is set. Pass --brain-dir <path>, "
                         "or set AIOS_INSTALL_ROOT to the install root (the folder that holds brains/).")
    root = os.path.abspath(env_root)
    return str(Path(root) / "brains" / brain)

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="brain_truths.py",
        description="Per-brain config-exposure seam: expose all config on the host, "
                    "human-named, admin-writable, brain read-only.")
    ap.add_argument("--brain", required=True, help="brain name")
    ap.add_argument("--brain-dir", dest="brain_dir", default=None,
                    help="the brain's host folder (default: %%AIOS_INSTALL_ROOT%%\\brains\\<brain>)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="print the human map of what lives where")
    sub.add_parser("provision", help="seed + acl + door (full host-side provisioning)")
    sub.add_parser("seed", help="capture current in-distro config OUT to brain_etc (bootstrap)")
    sub.add_parser("acl", help="apply the /etc posture: admins RW, brain read-only")
    sub.add_parser("door", help="open the knowledge/ data door to the live vector store")
    sub.add_parser("logs-door", help="open the read-only system/brain_logs/ door to the log seam (ADR-0018)")
    sub.add_parser("install-mount", help="install the RO mount + apply primitive into a running distro")
    sub.add_parser("apply", help="run the in-distro apply primitive now (sync host config -> stack)")
    sub.add_parser("manifest", help="print the mount->runtime copy manifest")
    sub.add_parser("mount-unit", help="print the systemd .mount unit for the RO drvfs mount")
    args = ap.parse_args(argv)
    if not args.brain_dir:
        args.brain_dir = _default_brain_dir(args.brain)
    return {"show": cmd_show, "provision": cmd_provision, "seed": cmd_seed,
            "acl": cmd_acl, "door": cmd_door, "logs-door": cmd_logs_door,
            "install-mount": cmd_install_mount,
            "apply": cmd_apply,
            "manifest": cmd_manifest, "mount-unit": cmd_mount_unit}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
