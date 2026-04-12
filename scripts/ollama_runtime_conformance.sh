#!/usr/bin/env bash
set -euo pipefail

# Runtime conformance smoke tests for Ollama-compatible inference routes.
#
# Usage:
#   OLLAMA_PROXY_BASE_URL=http://localhost:8002 \
#   OLLAMA_TEST_MODEL=gemma4:26b \
#   ./scripts/ollama_runtime_conformance.sh

BASE_URL="${OLLAMA_PROXY_BASE_URL:-http://localhost:8002}"
MODEL="${OLLAMA_TEST_MODEL:-gemma4:26b}"
EMBED_MODEL="${OLLAMA_TEST_EMBED_MODEL:-$MODEL}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

pass() { printf "[PASS] %s\n" "$1"; }
warn() { printf "[WARN] %s\n" "$1"; }
fail() { printf "[FAIL] %s\n" "$1"; exit 1; }

request() {
  local name="$1"
  local method="$2"
  local url="$3"
  local body="${4:-}"
  local out="$TMP_DIR/${name}.out"
  local hdr="$TMP_DIR/${name}.hdr"
  local code

  if [[ -n "$body" ]]; then
    code=$(curl -sS -D "$hdr" -o "$out" -w "%{http_code}" -X "$method" "$url" -H "Content-Type: application/json" -d "$body")
  else
    code=$(curl -sS -D "$hdr" -o "$out" -w "%{http_code}" -X "$method" "$url")
  fi

  printf "%s" "$code"
}

json_has_key() {
  local file="$1"
  local key="$2"
  python3 - "$file" "$key" <<'PY'
import json,sys
f,key=sys.argv[1],sys.argv[2]
try:
    obj=json.load(open(f))
except Exception:
    print("0")
    raise SystemExit(0)
print("1" if key in obj else "0")
PY
}

contains_header() {
  local file="$1"
  local needle="$2"
  python3 - "$file" "$needle" <<'PY'
import sys
data=open(sys.argv[1], 'r', errors='ignore').read().lower()
print("1" if sys.argv[2].lower() in data else "0")
PY
}

printf "Running runtime conformance checks against %s (model=%s)\n" "$BASE_URL" "$MODEL"

# 1) Model listing paths
for p in "/v1/models" "/models" "/api/models" "/api/v1/models" "/api/tags" "/api/version"; do
  code=$(request "models$(echo "$p" | tr '/' '_')" GET "$BASE_URL$p")
  [[ "$code" == "200" ]] || fail "$p expected 200, got $code"
  pass "$p returns 200"
done

# 2) OpenAI chat non-stream
code=$(request "v1_chat_nonstream" POST "$BASE_URL/v1/chat/completions" "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with OK\"}],\"stream\":false,\"max_tokens\":16}")
[[ "$code" == "200" ]] || fail "/v1/chat/completions non-stream expected 200, got $code"
[[ "$(json_has_key "$TMP_DIR/v1_chat_nonstream.out" "choices")" == "1" ]] || fail "/v1/chat/completions missing choices"
pass "/v1/chat/completions non-stream"

# 3) OpenAI chat alias
code=$(request "chat_alias" POST "$BASE_URL/chat/completions" "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with OK\"}],\"stream\":false,\"max_tokens\":16}")
[[ "$code" == "200" ]] || fail "/chat/completions expected 200, got $code"
pass "/chat/completions alias"

# 4) Native Ollama chat non-stream
code=$(request "api_chat_nonstream" POST "$BASE_URL/api/chat" "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with OK\"}],\"stream\":false}")
[[ "$code" == "200" ]] || fail "/api/chat non-stream expected 200, got $code"
[[ "$(json_has_key "$TMP_DIR/api_chat_nonstream.out" "message")" == "1" ]] || fail "/api/chat missing message"
pass "/api/chat non-stream"

# 5) Native Ollama generate non-stream
code=$(request "api_generate_nonstream" POST "$BASE_URL/api/generate" "{\"model\":\"$MODEL\",\"prompt\":\"Reply with OK\",\"stream\":false}")
[[ "$code" == "200" ]] || fail "/api/generate non-stream expected 200, got $code"
[[ "$(json_has_key "$TMP_DIR/api_generate_nonstream.out" "done")" == "1" ]] || fail "/api/generate missing done"
pass "/api/generate non-stream"

# 6) Streaming endpoint shape checks
code=$(request "api_chat_stream" POST "$BASE_URL/api/chat" "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hi\"}],\"stream\":true}")
[[ "$code" == "200" ]] || fail "/api/chat stream expected 200, got $code"
[[ "$(contains_header "$TMP_DIR/api_chat_stream.hdr" "application/x-ndjson")" == "1" ]] || warn "/api/chat stream did not return NDJSON content-type"
pass "/api/chat stream status"

code=$(request "v1_chat_stream" POST "$BASE_URL/v1/chat/completions" "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hi\"}],\"stream\":true,\"max_tokens\":8}")
[[ "$code" == "200" ]] || fail "/v1/chat/completions stream expected 200, got $code"
[[ "$(contains_header "$TMP_DIR/v1_chat_stream.hdr" "text/event-stream")" == "1" ]] || warn "/v1/chat/completions stream did not return SSE content-type"
pass "/v1/chat/completions stream status"

# 7) Tool-calling (best-effort)
TOOLS_PAYLOAD=$(cat <<JSON
{"model":"$MODEL","stream":false,"messages":[{"role":"user","content":"Use the tool to return weather for Tokyo"}],"tools":[{"type":"function","function":{"name":"get_weather","description":"Get weather","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]}
JSON
)
code=$(request "tool_call" POST "$BASE_URL/v1/chat/completions" "$TOOLS_PAYLOAD")
if [[ "$code" == "200" ]]; then
  pass "tool-calling request accepted"
else
  warn "tool-calling returned $code (model may not support tools)"
fi

# 8) JSON mode / structured output (best-effort)
code=$(request "json_mode" POST "$BASE_URL/api/generate" "{\"model\":\"$MODEL\",\"prompt\":\"Return valid JSON with key ok=true\",\"format\":\"json\",\"stream\":false}")
if [[ "$code" == "200" ]]; then
  pass "json mode request accepted"
else
  warn "json mode returned $code"
fi

# 9) Embeddings checks
code=$(request "embed_native" POST "$BASE_URL/api/embed" "{\"model\":\"$EMBED_MODEL\",\"input\":\"hello\"}")
if [[ "$code" == "200" ]]; then
  [[ "$(json_has_key "$TMP_DIR/embed_native.out" "embeddings")" == "1" ]] || warn "/api/embed 200 but missing embeddings field"
  pass "/api/embed success path"
elif [[ "$code" == "400" || "$code" == "404" || "$code" == "501" ]]; then
  pass "/api/embed capability failure path observed ($code)"
else
  warn "/api/embed returned unexpected status $code"
fi

code=$(request "embed_openai" POST "$BASE_URL/v1/embeddings" "{\"model\":\"$EMBED_MODEL\",\"input\":\"hello\"}")
if [[ "$code" == "200" ]]; then
  [[ "$(json_has_key "$TMP_DIR/embed_openai.out" "data")" == "1" ]] || warn "/v1/embeddings 200 but missing data field"
  pass "/v1/embeddings success path"
elif [[ "$code" == "400" || "$code" == "404" || "$code" == "501" ]]; then
  pass "/v1/embeddings capability failure path observed ($code)"
else
  warn "/v1/embeddings returned unexpected status $code"
fi

printf "Conformance checks completed.\n"
