#!/usr/bin/env bash
# probe_state.sh — READ-ONLY ground-truth probe of the live brain distro.
# Reports layout, stack, store location, ollama model, git/ssh readiness. Mutates nothing.
set -u
H="$HOME"
echo "== identity =="
id
echo "HOME=$H"
echo
echo "== distro layout (~) =="
ls -la "$H" | sed 's/^/  /'
echo
echo "== ~/knowledge (new layout target) =="
if [ -d "$H/knowledge" ]; then find "$H/knowledge" -maxdepth 3 -printf '  %y %p\n'; else echo "  ABSENT"; fi
echo
echo "== ~/chroma_store (old store) =="
if [ -d "$H/chroma_store" ]; then du -sh "$H/chroma_store" 2>/dev/null | sed 's/^/  /'; ls -la "$H/chroma_store" | sed 's/^/  /'; else echo "  ABSENT"; fi
echo
echo "== ~/docker (compose stack dir) =="
if [ -d "$H/docker" ]; then ls -la "$H/docker" | sed 's/^/  /'; echo "  --- .env ---"; [ -f "$H/docker/.env" ] && sed 's/^/    /' "$H/docker/.env"; else echo "  ABSENT"; fi
echo
echo "== running containers =="
docker ps --format '  {{.Names}}\t{{.Status}}\t{{.Image}}' 2>&1
echo
echo "== docker networks =="
docker network ls --format '  {{.Name}}\t{{.Driver}}' 2>&1 | grep -i "$(basename "$H")" || echo "  (no brain net listed)"
echo
echo "== ollama models (via container) =="
docker exec "$(basename "$H")-ollama" ollama list 2>&1 | sed 's/^/  /' || echo "  ollama container not reachable"
echo
echo "== git + ssh readiness (for git@github clone) =="
git --version 2>&1 | sed 's/^/  /'
ls -la "$H/.ssh" 2>&1 | sed 's/^/  /'
echo "  -- ssh github auth test --"
ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -T git@github.com 2>&1 | sed 's/^/  /'
echo
echo "== compose config seam mounts (in-distro) =="
findmnt -t drvfs,9p -o TARGET,SOURCE,OPTIONS 2>/dev/null | sed 's/^/  /' || echo "  (findmnt none)"
echo
echo "== DONE =="
