# Verified Compatibility Matrix for `serverless-proxy` on `:8002`

**Endpoint tested:** `http://192.168.50.157:8002`  
**Identity:** `GET /api/version` → `{"version":"serverless-proxy"}`

> This matrix reflects **directly verified behavior** from live testing.  
> It is **not a claim of universal support for every model or every edge case**.

## Status key
- ✅ Verified working
- ⚠️ Partially verified / limited scope
- ❌ Not verified / not tested here

---

## 1) Protocol Surface Overview

| Protocol Surface | Status | Notes |
|---|---:|---|
| Ollama API | ✅ | Core chat/generate/show tested successfully |
| OpenAI API | ✅ | `chat/completions`, `completions`, models, streaming tested |
| Anthropic API | ✅ | `/v1/messages`, streaming, tools tested |
| Embeddings | ✅ | Verified with valid embedding-capable models |
| Tool / Function Calling | ✅ | Verified on OpenAI and Anthropic, including multi-tool round-trips |

---

## 2) Ollama Compatibility

| Route / Capability | Status | Notes |
|---|---:|---|
| `GET /api/tags` | ✅ | Verified |
| `GET /api/ps` | ✅ | Verified |
| `POST /api/show` | ✅ | Verified for tested models |
| `POST /api/generate` non-stream | ✅ | Verified |
| `POST /api/generate` stream | ✅ | Verified |
| `POST /api/chat` non-stream | ✅ | Verified |
| `POST /api/chat` stream | ✅ | Verified |
| `system` field on generate | ✅ | Verified basic handling |
| `options` field on generate/chat | ✅ | Verified basic handling (`temperature`, `num_predict`) |
| `/api/embed` | ✅ | Verified with valid embedding models |
| `/api/embeddings` | ✅ | Verified with valid embedding models |
| Invalid model handling | ✅ | Verified |
| Non-embedding model on embedding route | ✅ | Verified structured error |

### Ollama models directly tested

| Model | Chat / Generate | Show | Embeddings |
|---|---:|---:|---:|
| `gpt-5.4-oauth` | ✅ | ✅ | ❌ by design / verified rejected on embedding route |
| `gemma4-e4b` | ✅ | ✅ | ❌ not validated as embedding-capable |
| `nomic-embed-text:latest` | ❌ not tested for chat | ✅ | ✅ |
| `qwen3-embedding` | ❌ not tested for chat | ✅ | ✅ |

**Ollama note:** Basic Ollama compatibility is strong in tested scenarios, but not every Ollama option or multimodal path was verified.

---

## 3) OpenAI Compatibility

| Route / Capability | Status | Notes |
|---|---:|---|
| `GET /v1/models` | ✅ | Verified |
| `GET /models` | ✅ | Verified |
| `POST /v1/chat/completions` non-stream | ✅ | Verified |
| `POST /v1/chat/completions` stream | ✅ | Verified SSE behavior |
| `POST /v1/completions` non-stream | ✅ | Verified |
| `POST /v1/completions` stream | ✅ | Verified SSE behavior |
| `stop` sequences on completions | ✅ | Verified in tested case |
| Invalid model handling | ✅ | Verified |
| Missing `messages` validation | ✅ | Verified proper 400 response |
| Malformed tool-message ordering validation | ✅ | Verified proper 400 response |

### OpenAI models directly tested

| Model | Chat Completions | Completions | Streaming |
|---|---:|---:|---:|
| `gpt-5.4-oauth` | ✅ | ✅ | ✅ |

**OpenAI note:** Core compatibility is strong in tested scenarios. Advanced structured-output features beyond tool calling were not verified here.

---

## 4) OpenAI Tool / Function Calling Compatibility

| Capability | Status | Notes |
|---|---:|---|
| Single tool declaration accepted | ✅ | Verified |
| Single tool call emitted | ✅ | Verified |
| Tool arguments preserved | ✅ | Verified |
| Tool round-trip final answer | ✅ | Verified |
| Streaming tool-call emission | ✅ | Verified |
| Streaming final answer after tool result | ✅ | Verified |
| Multiple tools offered | ✅ | Verified |
| Forced tool choice | ✅ | Verified |
| Multiple tool calls in one assistant turn | ✅ | Verified |
| Multi-tool round-trip aggregation | ✅ | Verified both tool results reflected in final answer |

### OpenAI tool scenarios directly tested

| Scenario | Status |
|---|---:|
| `get_weather(city)` single-call round-trip | ✅ |
| forced `get_time(city)` selection | ✅ |
| `get_weather + get_time` multi-call turn | ✅ |
| multi-tool final synthesis | ✅ |

**OpenAI tools note:** Basic and multi-tool agent workflows tested successfully. Extremely large tool payloads and partial-failure cases were not tested.

---

## 5) Anthropic Compatibility

| Route / Capability | Status | Notes |
|---|---:|---|
| `POST /v1/messages` non-stream | ✅ | Verified |
| `POST /v1/messages` stream | ✅ | Verified event stream |
| System prompt support | ✅ | Verified |
| Multi-turn conversation | ✅ | Verified |
| Text content block array input | ✅ | Verified |
| Invalid model handling | ✅ | Verified |
| Missing `messages` validation | ✅ | Verified proper 400 response |
| Tool schema validation | ✅ | Verified proper 400 response for malformed tool schema |

### Anthropic models directly tested

| Model | Messages | Streaming | Tools |
|---|---:|---:|---:|
| `gpt-5.4-oauth` | ✅ | ✅ | ✅ |
| `gemma4-e4b` | ✅ | ✅ | ⚠️ only basic message behavior verified earlier |

**Anthropic note:** Text and tool-use compatibility is strong in tested scenarios. Richer multimodal/document content blocks were not verified.

---

## 6) Anthropic Tool Compatibility

| Capability | Status | Notes |
|---|---:|---|
| Single tool_use block emitted | ✅ | Verified |
| Tool input preserved | ✅ | Verified |
| Tool-result round-trip final answer | ✅ | Verified |
| Streaming tool_use events | ✅ | Verified |
| Streaming final answer after tool_result | ✅ | Verified |
| Multiple tools offered | ✅ | Verified |
| Multiple `tool_use` blocks in one turn | ✅ | Verified |
| Multi-tool round-trip aggregation | ✅ | Verified both results reflected in final answer |

### Anthropic tool scenarios directly tested

| Scenario | Status |
|---|---:|
| `get_weather(city)` single tool_use round-trip | ✅ |
| `get_time(city)` selected from multiple tools | ✅ |
| `get_weather + get_time` multi-tool turn | ✅ |
| multi-tool final synthesis | ✅ |

---

## 7) Embedding Compatibility

| Capability | Status | Notes |
|---|---:|---|
| Ollama-style `/api/embed` | ✅ | Verified |
| Ollama-style `/api/embeddings` | ✅ | Verified |
| Embedding model metadata via `/api/show` | ✅ | Verified |
| Invalid embedding model handling | ✅ | Verified |
| Non-embedding model rejection on embedding route | ✅ | Verified |

### Embedding models directly tested

| Model | `/api/embed` | `/api/embeddings` | `/api/show` |
|---|---:|---:|---:|
| `nomic-embed-text:latest` | ✅ | ✅ | ✅ |
| `qwen3-embedding` | ✅ | ✅ | ✅ |

**Embedding note:** Verified only for these embedding-capable models. This should not be read as “all listed models support embeddings.”

---

## 8) Validation / Error Handling

| Validation Case | Status | Notes |
|---|---:|---|
| OpenAI invalid model | ✅ | Verified structured error |
| OpenAI missing `messages` | ✅ | Verified structured 400 |
| OpenAI bad tool message ordering | ✅ | Verified clean proxy-side 400 |
| Anthropic invalid model | ✅ | Verified structured error |
| Anthropic missing `messages` | ✅ | Verified structured 400 |
| Anthropic malformed tool schema | ✅ | Verified structured 400 |
| Ollama invalid model on show | ✅ | Verified |
| Ollama invalid model on embeddings | ✅ | Verified |
| Ollama non-embedding model on embedding route | ✅ | Verified structured unsupported-operation error |

---

## 9) Model Discovery / Surface Coherence

| Capability | Status | Notes |
|---|---:|---|
| Ollama model discovery via `/api/tags` | ✅ | Verified |
| Ollama runtime-ish model surface via `/api/ps` | ✅ | Verified |
| OpenAI model discovery via `/v1/models` | ✅ | Verified |
| Coherence spot check across discovery surfaces | ⚠️ | Verified on sampled models only, not exhaustively every listed model |

### Sampled visible models
- `gpt-5.4-oauth`
- `gemma4-e4b`
- `qwen3-embedding`
- `aiq-project-system-ai`
- multiple DeepInfra-backed models

---

## Tested Scenarios Summary

### Verified working end-to-end scenarios
- Ollama chat/generate for text models
- Ollama embeddings for proper embedding models
- OpenAI chat completions (stream + non-stream)
- OpenAI text completions (stream + non-stream)
- OpenAI stop-sequence handling
- OpenAI single-tool round-trip
- OpenAI multi-tool round-trip
- Anthropic messages (stream + non-stream)
- Anthropic system prompt + multi-turn text
- Anthropic single-tool round-trip
- Anthropic multi-tool round-trip

---

## Important Scope Notes

### This matrix does **not** verify
- every model listed by `/v1/models` or `/api/tags`
- image / multimodal inputs
- document/file content blocks
- advanced JSON-schema constrained outputs
- every Ollama option field
- long-context edge behavior
- concurrency/stress behavior
- partial failure handling when one of multiple tools fails
- rate limiting / retry semantics
- auth edge cases across every upstream provider family

---

## Overall Assessment

| Area | Assessment |
|---|---|
| Core multi-protocol compatibility | Strong in tested scenarios |
| Agent/tool workflow compatibility | Strong in tested scenarios |
| Validation/error behavior | Good in tested scenarios |
| Embeddings | Good for tested embedding-capable models |
| Advanced edge-case coverage | Still incomplete / not fully tested |

### Bottom line
For the scenarios tested, `:8002` behaves like a **capable multi-protocol compatibility proxy** across:
- Ollama
- OpenAI
- Anthropic
- embeddings
- single-tool and multi-tool agent workflows
