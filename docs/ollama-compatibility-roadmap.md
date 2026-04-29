# Ollama Endpoint Compatibility Roadmap

This document tracks Ollama protocol compatibility work in two phases.

## Phase 1 - Runtime Inference Compatibility

Goal: make proxy usable as an Ollama endpoint for inference clients and OpenAI-compatible clients without breaking existing behavior.

- [x] Add explicit `endpoint_type=ollama` routing (no generic fallback)
- [x] Add native Ollama runtime routes:
  - [x] `GET /api/tags`
  - [x] `GET /api/version`
  - [x] `POST /api/chat`
  - [x] `POST /api/generate`
  - [x] `POST /api/embed`
  - [x] `POST /api/embeddings` (compat alias)
- [x] Add OpenAI compatibility aliases commonly used by SDKs:
  - [x] `POST /chat/completions`
  - [x] `POST /api/chat/completions`
  - [x] `GET /models`, `GET /api/models`, `GET /api/v1/models`
  - [x] `POST /embeddings`, `POST /api/v1/embeddings`
- [x] Normalize OpenAI block-content messages for Ollama (`messages[].content` string)
- [x] Add Ollama error diagnostics with payload preview (`[OLLAMA_400]`)
- [x] Route Ollama virtual-model OpenAI requests upstream via `/v1/chat/completions` first, fallback `/api/chat`
- [x] Add response marker header for path verification: `X-Proxy: serverless-proxy`
- [x] Add ingress/egress diagnostics middleware (`[HTTP_IN]`, `[HTTP_OUT]`, `[HTTP_ERR]`)
- [x] Build a lightweight conformance test script for runtime endpoints (`scripts/ollama_runtime_conformance.sh`)
  - [x] Non-stream chat
  - [x] Stream chat
  - [x] Tool-calling chat (many tools)
  - [x] Structured outputs (`json` mode)
  - [x] Generate endpoint parity
  - [x] Embeddings success and capability failure paths

## Phase 2 - Full Ollama Surface Compatibility

Goal: support full native Ollama API surface for model lifecycle and admin operations, while translating request/response shapes for non-Ollama backends where that is practical.

- [ ] Add/verify model metadata and runtime routes:
  - [x] `POST /api/show`
    - [x] passthrough for real Ollama upstreams
    - [x] synthetic compatibility response for non-Ollama virtual models
  - [x] `POST /api/ps` (and `GET /api/ps`)
- [ ] Add model lifecycle routes:
  - [x] `POST /api/pull`
  - [x] `POST /api/push`
  - [x] `POST /api/create`
  - [x] `POST /api/copy`
  - [x] `DELETE /api/delete` (and `POST /api/delete`)
- [ ] Add blob routes used by create/import flows:
  - [x] `POST /api/blobs/:digest`
  - [x] `HEAD /api/blobs/:digest`
- [ ] Add explicit capability contracts and errors for unsupported upstreams
  - [x] translated `/api/show` compatibility contract for non-Ollama backends
  - [x] explicit streaming-not-supported response for unsupported translated backends
- [x] Add end-to-end conformance suite for full surface (`scripts/ollama_full_surface_conformance.sh`)
- [x] Document known version-dependent behavior by Ollama release (`docs/ollama-version-notes.md`)

## Acceptance Criteria

- [ ] OpenClaw/OpenAI JS against proxy base URL works for chat and tools on Ollama virtual models
- [ ] Native Ollama clients work against proxy runtime routes
- [ ] Native Ollama clients can inspect translated non-Ollama virtual models via `/api/show`
- [ ] Existing non-Ollama endpoint behavior is unchanged
- [ ] Diagnostics can identify method/path/upstream-shape issues in one run
