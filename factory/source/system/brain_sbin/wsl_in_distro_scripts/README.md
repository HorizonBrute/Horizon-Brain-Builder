# In-distro admin scripts

Scripts here run inside the brain's WSL distro. This host folder (admin read-write) is
mounted read-only into the distro at `/opt/brain_wsl_in_distro_scripts` by
`wsl_scripts.py install`. Author a script here; run it in-distro by path.

## Rules
- Scripts only — configuration lives in `brain_etc`.
- Admin read-write here; the brain sees it read-only in the distro.
- `.py` runs under `python3`, `.sh` under `bash` (by extension).
- Self-contained (stdlib / distro tools). Reach the stack on the distro's `localhost`
  (gateway `:8000` chroma, `:11434` ollama) or on `brain_net`.

## Use
```
python system/brain_sbin/wsl_scripts.py install            # enable the mount (one time)
python system/brain_sbin/wsl_scripts.py list               # list scripts
python system/brain_sbin/wsl_scripts.py run foo.py -- ARGS # run in-distro as the brain
python system/brain_sbin/wsl_scripts.py --as-root run x.sh # run in-distro as root
```
