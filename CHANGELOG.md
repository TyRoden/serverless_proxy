# Changelog

All notable changes to the Serverless Proxy will be documented in this file.

## [2.4.0] - 2026-04-05

### Added

- **Cached Token Pricing Support** - New configurable pricing for cached tokens:
  - `cost_per_1m_tokens_in_cached` - Price per 1M cached input tokens
  - `cost_per_1m_tokens_out_cached` - Price per 1M cached output tokens
  - Automatically tracks cached tokens from OpenAI/DeepInfra and Anthropic APIs
  - Database columns: `cached_input_tokens`, `cache_creation_tokens`, `cached_cost_estimate`
  - Usage page now displays cached token counts and cached costs in KPI cards and daily breakdown table

- **Anthropic Endpoint Support** - New endpoint type for direct Anthropic API calls:
  - Added "Anthropic" option to endpoint type dropdown
  - Backend routing to `/v1/messages` endpoint
  - Automatic request transformation for Anthropic format (system prompt handling)
  - Supports both cached and non-cached token pricing

- **Response Time Tracking** - Fixed response time tracking for streaming requests:
  - Streaming requests now properly record response_time_ms
  - Average response time displays correctly in Usage page

### Changed

- **Token Pricing Units** - Changed from per-1K to per-1M pricing:
  - Field names changed from `cost_per_1k_tokens` to `cost_per_1m_tokens`
  - Dashboard label updated to show "($/1M)"
  - Migration added to preserve existing cost values
  - Database columns renamed: `cost_per_1m_tokens_in`, `cost_per_1m_tokens_out`

- **max_tokens Limit** - Increased maximum tokens limit to 1,000,000 for supporting longer context windows

## [2.3.0] - 2026-04-01

### Added

- **Virtual Model Defaults** - New configurable defaults per virtual model:
  - `max_tokens` - Default max tokens (fallback when client doesn't specify)
  - `temperature` - Default temperature
  - `top_p` - Default top_p
  - `system_prompt` - System prompt prepended to all requests
- **Show Reasoning Toggle** - New `show_reasoning` option for virtual models:
  - Controls whether chain-of-thought/reasoning content is included in responses
  - Useful for models like MiniMax M2.5 that output thinking separately
  - Toggle available in dashboard edit form
  - Database column: `show_reasoning` (1=enabled, 0=disabled)

### Fixed

- **MiniMax Reasoning Stripping** - Fixed issue where MiniMax models leak thinking into responses:
  - MiniMax sends reasoning content separately from actual response
  - When tool calls are present, thinking is now stripped from response
  - Works for both streaming and non-streaming responses
  - Streaming: Clears text_content when tool calls detected
  - Non-streaming: Doesn't use reasoning_content as fallback when tool_calls present

## [2.2.1] - 2026-03-29

### Fixed

- **Tool Call Auto-Fix** - Enhanced robustness when models output malformed tool calls. The proxy now automatically fixes common mistakes:

  **Handled Patterns:**
  - Models using `action` field instead of proper parameters (e.g., `{"action": "searching for..."}`)
  - Models using `description` or `query` fields
  - Empty arguments with unknown tool names
  - Wrong tool names in the function call

  **Auto-Detection:**
  - Analyzes the text to detect intended tool (grep, read, glob, bash, task)
  - Parses natural language actions to proper parameters
  - Falls back to `task` tool with the original content as prompt

  **This fix significantly improves compatibility with models like Qwen3-80B** that may output tool calls in non-standard formats.

## [2.2.0] - 2026-03-28

### Added

- **Anthropic API Compatibility** - The proxy now supports Anthropic API format (`/v1/messages`), enabling use with Claude Code and other Anthropic-compatible clients:

  **Supported Features:**
  - `/v1/messages` endpoint (Anthropic format)
  - `/v1/messages?beta=true` endpoint (with beta features)
  - `/v1` HEAD health check
  - System messages (both top-level `system` field and in messages array)
  - Content blocks (text, tool_use, tool_result)
  - Tool calls and tool results
  - Streaming responses (SSE format)
  - Claude Code tool format (`{"name": "...", "description": "...", "parameters": {...}}`)

  **Configuration for Claude Code:**
  ```bash
  export ANTHROPIC_BASE_URL=http://localhost:8002
  export ANTHROPIC_AUTH_TOKEN=your-api-key
  export CLAUDE_CODE_MODEL="your-model-name"
  ```

  **Configuration for Other Clients:**
  ```bash
  export ANTHROPIC_BASE_URL=http://localhost:8002/v1
  export ANTHROPIC_AUTH_TOKEN=your-api-key
  ```

- **Tool Call Extraction** - The proxy now extracts tool calls from various model output formats and converts them to OpenAI function calling format:

  **Supported Input Formats:**
  - `<tool_call>{"name": "read", "arguments": {"filePath": "/path"}}</tool_call>` - Qwen3 Next format
  - `<tool_code>{"name": "read", "arguments": {"filePath": "/path"}}</tool_code>` - Generic XML format
  - `<tool_use code name="read">{"filePath": "/path"}</tool_use>` - Tool use tag format
  - Code fences: ```bash\nread /etc/hostname\n``` or ```tool_call\n{...}```
  - Bracket notation: `[Use the read_file tool to read /mnt/ai/file.md]`
  - Inline patterns: `commentary to=read {"filePath": "/path"}`

  **Tool Name Mapping:**
  - `read_file` → `read`
  - `write_file` → `write`
  - `edit_file` → `edit`
  - `ls` / `glob` → `glob`
  - `grep` / `search` → `grep`
  - `bash` → `bash`

  **Output:**
  - Converts all formats to OpenAI function calling format
  - Extracts arguments and wraps in proper JSON
  - Works for both streaming and non-streaming responses

- **Tool Call Extraction for Streaming** - Extract and process tool calls from streaming responses:
  - Accumulates streaming chunks to extract tool call content
  - Parses `<tool_call>`, `<tool_code>`, `<tool_use>` formats
  - Handles code fence formats (```bash\nread /path```)
  - Converts extracted tool calls to proper OpenAI format
  - Fixed streaming finish_reason to show "tool_calls" when applicable

- **Extended Tool Call Format Support** - Added support for additional model output formats:
  - `<tool_call>{"name": "...", "arguments": {...}}</tool_call>` - Qwen3 Next format
  - `lang tool_name argument` format (e.g., "bash read /etc/hostname")

## [2.1.0] - 2026-03-28

### Added

- **Token & Cost Tracking** - Track token usage and compare costs against paid services:
  - Logs prompt tokens (input), completion tokens (output) separately
  - Tracks response time per request
  - Per-virtual-model cost configuration (different rates for input/output tokens)
  - Cost calculated per 1M tokens (industry standard)
  - Usage API endpoints:
    - `GET /api/admin/usage` - Summary with daily breakdown
    - `GET /api/admin/usage/by_model` - Per-model breakdown
    - `GET /api/admin/usage/by_endpoint` - Per-endpoint breakdown
    - `POST /api/admin/usage/export` - CSV export
    - `GET /api/admin/usage/embeddings` - Embedding usage
  - Admin UI "Usage & Cost" tab with:
    - Tab-based navigation (Dashboard / Usage & Cost)
    - Summary cards (requests, tokens, cost, avg response time)
    - Date range filtering (24h, 7d, 30d, custom)
    - Virtual model filter
    - Daily breakdown table
    - CSV export button

- **Serverless Proxy Dashboard** - Web admin UI at `/proxy-dashboard`:
  - Client-side session validation with cookie forwarding
  - Endpoints management (add, edit, delete, test, fetch models)
  - Virtual models management with cost configuration
  - Proper tab navigation

### Fixed

- Session validation for FastAPI endpoints (was failing outside request context)
- Caddyfile routing for `/session*` and `/api/admin/usage*`
- Credentials forwarding for fetch calls (`credentials: 'same-origin'`)
- JavaScript syntax error breaking function definitions
- Tab layout - Usage section now properly toggles as tab
- Navigation styling - now looks like proper tabs
- Cost calculation changed from per-1K to per-1M tokens (industry standard)

### Database Changes

- New `request_usage` table - tracks chat/completion usage
- New `embedding_usage` table - tracks embedding usage separately
- Added `cost_per_1k_tokens_in` and `cost_per_1k_tokens_out` to virtual_models
- Automatic migration for existing databases

## [2.0.0] - 2026-03-28

### Added

- **Multi-Backend Support** - Connect to any LLM backend:
  - RunPod Serverless
  - Ollama
  - OpenAI-compatible APIs
  - Together AI
  - vLLM
  - Extensible backend architecture for easy addition of new providers

- **Web Admin UI** - Configure endpoints and virtual models via browser:
  - Access at `/proxy-dashboard`
  - Endpoint management (add, edit, delete, test)
  - Virtual model mapping
  - Model discovery from endpoints
  - Enable/disable toggles
  - Integrated with AI Menu System authentication

- **Virtual Model Mapping** - Map user-facing model names to actual backend models:
  - Virtual names exposed via `/v1/models`
  - Requests routed to correct backend based on virtual model
  - Supports any backend type

- **Admin API** - RESTful API for endpoint and virtual model management:
  - CRUD operations for endpoints
  - CRUD operations for virtual models
  - Endpoint testing and model discovery endpoints
  - Session-based authentication via AI Menu System

### Changed

- Renamed from "RunPod Serverless Proxy" to "Serverless Proxy" (universal)
- Default port: 8002 (API), 5001 (Admin API)
- SQLite database at `/data/proxy.db`
- Admin UI served as static HTML with JavaScript

### Files Changed

- simple_bridge.py: Added Flask admin routes, backend abstraction, virtual model routing
- templates/admin_dashboard.html: New admin UI
- requirements.txt: Added flask, flask-cors, jinja2

## [1.5.1] - 2026-03-28

### Changed

- **Major refactor**: Unified code with backend abstraction layer
  - Removed duplicate code paths (~460 lines removed)
  - Chat completions now use `BACKEND.chat_completion()` uniformly
  - Easier to add new backends
- Default model: `project-system-ai`

### Files Changed

- simple_bridge.py: 1500+ → 1036 lines (net -660 lines including additions)

### Added

- **Backend Abstraction Layer** - New plugin-based architecture for easy extension:
  - `LLMBackend` abstract base class for defining new backends
  - `AIQueueBackend` - Routes requests through AI Queue Master
  - `RunPodBackend` - Routes directly to RunPod Serverless
  - `get_backend()` factory function for dynamic backend selection
  - Easy to add new backends (Together AI, Anyscale, etc.) by extending `LLMBackend`
- **Health Endpoint** (`/health`) - Returns backend health status for container orchestration
- **OpenAI Parameter Support** - Added support for all common OpenAI parameters:
  - `stop` - Stop sequences
  - `presence_penalty` - Presence penalty
  - `frequency_penalty` - Frequency penalty
  - `logit_bias` - Token probability modification
  - `user` - End-user ID (passed as X-User-ID header to queue)
  - `tool_choice` - Force specific tool or auto
  - `response_format` - JSON/JSON schema mode
  - `seed` - Reproducible outputs
  - `parallel_tool_calls` - Enable/disable parallel tool execution
- **OpenAI Error Format** - All error responses now return proper OpenAI format:
  - `{"error": {"message": "...", "type": "...", "param": "...", "code": "..."}}`
  - Proper HTTP status codes (400, 401, 403, 404, 408, 422, 429, 500, 503)

### Changed

- Default model changed to `project-system-ai`
- AI Queue enabled by default (`USE_AI_QUEUE=true`)
- Configuration now documented in UPGRADES.md for future planning

## [1.4.1] - 2026-03-22

### Added

- `X-Model` header in AI Queue requests for model identification

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
