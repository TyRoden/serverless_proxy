# Protocol Compatibility and Validation Guide

This document is the canonical compatibility reference for `serverless-proxy`.

It summarizes what is intentionally supported on the proxy protocol surfaces, which strict-validation guarantees are enforced, and what validation probes should be run before release.

For a larger tested matrix snapshot, see `docs/Compatibility-Tests-Verified.md`.

## Supported protocol surfaces

- OpenAI-compatible routes (`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models`)
- Anthropic-compatible routes (`/v1/messages`, `/v1` HEAD)
- Ollama-compatible routes (`/api/chat`, `/api/generate`, `/api/embed`, `/api/embeddings`, `/api/show`, `/api/tags`, `/api/ps`)

## Compatibility guarantees (current)

- Stream-required OAuth backends are normalized so non-stream callers still receive valid non-stream protocol responses.
- OpenAI and Anthropic tool-calling support includes:
  - single-tool round-trip
  - multi-tool emission in one turn
  - multi-tool result aggregation in final answer
  - streaming and non-streaming final answer paths
- Embedding routes support cross-host virtual model routing (not only local Ollama backends).
- Validation strictness is enforced for malformed requests instead of fabricating friendly fallback responses.

## Strict validation guarantees

- OpenAI `/v1/chat/completions`
  - rejects missing required `model`
  - rejects missing or empty `messages`
  - rejects invalid tool-message ordering (unknown `tool_call_id`)
- OpenAI `/v1/completions`
  - supports true SSE framing when `stream:true`
  - honors `stop` sequence truncation (`string` or `string[]`)
- Anthropic `/v1/messages`
  - rejects missing required `model`
  - rejects missing required `max_tokens`
  - rejects missing or empty `messages`
  - rejects malformed tools missing required `name` or `input_schema`

## Release validation checklist

Run these probes after compatibility-sensitive changes:

1. OpenAI chat stream/non-stream smoke (`/v1/chat/completions`)
2. OpenAI tool round-trip smoke (single-tool and multi-tool)
3. OpenAI completions stream + stop-sequence probe (`/v1/completions`)
4. Anthropic messages stream/non-stream smoke (`/v1/messages`)
5. Anthropic tool round-trip smoke (single-tool and multi-tool)
6. Ollama embeddings smoke (`/api/embed`, `/api/embeddings`) on embedding and non-embedding models
7. Validation strictness probes for malformed payloads (OpenAI + Anthropic)

## Notes on scope

- Compatibility is verified against representative models and scenarios; it is not a claim that every model supports every feature.
- Tool-result fallback synthesis exists for upstream edge cases that return `finish_reason` without final assistant text.
- Keep error normalization proxy-side for malformed local-request patterns; avoid leaking provider-specific internals when pre-validation can fail early.
