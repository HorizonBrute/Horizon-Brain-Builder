#!/usr/bin/env bash
# verify_stage_b.sh — confirm the layout reconcile before building the neuron.
set -u
H="$HOME"
echo "== chroma container + bind path =="
docker inspect -f '  running={{.State.Running}} image={{.Config.Image}}' "$(basename "$H")-chroma" 2>&1
docker inspect -f '{{range .Mounts}}  mount {{.Source}} -> {{.Destination}}{{"\n"}}{{end}}' "$(basename "$H")-chroma" 2>&1
echo
echo "== new store dir (should be populated by chroma now) =="
ls -la "$H/knowledge/brain_rw/chroma" 2>&1 | sed 's/^/  /'
echo
echo "== neuron config synced into ~/docker =="
if [ -f "$H/docker/neuron/sources.yaml" ]; then grep -nE 'adapter:|url:|include:' "$H/docker/neuron/sources.yaml" | sed 's/^/  /'; else echo "  MISSING"; fi
echo
echo "== /opt/input_neurons READABILITY (the 9p read-wall test) =="
findmnt /opt/input_neurons 2>&1 | sed 's/^/  /'
echo "  -- can the brain READ the code+Dockerfile? --"
head -1 /opt/input_neurons/Dockerfile 2>&1 | sed 's/^/  Dockerfile: /'
head -1 /opt/input_neurons/neuron.py 2>&1 | sed 's/^/  neuron.py: /'
ls /opt/input_neurons/delivery/*.py 2>&1 | sed 's/^/  /'
echo
echo "== chroma reachable in-network (heartbeat via gateway localhost) =="
curl -sk --max-time 5 -o /dev/null -w '  gateway heartbeat http=%{http_code}\n' https://127.0.0.1:8000/api/v2/heartbeat 2>&1
echo "== DONE =="
