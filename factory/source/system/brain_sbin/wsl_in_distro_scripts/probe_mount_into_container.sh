#!/usr/bin/env bash
# probe_mount_into_container.sh — does a runtime bind of the 9p /opt/input_neurons reach a container?
set -u
echo "== distro sees /opt/input_neurons =="
ls /opt/input_neurons | sed 's/^/  /'
echo "== container sees -v /opt/input_neurons:/x:ro =="
docker run --rm --entrypoint ls -v /opt/input_neurons:/x:ro "$(basename "$HOME")-input_neurons" -la /x 2>&1 | sed 's/^/  /'
echo "== DONE =="
