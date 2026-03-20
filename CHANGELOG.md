# Changelog

All notable changes to the RunPod Serverless Proxy will be documented in this file.

## [1.4.0] - 2026-03-20

> Forked from [runpod-serverless-proxy](https://github.com/dannysemi/runpod-serverless-proxy) by [Daniel Semanisin](https://github.com/dannysemi)
> Tested with [RunPod Worker Ollama](https://hub.docker.com/r/ollama/ollama) `ollama@0.18.2` on RunPod Serverless

### Added

- Comprehensive tool call parsing supporting multiple model output formats:
  - Fenced JSON: ` ```tool_call {"name": "...", "arguments": {...}} ``` `
  - XML-style: `<tool_code>{"name":"...","arguments":{...}}</tool_code>` (OpenCode task format)
  - `<tool_use code name="...">` format
  - Bare Python calls: `task(description="...", prompt="...")` with both `:` and `=` argument separators
  - Multiple calls per fence: `{"name":"x"}{"name":"y"}`
- Content-wide bare call extraction that scans remaining text after fence removal
- `KNOWN_TOOL_NAMES` frozenset for efficient tool name lookup
- `parse_json_objects()` for parsing concatenated JSON objects
- `<tool_code>...</tool_code>` XML tag support for OpenCode task tool calls
- `_fix_json_newlines()` for fixing malformed JSON with real newlines in string values
- `.env` file for secrets management (`.env.example` template provided)
- **AI Queue Master integration** — Optional routing through AI Queue Master for priority queuing and request tracking:
  - `USE_AI_QUEUE` — Enable/disable queue routing
  - `AI_QUEUE_URL` — Queue Master URL
  - `AI_QUEUE_PRIORITY` — Priority level (HIGH/NORMAL/LOW)
  - `AI_QUEUE_SOURCE` — Source identifier for tracking
  - `extra_hosts` configuration for Docker networking

### Fixed

- Bare call extraction only searched remaining text (not full content), preventing double-extraction of fenced tool calls
- `_parse_bare_call` now supports both `key: value` and `key = value` argument formats

## [1.3.0] - 2026-03-20

### Fixed

- Tool call fence echoed in `message.content` when tool calls extracted
- Newline preservation in text responses (`\n`.join instead of space join)
- Nested JSON parsing in tool arguments (handles `arguments` as embedded JSON with real newlines)
- Multiple tool calls in single fence (`{"name":"x"}{"name":"y"}` format)
- Duplicate unreachable code in `extract_tool_calls` function
- `<tool_use code name="...">` format support (model sometimes uses this instead of fenced format)
- Malformed JSON with real newlines in string values fixed via pre-processing
- Bare Python-style function call format support: ` ``` task(description: "...", ...) ``` `
- Fence content without `tool_call` prefix (plain JSON or bare calls) now correctly parsed

## [1.2.0] - 2026-03-20

### Added

- Ollama endpoint support via `ENDPOINT_TYPE` environment variable
- Switch between vLLM and Ollama formats dynamically
- Ollama format converts messages to prompt format for RunPod Ollama endpoints

## [1.1.0] - 2026-03-20

### Fixed

- Chain-of-thought stripping for `assistantfinal` (no colon) pattern
- Tool call detection regex to properly match `assistantcommentary to=...` patterns
- Strip `analysis` prefix while preserving tool calls when present
- Streaming response handling for queued jobs
- Proper OpenAI streaming tool_calls format

## [1.0.0] - 2026-03-20

### Added

- Initial proxy implementation bridging OpenAI-compatible API to RunPod Serverless
- `simple_bridge.py` - Main FastAPI application
- Docker support with `Dockerfile` and `docker-compose.yml`
- Non-streaming and streaming chat completions (SSE format)
- Chain-of-thought prefix stripping (`analysis:`, `final:`, `assistantfinal`)
- Tool call preservation and parsing from multiple model output formats
- Job polling for queued requests
- `/v1/models` and `/v1/chat/completions` endpoints

### Configuration

- Default endpoint type: `ollama` (for RunPod Ollama endpoints) or `vllm` (for vLLM endpoints)
- Default model: `qwen3.5:27b`
- Default port: 8002
- Default timeout: 300 seconds
