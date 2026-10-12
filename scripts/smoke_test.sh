#!/usr/bin/env bash
# ===========================================================================
# Text2Voice Agent - end-to-end smoke test against a running server.
#
#   bash scripts/smoke_test.sh [BASE_URL]        # default http://127.0.0.1:8000
#
# Exercises the real HTTP surface with the offline `mock` providers, so it
# needs no API keys and no network access. Exits non-zero on any failure.
#
# Windows notes (this script is meant to run under Git Bash / WSL):
#   * A Windows checkout may leave CR at end-of-line, which corrupts every
#     JSON body. The guard below strips them and re-execs once.
#   * curl.exe decodes argv with the ANSI code page, so passing non-ASCII text
#     as a command-line argument mangles it into invalid UTF-8. Every request
#     body containing CJK is therefore written to a file with a heredoc (bash
#     writes the bytes directly) and sent with `--data-binary @file`, and the
#     one CJK query parameter is percent-encoded.
# ===========================================================================
set -uo pipefail

if grep -q $'\r' "$0" 2>/dev/null; then
  tr -d '\r' < "$0" > "$0.lf" && mv "$0.lf" "$0" && exec bash "$0" "$@"
fi

BASE="${1:-http://127.0.0.1:8000}"
API="$BASE/api/v1"
PASS=0
FAIL=0

c_green=$'\033[32m'; c_red=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

check() { # check <label> <0|1> [detail]
  if [ "$2" = "0" ]; then
    PASS=$((PASS + 1)); printf '%s  PASS%s %s\n' "$c_green" "$c_off" "$1"
  else
    FAIL=$((FAIL + 1)); printf '%s  FAIL%s %s %s%s%s\n' "$c_red" "$c_off" "$1" "$c_dim" "${3:-}" "$c_off"
  fi
}

# jget <json> <python-expression-over-`d`>
jget() { python -c "import json,sys; d=json.loads(sys.argv[1]); print($2)" "$1" 2>/dev/null; }

status_code() { curl -sS -o /dev/null -w '%{http_code}' "$@" 2>/dev/null; }
post_file() { curl -fsS -X POST "$API$1" -H 'Content-Type: application/json' --data-binary "@$2"; }
post_status() { status_code -X POST "$API$1" -H 'Content-Type: application/json' --data-binary "@$2"; }

# --- request bodies (heredoc -> bash writes UTF-8 bytes verbatim) -----------
BODY_DIR=$(mktemp -d)
trap 'rm -rf "$BODY_DIR"' EXIT

cat > "$BODY_DIR/generate.json" <<'JSON'
{"text":"请用一句话介绍杭州西湖。","llm":{"provider":"mock","model":"mock-echo-v1"},"tts":{"provider":"mock","voice":"mock-neutral","format":"wav"},"options":{"auto_save":true,"fallback_enabled":true}}
JSON

cat > "$BODY_DIR/leak.json" <<'JSON'
{"text":"密钥检查","llm":{"provider":"mock","api_key":"sk-proj-LEAKCANARY1234567890"},"tts":{"provider":"mock","format":"wav"},"options":{"auto_save":true}}
JSON

echo "==> Text2Voice Agent smoke test against $BASE"
echo

# --- 1. health --------------------------------------------------------------
HEALTH=$(curl -fsS "$BASE/health" 2>/dev/null)
check "GET /health returns 200" "$([ -n "$HEALTH" ] && echo 0 || echo 1)"
check "health.status == ok" "$([ "$(jget "$HEALTH" "d['status']")" = "ok" ] && echo 0 || echo 1)" "$HEALTH"
check "health.database == ok" "$([ "$(jget "$HEALTH" "d['database']")" = "ok" ] && echo 0 || echo 1)"

# --- 2. providers -----------------------------------------------------------
PROVIDERS=$(curl -fsS "$API/providers")
check "GET /providers returns 200" "$([ -n "$PROVIDERS" ] && echo 0 || echo 1)"
check "8+ LLM providers registered" "$([ "$(jget "$PROVIDERS" "len(d['llm'])")" -ge 8 ] && echo 0 || echo 1)"
check "6+ TTS providers registered" "$([ "$(jget "$PROVIDERS" "len(d['tts'])")" -ge 6 ] && echo 0 || echo 1)"
check "openai/anthropic/qwen present" \
  "$(jget "$PROVIDERS" "all(x in [p['id'] for p in d['llm']] for x in ('openai','anthropic','qwen'))" | grep -q True && echo 0 || echo 1)"
check "elevenlabs/azure/volcengine present" \
  "$(jget "$PROVIDERS" "all(x in [p['id'] for p in d['tts']] for x in ('elevenlabs','azure','volcengine'))" | grep -q True && echo 0 || echo 1)"
check "provider catalog exposes voices + formats" \
  "$(jget "$PROVIDERS" "all(p['voices'] and p['formats'] for p in d['tts'])" | grep -q True && echo 0 || echo 1)"

# --- 3. OpenAPI / Swagger ---------------------------------------------------
curl -fsS "$BASE/openapi.json" > /tmp/t2v-openapi.json
check "GET /openapi.json returns 200" "$([ -s /tmp/t2v-openapi.json ] && echo 0 || echo 1)"
check "OpenAPI documents the shared error envelope" "$(grep -q 'ErrorResponse' /tmp/t2v-openapi.json && echo 0 || echo 1)"
check "GET /docs (Swagger UI) returns 200" "$([ "$(status_code "$BASE/docs")" = "200" ] && echo 0 || echo 1)"
check "OpenAPI lists all core paths" \
  "$(grep -q '/api/v1/generate' /tmp/t2v-openapi.json && grep -q '/api/v1/tasks/{task_id}' /tmp/t2v-openapi.json && grep -q '/api/v1/audio/{record_id}' /tmp/t2v-openapi.json && echo 0 || echo 1)"

# --- 4. synchronous generation ---------------------------------------------
GEN=$(post_file /generate "$BODY_DIR/generate.json")
check "POST /generate returns a payload" "$([ -n "$GEN" ] && echo 0 || echo 1)" "$GEN"
RECORD_ID=$(jget "$GEN" "d['record_id']")
check "response has a record_id" "$([ -n "$RECORD_ID" ] && echo 0 || echo 1)" "$GEN"
check "reply_text is non-empty" "$([ "$(jget "$GEN" "len(d['reply_text'])")" -gt 0 ] && echo 0 || echo 1)"
check "audio_bytes > 44 (valid WAV header)" "$([ "$(jget "$GEN" "d['audio_bytes']")" -gt 44 ] && echo 0 || echo 1)"
check "duration_ms is reported" "$([ "$(jget "$GEN" "d['duration_ms'] or 0")" -gt 0 ] && echo 0 || echo 1)"
check "not degraded with the mock provider" "$([ "$(jget "$GEN" "d['degraded']")" = "False" ] && echo 0 || echo 1)"
check "total_latency_ms recorded" "$([ "$(jget "$GEN" "d['total_latency_ms']")" -ge 0 ] && echo 0 || echo 1)"
check "reply echoes the submitted text" "$(jget "$GEN" "d['input_text']" | grep -q . && echo 0 || echo 1)"

# --- 5. audio playback / download / range ----------------------------------
AUDIO_URL="$BASE$(jget "$GEN" "d['audio_url']")"
CODE=$(status_code "$AUDIO_URL")
check "GET audio_url returns 200" "$([ "$CODE" = "200" ] && echo 0 || echo 1)" "got $CODE"
check "audio payload starts with RIFF" \
  "$(curl -fsS "$AUDIO_URL" -o /tmp/t2v-audio.wav && head -c 4 /tmp/t2v-audio.wav | grep -q RIFF && echo 0 || echo 1)"
check "?download=1 sets Content-Disposition: attachment" \
  "$(curl -fsS -D - -o /dev/null "$AUDIO_URL?download=1" | grep -qi 'content-disposition: attachment' && echo 0 || echo 1)"
check "Range request returns 206" "$([ "$(status_code -H 'Range: bytes=0-99' "$AUDIO_URL")" = "206" ] && echo 0 || echo 1)"

# --- 6. async task lifecycle ------------------------------------------------
TASK=$(post_file /tasks "$BODY_DIR/generate.json")
TASK_ID=$(jget "$TASK" "d['task_id']")
check "POST /tasks returns a task_id" "$([ -n "$TASK_ID" ] && echo 0 || echo 1)" "$TASK"
check "task starts in pending/running" \
  "$(jget "$TASK" "d['status'] in ('pending','running')" | grep -q True && echo 0 || echo 1)"

STATUS=""; POLL=""
for _ in $(seq 1 150); do
  POLL=$(curl -fsS "$API/tasks/$TASK_ID")
  STATUS=$(jget "$POLL" "d['status']")
  if [ "$STATUS" = "succeeded" ] || [ "$STATUS" = "failed" ]; then break; fi
  sleep 0.2
done
check "task reaches 'succeeded'" "$([ "$STATUS" = "succeeded" ] && echo 0 || echo 1)" "$POLL"
check "task progress reaches 100" "$([ "$(jget "$POLL" "d['progress']")" = "100" ] && echo 0 || echo 1)"
check "task stage is done" "$([ "$(jget "$POLL" "d['stage']")" = "done" ] && echo 0 || echo 1)"

RESULT=$(curl -fsS "$API/tasks/$TASK_ID/result")
check "GET /tasks/{id}/result returns the payload" \
  "$([ "$(jget "$RESULT" "len(d['reply_text'])")" -gt 0 ] && echo 0 || echo 1)"
check "unknown task returns 404" "$([ "$(status_code "$API/tasks/deadbeef")" = "404" ] && echo 0 || echo 1)"

# --- 7. history -------------------------------------------------------------
HISTORY=$(curl -fsS "$API/history?page=1&page_size=10")
check "GET /history lists the generated records" "$([ "$(jget "$HISTORY" "d['total']")" -ge 2 ] && echo 0 || echo 1)"
check "history items carry audio_url" \
  "$([ "$(jget "$HISTORY" "all(i['audio_url'] for i in d['items'])")" = "True" ] && echo 0 || echo 1)"
check "pagination metadata is present" "$([ "$(jget "$HISTORY" "d['pages']")" -ge 1 ] && echo 0 || echo 1)"
# 西湖 percent-encoded: curl.exe cannot carry raw CJK in argv.
check "search filter works" \
  "$([ "$(jget "$(curl -fsS "$API/history?q=%E8%A5%BF%E6%B9%96")" "d['total']")" -ge 1 ] && echo 0 || echo 1)"
check "history stats endpoint" "$([ "$(jget "$(curl -fsS "$API/history/stats")" "d['total']")" -ge 2 ] && echo 0 || echo 1)"
check "CSV export returns text/csv" \
  "$(curl -fsS -D - -o /dev/null "$API/history/export" | grep -qi 'content-type: text/csv' && echo 0 || echo 1)"

# --- 8. settings ------------------------------------------------------------
SETTINGS=$(curl -fsS "$API/settings")
check "GET /settings returns 200" "$([ -n "$SETTINGS" ] && echo 0 || echo 1)"
check "settings expose no api_key field" "$(echo "$SETTINGS" | grep -q 'api_key' && echo 1 || echo 0)"
check "PUT /settings updates a preference" \
  "$(curl -fsS -X PUT "$API/settings" -H 'Content-Type: application/json' -d '{"default_speed":1.25}' | grep -q '1.25' && echo 0 || echo 1)"
check "PUT /settings rejects an api_key field (422)" \
  "$([ "$(status_code -X PUT "$API/settings" -H 'Content-Type: application/json' -d '{"api_key":"sk-proj-shouldneverpersist"}')" = "422" ] && echo 0 || echo 1)"

# --- 9. error contract ------------------------------------------------------
ERR=$(curl -sS "$API/does-not-exist")
check "404 uses the error envelope" "$([ "$(jget "$ERR" "d['code']")" = "NOT_FOUND" ] && echo 0 || echo 1)" "$ERR"
check "error carries a request_id" "$([ -n "$(jget "$ERR" "d['request_id']")" ] && echo 0 || echo 1)"
check "malformed request returns 422" \
  "$([ "$(status_code -X POST "$API/generate" -H 'Content-Type: application/json' -d '{"text":"hi"}')" = "422" ] && echo 0 || echo 1)"
check "unsupported provider returns 400" \
  "$([ "$(status_code -X POST "$API/generate" -H 'Content-Type: application/json' -d '{"text":"hi","llm":{"provider":"nope"},"tts":{"provider":"mock"}}')" = "400" ] && echo 0 || echo 1)"
check "missing credential returns 400" \
  "$([ "$(status_code -X POST "$API/generate" -H 'Content-Type: application/json' -d '{"text":"hi","llm":{"provider":"openai"},"tts":{"provider":"mock"}}')" = "400" ] && echo 0 || echo 1)"

# --- 10. credential hygiene -------------------------------------------------
LEAK=$(post_file /generate "$BODY_DIR/leak.json")
check "response never echoes the submitted key" "$(echo "$LEAK" | grep -q 'LEAKCANARY' && echo 1 || echo 0)"
check "database never stores the submitted key" \
  "$(grep -rq 'LEAKCANARY' "$(dirname "$0")/../backend/data" 2>/dev/null && echo 1 || echo 0)"
check "server log never contains the submitted key" "$(grep -q 'LEAKCANARY' /tmp/t2v-server.log 2>/dev/null && echo 1 || echo 0)"

# --- 11. cleanup ------------------------------------------------------------
check "DELETE /history/{id} returns 204" "$([ "$(status_code -X DELETE "$API/history/$RECORD_ID")" = "204" ] && echo 0 || echo 1)"
check "deleted audio returns 404" "$([ "$(status_code "$AUDIO_URL")" = "404" ] && echo 0 || echo 1)"

echo
printf 'result: %s%d passed%s, %s%d failed%s\n' "$c_green" "$PASS" "$c_off" "$c_red" "$FAIL" "$c_off"
[ "$FAIL" -eq 0 ] || exit 1