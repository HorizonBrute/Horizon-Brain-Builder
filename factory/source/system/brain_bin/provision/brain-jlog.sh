#!/usr/bin/env bash
# Shared structured logger for brain maintenance events.
# JSON-lines schema (one object per line) -> ~/logs/brain-maintenance.jsonl
#   ts       : UTC ISO-8601
#   host     : distro hostname
#   component: e.g. chroma
#   event    : backup | update_check | update_bump | update_ok | update_rollback
#   result   : ok | fail | warn | noop | start
#   from,to  : version transition (may be empty)
#   detail   : free text
#   artifact : path to produced artifact (e.g. a backup file), may be empty
BRAIN_LOG_DIR="${BRAIN_LOG_DIR:-$HOME/logs}"
BRAIN_LOG="${BRAIN_LOG:-$BRAIN_LOG_DIR/brain-maintenance.jsonl}"

jlog() {
  mkdir -p "$BRAIN_LOG_DIR"
  local comp="${1:-}" ev="${2:-}" res="${3:-}" from="${4:-}" to="${5:-}" detail="${6:-}" artifact="${7:-}"
  local ts; ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  jq -cn \
    --arg ts "$ts" --arg host "$(hostname)" --arg component "$comp" --arg event "$ev" \
    --arg result "$res" --arg from "$from" --arg to "$to" --arg detail "$detail" --arg artifact "$artifact" \
    '{ts:$ts,host:$host,component:$component,event:$event,result:$result,from:$from,to:$to,detail:$detail,artifact:$artifact}' \
    >> "$BRAIN_LOG"
}
