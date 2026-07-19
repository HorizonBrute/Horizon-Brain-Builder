#!/usr/bin/env bash
# mk_knowledge_layout.sh — create the brain-owned data-in seam layout (idempotent).
#   knowledge/brain_ro/  — brain READ-ONLY source content; ingest reads this :ro
#   knowledge/brain_rw/chroma/ — brain READ-WRITE; the vector store bind target (chroma :/data)
# Runs as the brain, so the tree is brain-owned (chroma writes brain_rw/chroma as the brain uid).
# (config-flow Phase 5 removed knowledge/inbox/ — it was never a wired ingest source.)
set -eu
mkdir -p "$HOME/knowledge/brain_ro"
mkdir -p "$HOME/knowledge/brain_rw/chroma"
echo "== knowledge layout =="
find "$HOME/knowledge" -maxdepth 3 -printf '  %y %M %u %p\n'
echo "== DONE =="
