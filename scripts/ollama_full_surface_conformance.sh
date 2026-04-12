#!/usr/bin/env bash
set -euo pipefail

# Full-surface Ollama compatibility checks for proxy routes.
#
# Default mode is safe/non-mutating.
# Set OLLAMA_RUN_MUTATING=1 to include create/copy/delete/pull/push probes.

BASE_URL="${OLLAMA_PROXY_BASE_URL:-http://localhost:8002}"
MODEL="${OLLAMA_TEST_MODEL:-gemma4:e4b}"
RUN_MUTATING="${OLLAMA_RUN_MUTATING:-0}"

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

assert_route_reachable() {
  local route="$1"
  local code="$2"
  local hdr_file="$3"
  if ! grep -qi '^x-proxy: serverless-proxy' "$hdr_file"; then
    fail "$route did not include X-Proxy marker; request may not have hit this proxy"
  fi
  pass "$route reachable (status $code)"
}

printf "Running full-surface checks against %s (model=%s, mutating=%s)\n" "$BASE_URL" "$MODEL" "$RUN_MUTATING"

# Non-mutating endpoints
code=$(request "show" POST "$BASE_URL/api/show" "{\"model\":\"$MODEL\"}")
assert_route_reachable "/api/show" "$code" "$TMP_DIR/show.hdr"

code=$(request "ps_get" GET "$BASE_URL/api/ps")
assert_route_reachable "GET /api/ps" "$code" "$TMP_DIR/ps_get.hdr"

code=$(request "ps_post" POST "$BASE_URL/api/ps" "{}")
assert_route_reachable "POST /api/ps" "$code" "$TMP_DIR/ps_post.hdr"

code=$(request "blob_head" HEAD "$BASE_URL/api/blobs/sha256:test")
assert_route_reachable "HEAD /api/blobs/{digest}" "$code" "$TMP_DIR/blob_head.hdr"

code=$(curl -sS -D "$TMP_DIR/blob_post.hdr" -o "$TMP_DIR/blob_post.out" -w "%{http_code}" -X POST "$BASE_URL/api/blobs/sha256:test" --data-binary 'abc')
assert_route_reachable "POST /api/blobs/{digest}" "$code" "$TMP_DIR/blob_post.hdr"

if [[ "$RUN_MUTATING" == "1" ]]; then
  test_model="proxy-compat-test-$RANDOM"
  copy_model="proxy-compat-copy-$RANDOM"

  code=$(request "pull" POST "$BASE_URL/api/pull" "{\"model\":\"$MODEL\",\"stream\":false}")
  assert_route_reachable "/api/pull" "$code" "$TMP_DIR/pull.hdr"

  code=$(request "push" POST "$BASE_URL/api/push" "{\"model\":\"$MODEL\",\"stream\":false}")
  assert_route_reachable "/api/push" "$code" "$TMP_DIR/push.hdr"

  code=$(request "create" POST "$BASE_URL/api/create" "{\"model\":\"$test_model\",\"from\":\"$MODEL\",\"stream\":false}")
  assert_route_reachable "/api/create" "$code" "$TMP_DIR/create.hdr"

  code=$(request "copy" POST "$BASE_URL/api/copy" "{\"source\":\"$MODEL\",\"destination\":\"$copy_model\"}")
  assert_route_reachable "/api/copy" "$code" "$TMP_DIR/copy.hdr"

  code=$(request "delete_delete" DELETE "$BASE_URL/api/delete" "{\"model\":\"$copy_model\"}")
  assert_route_reachable "DELETE /api/delete" "$code" "$TMP_DIR/delete_delete.hdr"

  code=$(request "delete_post" POST "$BASE_URL/api/delete" "{\"model\":\"$test_model\"}")
  assert_route_reachable "POST /api/delete" "$code" "$TMP_DIR/delete_post.hdr"
else
  warn "Skipping mutating endpoints. Set OLLAMA_RUN_MUTATING=1 to run full lifecycle checks."
fi

printf "Full-surface conformance checks completed.\n"
