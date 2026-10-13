#!/usr/bin/env bash
# Live end-to-end check: submit a real generation task and poll it to completion.
#
# Usage (from Git Bash):
#   T2V_KEY=<api-key> bash scripts/e2e_generate.sh
#
# It writes a payload, POSTs it to a running backend, polls the task until it
# reaches a terminal state, then reports the audio the server produced. Set
# T2V_BASE to point at a different host/port.
set -u

BASE="${T2V_BASE:-http://127.0.0.1:8000/api/v1}"
PY="${T2V_PY:-$(dirname "$0")/../.venv/Scripts/python.exe}"
KEY="${T2V_KEY:?set T2V_KEY to an API key before running}"

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
PAYLOAD="$SELF_DIR/.e2e_payload.json"
OUT="$SELF_DIR/.e2e_task.json"

cleanup() { rm -f "$PAYLOAD" "$OUT"; }
trap cleanup EXIT

# Build the payload in Python: MSYS heredocs mangle the quotes inside JSON.
"$PY" "$SELF_DIR/e2e_payload.py" "$KEY" "$PAYLOAD" || exit 1

echo "=== POST $BASE/tasks ==="
RESP=$(curl -s -m 20 -X POST "$BASE/tasks" \
  -H 'Content-Type: application/json' \
  --data-binary @"$PAYLOAD")
echo "$RESP" | head -c 400

TASK=$(printf '%s' "$RESP" | "$PY" -c \
  "import sys,json; print(json.load(sys.stdin).get('task_id',''))" 2>/dev/null)
echo
if [ -z "$TASK" ]; then
  echo "[X] no task_id returned; aborting"
  exit 1
fi
echo "task_id=$TASK"

for i in $(seq 1 50); do
  sleep 3
  STATE=$(curl -s -m 10 "$BASE/tasks/$TASK")
  printf '%s' "$STATE" > "$OUT"
  LINE=$(printf '%s' "$STATE" | "$PY" -c \
    "import sys,json; d=json.load(sys.stdin); print(d.get('status'), d.get('stage'), d.get('progress'), (d.get('message') or '')[:50])" 2>/dev/null)
  echo "[$i] $LINE"
  case "$LINE" in
    succeeded*|failed*|canceled*) break ;;
  esac
done

echo "=== result ==="
"$PY" "$SELF_DIR/e2e_report.py" "$OUT"
