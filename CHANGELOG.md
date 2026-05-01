# Changelog

All notable changes to the Serverless Proxy will be documented in this file.

## [Unreleased]

### Added

- **Per-Key Usage Filtering** - Added dashboard filtering for `Usage & Cost` and `Activity` by inbound API key so teams can measure requests, tokens, and cost per client key label.
- **Activity Key Attribution** - Activity rows now display the inbound API key label beneath the caller IP when a request was authenticated with a key.
- **Translated Ollama Show Metadata** - Added synthetic `/api/show` compatibility responses for non-Ollama virtual models and accepted both `model` and `name` request fields for broader Ollama client compatibility.

### Fixed

- **Chat Key Attribution Logging** - Chat traffic now persists inbound API key metadata into `recent_activity` and `request_usage`, so key-based filters work consistently across the primary request path.
- **Trusted Internal Key Attribution** - Requests from trusted internal CIDRs now still persist inbound API key metadata when a valid key is explicitly supplied, restoring Activity/Usage attribution for LAN callers using keys.
- **Ollama Non-Stream Translation** - `openai_oauth`-backed virtual models now synthesize non-stream Ollama responses for `/api/chat` and `/api/generate` instead of leaking upstream stream-only constraints.
- **Anthropic Non-Stream Translation** - `openai_oauth`-backed virtual models now synthesize non-stream Anthropic `/v1/messages` responses by buffering streamed upstream output.
- **OpenAI Completions Normalization** - `openai_oauth`-backed virtual models now synthesize non-stream `/v1/completions` responses and improved streamed completions output instead of returning only `[DONE]`.
- **OAuth Chat Streaming Refactor Runtime Fix** - Fixed runtime `NameError` regressions in `chat_completions` stream parsing path by rehydrating parser outputs (`full_content`, `full_reasoning`, `finish_reason`, `stats`, `stream_data`) after helper extraction.
- **OpenAI Chat Non-Stream OAuth Normalization** - `openai_oauth`-backed models now return standard non-stream JSON `chat.completion` bodies for `POST /v1/chat/completions` (`stream:false`) while keeping streaming behavior unchanged.
- **OpenAI Tool Round-Trip Completion Fix** - Fixed `openai_oauth` tool-result follow-up turns that could return empty final assistant content by preserving text emitted in OAuth Responses `response.output_item.added` / `response.output_text.added` events during SSE-to-chat normalization.
- **Anthropic Stop-Reason/Usage Mapping** - Normalized Anthropic non-stream synthesis to map finish reasons (`stop`, `tool_calls`, `length`) to Anthropic-compatible `stop_reason` and stable token usage fields.
- **Anthropic Tool Schema Mapping Compatibility** - `/v1/messages` tool definitions now normalize both Anthropic `input_schema` and OpenAI-style `parameters`, preserving tool argument generation for `openai_oauth` backends.
- **Anthropic Tool-Use Streaming Compatibility** - `openai_oauth` Anthropic streaming now emits Anthropic-style SSE events (`message_start`, `content_block_start`/`content_block_stop`, `message_delta`, `[DONE]`) instead of `[DONE]`-only output.
- **Anthropic Tool-Result Round-Trip Completion Fix** - Fixed follow-up `tool_result` turns that could end with empty assistant content by preserving/normalizing final text and adding fallback synthesis from tool-result payloads when upstream returns empty content.
- **Ollama Embeddings Cross-Host Compatibility** - `/api/embed` and `/api/embeddings` now resolve virtual models first (with alias fallback such as `name`/`name:latest`) so embedding models hosted on non-Ollama backends work through Ollama-compatible routes.
- **Ollama Embedding Error Hygiene** - Non-embedding models on Ollama embedding routes now return structured `unsupported_operation` compatibility responses instead of leaking upstream auth/capability errors.
- **Ollama Show Fallback for Backend-Loaded Models** - `/api/show` now supports passthrough for backend-loaded Ollama models not explicitly present in virtual model mappings.
- **Ollama /api/ps Discovery Merge** - `/api/ps` now merges embedding-capable virtual models into discovery output and can return synthetic discovery when an Ollama passthrough endpoint is unavailable.

### Documentation

- **README Key Analytics Notes** - Documented that Usage and Activity can be filtered by inbound API key, enabling per-team/per-client usage measurement from the dashboard.
- **README Multi-Protocol Notes** - Documented translated Ollama compatibility behavior, trusted-internal key attribution behavior, and OpenAI/Anthropic/Ollama protocol-surface compatibility expectations for `openai_oauth` models.
- **README Compatibility Validation Expansion** - Added expanded compatibility notes covering OAuth/OpenAI chat normalization, Anthropic non-stream synthesis, Ollama embeddings cross-host behavior, `/api/ps` discovery merge behavior, and end-to-end validation probes.
- **README OpenAI/Anthropic Coverage Clarification** - Added explicit notes that compatibility validation and fixes now include full OpenAI and Anthropic tool-call/tool-result round-trip coverage (streaming and non-streaming).

### Validation

- Verified Python syntax (`python3 -m py_compile simple_bridge.py`) after compatibility patches.
- Rebuilt/restarted proxy container (`docker compose up -d --build serverless-proxy`) during staged validation.
- Validated OpenAI OAuth chat path:
  - `POST /v1/chat/completions` (`stream:true`) returned valid SSE chunks and `[DONE]`.
  - `POST /v1/chat/completions` (`stream:false`) returned valid JSON `chat.completion` body.
  - Required tool-call stream probe returned correct tool-call deltas and `finish_reason:"tool_calls"`.
  - Full tool round-trip probe (dynamic `tool_call_id` from turn 1 carried into turn 2 `role:"tool"` message) returned non-empty final assistant content with `finish_reason:"stop"`.
- Validated Anthropic compatibility:
  - `POST /v1/messages` non-stream for `gpt-5.4-oauth` returned valid Anthropic message payload.
  - `POST /v1/messages` stream for `gpt-5.4-oauth` emitted Anthropic event sequence with `tool_use` content blocks and parsed input.
  - `POST /v1/messages` non-stream for `gemma4-e4b` remained non-regressed.
  - `POST /v1/messages` tool-use non-stream returned `tool_use` content blocks with parsed arguments.
  - Full Anthropic tool round-trip probe (dynamic `tool_use_id` from turn 1 carried into turn 2 `tool_result`) returned non-empty final assistant text.
- Validated Ollama embeddings compatibility:
  - `POST /api/embed` and `POST /api/embeddings` succeeded for `nomic-embed-text` and `qwen3-embedding`.
  - `POST /api/embed` batch input succeeded for `qwen3-embedding`.
  - `POST /api/embed` for non-embedding model (`gpt-5.4-oauth`) returned structured `unsupported_operation`.
  - `GET /api/ps` showed merged discovery including embedding-capable virtual model entries.

## [2.4.6] - 2026-04-23

### Added

- **Deployment Modes** - Added `deployment_mode` with `internal_only` and `internet_facing` behavior so the proxy can stay simple by default while supporting secure public exposure when explicitly enabled.
- **Trusted Internal CIDR Setting** - Added `trusted_internal_cidrs` setting in the dashboard so operators can configure trusted internal networks without code edits.
- **Inbound API Key Management** - Added dashboard-managed inbound API key support with labeled key generation, one-time secret display, enable/disable, and delete actions.
- **Dedicated API Keys Admin UI** - Added `API Keys` tab to the dashboard for managing inbound client access.
- **External Authentication Setup Guide** - Added `docs/external-authentication-setup.md` with step-by-step internet-facing setup, Caddy examples, trusted CIDR guidance, API key usage, and deployment recommendations.
- **Configurable Dashboard Timezone Setting** - Added a `Display Timezone` setting with `Server Local Time` default plus common world timezone options for dashboard/reporting views.

### Changed

- **Default Product Posture** - Preserved `internal_only` as the intended default mode so fresh installs remain easy for private/local users.
- **Public Runtime Hostname Guidance** - Documented the recommended dedicated hostname approach (for example `api.yourdomain.com`) instead of overloading an existing UI hostname/path space.
- **Admin Routing Requirements** - Extended Caddy routing documentation to include `/api/admin/inbound-api-keys*` so the new admin API reaches the correct backend.
- **Production Safety Settings** - Reduced live deployment debug posture to `debug_mode=basic` and `payload_audit_enabled=false` for safer internet-facing use.

### Fixed

- **Public/Internal Trust Separation** - Separated trusted proxy CIDRs from trusted internal client CIDRs so external traffic forwarded by Caddy is not incorrectly treated as internal.
- **Docker Internal Caller Compatibility** - Documented and validated the distinction between Docker gateway/proxy traffic and Docker-based internal client traffic when using `host.docker.internal`.
- **API Keys Page Routing** - Fixed admin API routing omission so `/api/admin/inbound-api-keys*` is served by the Flask/admin backend instead of falling through to HTML responses.
- **Timezone Rendering Consistency** - Fixed dashboard settings/activity timezone helpers and usage export formatting so the selected timezone is applied consistently without hardcoded EST offsets.

### Documentation

- **README Deployment Guidance** - Expanded README to explain default internal-only behavior, internet-facing usage, inbound API keys, reverse-proxy routing, and the new external authentication guide.
- **README Timezone Notes** - Documented the new configurable dashboard/reporting timezone behavior and `Server Local Time` default.
- **Rollout Checklist Updates** - Updated `docs/internet_facing_rollout_checklist.md` with verified Docker/Caddy behavior and rollout findings.

### Validation

- Verified Python syntax (`python3 -m py_compile simple_bridge.py`).
- Rebuilt and restarted the proxy container repeatedly during staged validation.
- Validated Caddy syntax for staged and live configs.
- Verified internal hostname-based access without inbound API key from trusted internal ranges.
- Verified public hostname routing, private `/health`, and dashboard API key generation/use flow.

## [2.4.5] - 2026-04-20

### Added

- **Failover Runtime Routing Visibility** - Activity logging now captures routed model/endpoint details after backend selection, including failover substitutions.
- **Virtual Model Failover Controls (UI/API)** - Added virtual model failover strategy configuration support (`backup`, `rotational`, `duplicate`) with target selection and optional per-model overrides.
- **Health + Cache Settings in Admin UI** - Added Settings controls for cache TTL and circuit/health tuning:
  - `cache_ttl_chat`
  - `cache_ttl_embeddings`
  - `health_check_interval`
  - `circuit_failure_threshold`
  - `circuit_failure_window`
  - `circuit_cooldown_seconds`
- **Endpoint Health Check URL** - Added optional endpoint-level `health_check_url` field in endpoint create/edit flows.
- **Virtual Model Cache Toggle** - Added `cache_enabled` UI/backend support to disable non-stream cache per virtual model.

### Fixed

- **Activity Endpoint Attribution Fallback** - When activity rows are logged without explicit endpoint metadata, endpoint id/name is now resolved from virtual model mapping.
- **Completions/Embeddings Cache Path Stability** - Fixed cache path issues in legacy completions and embeddings flows so cache lookup/store and usage/activity logging are consistent.
- **Admin UI Alignment Polish** - Fixed Virtual Models actions cell layout and Settings "Enable AI Queue Integration" checkbox alignment issues.

### Documentation

- **Failover + Cache Operations Guide** - Added `docs/failover-cache-operations.md` with complete behavior documentation, strategy semantics, and setting-by-setting reference.
- **README Feature Expansion** - Expanded failover/cache/health sections with retry/circuit semantics, settings reference table, activity visibility behavior, and link to operations guide.

### Validation

- Verified Python syntax (`python3 -m py_compile simple_bridge.py`).
- Rebuilt/restarted container (`docker compose build serverless-proxy && docker compose up -d serverless-proxy`).
- Ran smoke checks (`task test:models`, `task test:chat`, `task test:stream`).

## [2.4.4] - 2026-04-18

### Fixed

- **OpenAI OAuth Required Tool Choice** - Preserved `tool_choice="required"` for `openai_oauth` request mapping instead of downgrading to `auto`, so strict tool-call intent is forwarded to `/backend-api/codex/responses`.
- **OpenAI OAuth Responses Stream Tool Assembly** - Updated SSE translation for OAuth-backed Responses streams to correctly assemble tool calls from:
  - `response.output_item.added`
  - `response.function_call_arguments.delta`
  - `response.output_item.done`
- **Tool Call ID Correlation** - Added alias correlation between `item.id` and `call_id` in OAuth stream conversion to prevent duplicate/fragmented tool-call entries when argument deltas reference a different identifier.
- **Empty Tool Arguments Compatibility** - Normalized empty/blank tool arguments to valid JSON (`{}`) in OpenAI-compatible response shaping paths to improve downstream tool parser compatibility.
- **OAuth Model Discovery Fallback** - When OAuth-backed upstream model routes return non-success (common with scoped ChatGPT/Codex tokens), `/endpoints/<id>/models` now returns already-configured `virtual_models.actual_model` values for that endpoint so dashboard model selection remains usable.
- **OAuth Usage Estimation** - Added OpenAI OAuth token estimation fallback for streaming chat requests when upstream token counts are missing, so Usage & Cost tracking records input/output/total token estimates instead of zeros.
- **OAuth Secret Preservation on Edit** - Endpoint update routes now preserve existing `oauth_client_secret` and `oauth_refresh_token` when edit forms submit those fields blank, preventing accidental credential loss.
- **OAuth Token Error Diagnostics** - Token refresh rejection logs now include parsed OAuth error code/message snippets (for example `refresh_token_reused`) to speed up troubleshooting.

### Added

- **OAuth Tool Stream Diagnostics** - Added `[OAUTH_SSE]` debug telemetry for OAuth stream conversion with counters for event volume and tool assembly stages (`tool_added`, `tool_arg_delta`, `tool_done`, `unknown_delta`).
- **Usage UI Estimate Marking** - Usage dashboard now marks OAuth-backed model/token totals with `*` and shows a footer note clarifying that OpenAI OAuth token counts are estimates when upstream counts are unavailable.
- **Activity Tab (v1)** - Added a lightweight admin `Activity` tab with auto-refreshing recent request feed (route, model, IP, source, status, latency) and compact filters for quick operational scanning.
- **Recent Activity Storage/API** - Added `recent_activity` SQLite table, retention pruning, and admin API routes (`GET /api/admin/activity` and `GET /api/admin/endpoints/activity`) for metadata-only traffic visibility.
- **Activity Capture Coverage** - Added activity logging for chat, completions, embeddings, and model-list requests with normalized outcomes and latency.

### Documentation

- **README Updates** - Documented Activity tab behavior, activity admin APIs, and Caddy/reverse-proxy route requirements for OAuth admin paths (`/api/admin/oauth/*`) and activity paths.

### Validation

- Rebuilt and restarted proxy container (`docker compose up -d --build serverless-proxy`) and validated OAuth endpoint behavior with streaming probes:
  - baseline chat response (`finish_reason: stop`)
  - `tool_choice: auto` tool call with valid arguments payload
  - `tool_choice: required` enforced tool call with `finish_reason: tool_calls`
  - follow-up turn with tool result replay

## [2.4.3] - 2026-04-18

### Added

- **OpenAI OAuth Provider Type (`openai_oauth`)** - Added a new endpoint type for OAuth-backed OpenAI-compatible upstreams with OpenAI-first defaults.
- **OAuth Endpoint UI Fields** - Added endpoint form fields for OAuth configuration in the admin dashboard:
  - `oauth_enabled`
  - `oauth_grant_type` (`refresh_token`, `client_credentials`)
  - `oauth_token_url`, `oauth_client_id`, `oauth_client_secret`
  - `oauth_scope`, `oauth_refresh_token`
  - `oauth_token_request_format` (`json`, `form`)
  - `oauth_client_auth_method` (`client_secret_post`, `client_secret_basic`)
- **OAuth Runtime Auth Resolution** - Added endpoint auth precedence logic:
  1. OAuth bearer token (if configured)
  2. Static API key bearer token (fallback)
  3. No auth header
- **OAuth Token Lifecycle Handling** - Added token fetch/refresh support with in-memory access token cache and refresh-token rotation persistence to SQLite.
- **Encryption-Ready OAuth Schema** - Added additive `endpoints` columns to support future at-rest secret encryption rollout without destructive migrations.
- **OAuth Implementation Runbook** - Added `docs/oauth-encryption-secrets-storage.md` with migration, key-rotation, and recovery notes for future encryption enablement.

### Changed

- **Endpoint CRUD APIs** - Updated endpoint create/update routes to accept and persist OAuth fields on both admin API surfaces.
- **Backend Routing Compatibility** - `openai_oauth` endpoints route through OpenAI-compatible upstream paths (`/v1/chat/completions`, `/v1/models`, `/v1/embeddings`).
- **Endpoint Test/Model Fetch Auth** - Endpoint test and model discovery now use OAuth-first auth resolution when configured.

### Fixed

- **Endpoint Test Lookup** - Fixed endpoint lookup in `/endpoints/<id>/test` to avoid double `fetchone()` consumption.

### Documentation

- **README OAuth Docs** - Added detailed OAuth configuration and compatibility documentation.
- **README Upgrade Section** - Added explicit "How to Update Safely" upgrade/rollback workflow, including required DB backup step and container stop/restart commands.

## [2.4.2] - 2026-04-13

### Fixed

- **Qwen 3.5 Tool-Call Compatibility (DB patterns)** - Added documentation and operational guidance for handling Qwen 3.5 XML-style tool calls through `tool_patterns` table records instead of hardcoded parsing rules.
- **Truncated XML Tool Calls** - Documented tolerant pattern strategy for partial `<tool_call>` payloads so extraction still succeeds when responses are cut off before closing tags.

## [2.4.1] - 2026-04-11

### Added

- **OpenWebUI Endpoint Type** - New endpoint type for native OpenWebUI upstreams:
  - Added `openwebui` option to endpoint type dropdown in admin UI
  - Routes chat completions to `/api/chat/completions`
  - Routes embeddings to `/api/v1/embeddings`
  - Supports OpenWebUI model discovery routes (`/api/models`, `/api/v1/models`) in endpoint model fetch fallback chain

### Changed

- **Documentation Updates** - Added OpenWebUI-specific upstream guidance in `README.md`, `AGENTS.md`, and deployment docs.

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
