# Ollama Version Notes

This document tracks known Ollama version-dependent behavior relevant to proxy compatibility.

## Scope

- Native Ollama API routes (`/api/*`)
- Ollama OpenAI-compatible routes (`/v1/*`)
- Tool-calling, structured output, streaming, and embeddings behavior

## Version-Dependent Behavior

### OpenAI-compatible endpoint maturity (`/v1/*`)

- Behavior differs across Ollama releases for advanced OpenAI fields and large tool payloads.
- In this proxy, `endpoint_type=ollama` chat routing is:
  1. try upstream `POST /v1/chat/completions`
  2. fallback to `POST /api/chat` on `404/405`
- Rationale: some client payloads (notably large tool schemas) may parse more reliably via `/v1/chat/completions`.

### Native `/api/chat` message content shape

- Ollama native chat expects `messages[].content` as string content.
- OpenAI block-style content arrays can fail native parsing depending on payload/model/server behavior.
- Proxy mitigation: normalize block arrays to string content when forwarding to native `/api/chat`.

### Tool-calling payload differences

- `tool_choice` object handling is OpenAI-specific and may be rejected on native Ollama route.
- Proxy mitigation: do not forward OpenAI `tool_choice` object to native `/api/chat` payload.

### Structured outputs (`format` / `response_format`)

- Native Ollama supports `format` values (`"json"` or JSON schema).
- OpenAI-style `response_format` variants may not map 1:1.
- Proxy mapping:
  - `response_format.type=json_object` -> `format="json"`
  - avoids passing unsupported/ambiguous schema objects directly in native mode unless explicitly supported by route logic.

### Streaming transport differences

- Native Ollama routes stream NDJSON (`application/x-ndjson`).
- OpenAI-compatible routes stream SSE (`text/event-stream`).
- Proxy preserves these semantics by route family.

### Embeddings capability is model-dependent

- Even when route is valid, specific models may not support embeddings.
- Proxy behavior:
  - upstream attempt `/api/embed`, fallback `/api/embeddings`
  - returns upstream capability error (often non-200/501-like behavior) rather than masking it.

### Management routes behavior

- Full native surface routes are proxied/passthrough.
- Exact status/body semantics for `/api/show`, `/api/ps`, `/api/pull`, `/api/push`, `/api/create`, `/api/copy`, `/api/delete`, `/api/blobs/*` depend on upstream Ollama version and state.

## Verification Commands

Runtime inference conformance:

```bash
./scripts/ollama_runtime_conformance.sh
```

Full-surface conformance (non-mutating):

```bash
./scripts/ollama_full_surface_conformance.sh
```

Full-surface conformance (including mutating lifecycle checks):

```bash
OLLAMA_RUN_MUTATING=1 ./scripts/ollama_full_surface_conformance.sh
```

## Diagnostics to Capture During Version Triage

- Proxy headers: verify `X-Proxy: serverless-proxy`
- Proxy logs: `[HTTP_IN]`, `[HTTP_OUT]`, `[HTTP_ERR]`, `[OLLAMA_400]`
- Payload audit snapshots (if enabled): `data/payload_audit/*`
