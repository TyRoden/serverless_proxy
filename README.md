# Serverless Proxy - Universal LLM Gateway

A universal internal and external LLM gateway that bridges standard API requests to multiple backend providers (RunPod, Ollama, OpenAI-compatible APIs, Together AI, OAuth-backed providers, and more). Configure endpoints through the web admin UI, map virtual model names to actual backend models, and optionally expose the proxy safely to other tools with inbound API key generation. OAuth-backed endpoints can be shared across multiple tools, and the proxy handles token refresh centrally.

## Overview

```
Client (OpenAI format) → Serverless Proxy (port 8002) → Configured Backends
```

- **Universal**: Connect to any LLM backend (RunPod, Ollama, OpenAI, OAuth-based OpenAI-compatible providers, Together AI, etc.)
- **Virtual Models**: Map user-facing model names to actual backend models
- **Admin UI**: Configure endpoints and virtual models via web interface
- **Deployment Modes**: Keep installs simple with default `internal_only`, or switch to `internet_facing` when you want secure external access
- **Inbound API Keys**: Generate labeled API keys for external tools and systems
- **Per-Key Usage Analysis**: Filter Usage and Activity by inbound API key label to measure traffic and cost per client/team key
- **Tool-Call Compatibility**: Normalize misformatted model tool calls with DB-driven regex patterns
- **OpenAI-compatible**: Works with any OpenAI client library

## Internal-Only by Default

The proxy should remain easy to install and use for normal users.

Default behavior is:

- `deployment_mode = internal_only`
- no new inbound runtime API key requirement
- local and LAN installs continue to work simply

This default should remain in the repo so fresh installs stay easy.

If you want to expose the proxy publicly, you can switch to `internet_facing` mode later in the dashboard.

Detailed guide:

- `docs/external-authentication-setup.md`

## Quick Start

This guide walks you through getting the Serverless Proxy up and running in just a few minutes.

### Prerequisites

**Install Docker first** if you don't have it:
- **Docker Desktop** (Windows/Mac): https://www.docker.com/products/docker-desktop
- **Docker Engine** (Linux): https://docs.docker.com/engine/install/

### Step 1: Clone and Setup

```bash
# Clone the repository
git clone https://github.com/TyRoden/serverless_proxy.git
cd serverless_proxy

# Copy the example environment file
cp .env.example .env
```

### Step 2: Configure Your Environment

Open the `.env` file in a text editor and check these settings:

```bash
# Required: Set AUTH_ENABLED to false for first-time setup (no auth service needed yet)
AUTH_ENABLED=false

# Optional: If using Ollama locally, it should work out of the box
```

### Step 3: Start the Proxy

```bash
# Build and start the container
docker compose up -d --build

# Verify FastAPI is served via Uvicorn (required for API routes)
docker compose exec serverless-proxy sh -c "ps aux | grep uvicorn" || echo "WARNING: Uvicorn not running. Ensure serverless-proxy service uses 'uvicorn simple_bridge:app' in docker-compose.yml. See docs for details."
```

### Step 4: Configure in the Admin UI

1. Open your browser and go to: **http://localhost:5001/proxy-dashboard**
2. With `AUTH_ENABLED=false` for first-time setup, you'll see the admin dashboard without a login prompt.

#### Add an Endpoint
3. Click **+ Add Endpoint** under Endpoints
4. Fill in:
   - **Name**: Something like "My Ollama" or "RunPod Production"
   - **URL**: Your backend URL (e.g., `http://localhost:11434` for local Ollama, or your RunPod endpoint URL)
   - **API Key**: Your API key if required (leave blank for local Ollama)
   - **Type**: Select the type (`openwebui`, `openai`, `openai_oauth`, `ollama`, `runpod`, `anthropic`, `deepinfra`, etc.)
   - If you choose **OpenAI OAuth**, OAuth fields are shown and prefilled with OpenAI defaults
    - Click **Save**

#### Add a Virtual Model
5. Click **+ Add Virtual Model** under Virtual Models
6. Fill in:
   - **Name**: What you want to call it (e.g., `gpt-4`, `llama-production`)
   - **Endpoint**: Select the endpoint you just created
   - **Actual Model**: The actual model name on the backend (e.g., `gpt-4o`, `llama3:70b`)
   - Click **Save**

### Step 5: Use the Proxy

Your AI tools can now connect to the proxy:

| Service | URL |
|---------|-----|
| API Endpoint | `http://localhost:8002` |
| Admin UI | `http://localhost:5001/proxy-dashboard` |

**Example - Using with OpenWebUI or any OpenAI-compatible client:**

```
Base URL: http://localhost:8002/v1
API Key: any non-empty value for local/default internal-only setups, or the configured inbound API key in internet-facing mode
Model: the-virtual-model-name-you-created
```

## Internet-Facing Usage

When you want the proxy to act as a full internal and external gateway for your tools, switch to `internet_facing` mode and place it behind a reverse proxy such as Caddy.

Use cases:

- configure OAuth-backed upstreams once, then let multiple tools consume them through one proxy
- expose OpenAI-compatible, Anthropic-compatible, and Ollama-compatible routes from one hostname
- generate inbound API keys for external systems while keeping internal tools easy to use

Recommended public hostname example:

```text
https://api.yourdomain.com
```

Important:

- internal tools should also use the hostname through Caddy in internet-facing mode
- do not rely on raw `host.docker.internal:8002` as the preferred path in internet-facing mode

Full step-by-step guide:

- `docs/external-authentication-setup.md`

### Example external usage

```bash
curl https://api.yourdomain.com/v1/models \
  -H "Authorization: Bearer spk_your_generated_key"
```

```bash
curl -X POST https://api.yourdomain.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer spk_your_generated_key" \
  -d '{
    "model": "your-virtual-model-name",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Example internal usage in internet-facing mode

```bash
curl https://api.yourdomain.com/v1/models
```

This works without an inbound API key only when the caller is inside the configured trusted internal CIDR ranges.

**Example - Test with curl:**

```bash
curl http://localhost:8002/v1/models

curl -X POST http://localhost:8002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "your-virtual-model-name",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Troubleshooting

```bash
# Check if the proxy is running
curl http://localhost:8002/health

# View container logs
docker logs serverless-proxy

# Restart the container
docker restart serverless-proxy
```

## How to Update Safely

If you already have endpoints and virtual models configured, use this upgrade flow to avoid breaking your setup.

### 1) Back up the database first (required)

```bash
sqlite3 /mnt/ai/serverless-proxy/data/proxy.db \
  ".backup /mnt/ai/serverless-proxy/data/proxy-pre-upgrade-$(date +%Y%m%d-%H%M%S).db"
```

### 2) Stop the running container (recommended)

```bash
docker compose down
```

### 3) Pull the latest code

```bash
git pull
```

### 4) Rebuild and restart

```bash
docker compose up -d --build
```

### 5) Let migrations run automatically

On startup, the proxy runs additive schema migrations (new columns only). Existing endpoint rows are preserved.

### 6) Verify existing setup still works

```bash
# API health
curl http://localhost:8002/health

# List routed models
curl http://localhost:8002/v1/models

# Optional: test an existing virtual model
curl -X POST http://localhost:8002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "your-existing-virtual-model",
    "messages": [{"role": "user", "content": "ping"}]
  }'
```

### 7) (Optional) Start using OAuth endpoints

Existing endpoints continue to work unchanged. To use OAuth, add a new endpoint with type `openai_oauth` and configure OAuth fields.

### Rollback (if needed)

If something fails after upgrade:

1. Stop the container
2. Restore the DB backup
3. Restart with your prior image/commit

```bash
# Stop current container
docker compose down

# Example DB restore
cp /mnt/ai/serverless-proxy/data/proxy-pre-upgrade-YYYYMMDD-HHMMSS.db \
   /mnt/ai/serverless-proxy/data/proxy.db
docker compose up -d
```

## Configuration

## Health, Failover, and Cache

The proxy supports optional endpoint health polling, per-virtual-model failover, and non-streaming response cache.

For full operational details (runtime flow, strategy behavior, and every related setting), see:

- `docs/failover-cache-operations.md`

### Endpoint Health Polling

- Health polling runs only when at least one virtual model has failover configured.
- Polling is per endpoint and optional. Leave `Health Check URL` blank to disable active polling for that endpoint.
- Polling interval is configured in Settings (`health_check_interval`).

Accepted healthy/unhealthy responses for `health_check_url`:

- HTTP `2xx` with no JSON body → healthy
- HTTP `2xx` with JSON `{"healthy": true}` or `{"status": "ok"}` → healthy
- HTTP `2xx` with JSON `{"healthy": false}` or `{"status": "down"|"error"|"unhealthy"}` → unhealthy
- non-`2xx` response or network timeout/error → unhealthy/failure increment

Example health endpoints:

```bash
# simple
https://api.example.com/health

# Kubernetes style
https://api.example.com/healthz

# custom app route
https://api.example.com/internal/status
```

### Failover Configuration

Failover is disabled by default and only applies to virtual models with explicit failover settings.

- `backup`: try primary, then target list in order
- `rotational`: rotate through targets
- `duplicate`: automatically try other enabled virtual models with the same `actual_model`

Retry behavior and circuit controls:

- Failover retries only retryable upstream failures (`429`, `500`, `502`, `503`, `504`).
- Endpoints with open circuits are skipped.
- Circuit state is tracked per endpoint and updated by failure thresholds/cooldown.

Failover and circuit controls:

- Global defaults from Settings:
  - `circuit_failure_threshold`
  - `circuit_failure_window`
  - `circuit_cooldown_seconds`
- Per-virtual-model overrides (optional) in the failover form:
  - max attempts
  - failure threshold
  - cooldown seconds

### Non-Streaming Cache

- Cache is applied only to non-stream requests (`chat`, `completions`, and `embeddings`).
- Per virtual model, `Enable Non-Stream Cache` controls cache participation (`cache_enabled`).
- Cache is bypassed when request header includes `Cache-Control: no-store`.
- Tool-call responses are not cached.
- Error responses are not cached.
- TTLs are configured in Settings:
  - `cache_ttl_chat`
  - `cache_ttl_embeddings`

### Usage and Savings Metrics

Usage dashboard includes cache metrics:

- cache attempts
- cache hits
- cache hit rate
- estimated cache savings

### Settings Reference (Health/Failover/Cache)

| UI Section | Setting | Meaning |
|---|---|---|
| Settings | `Chat Cache TTL (seconds)` | TTL for non-stream chat/completions cache entries |
| Settings | `Embeddings Cache TTL (seconds)` | TTL for non-stream embeddings cache entries |
| Settings | `Health Check Interval (seconds)` | Poll interval for endpoint health URLs |
| Settings | `Circuit Failure Threshold` | Retryable failure count before circuit opens |
| Settings | `Circuit Failure Window (seconds)` | Time window for counting failures |
| Settings | `Circuit Cooldown (seconds)` | How long a circuit stays open before retry |
| Endpoint modal | `Health Check URL (optional)` | Optional custom health URL for endpoint polling |
| Virtual model modal | `Enable Non-Stream Cache` | Enables/disables cache for that model |
| Virtual model modal | `Enable Failover` | Enables/disables failover for that model |
| Virtual model modal | `Failover Strategy` | `backup`, `rotational`, or `duplicate` |
| Virtual model modal | `Failover Targets` | Target virtual models for `backup`/`rotational` |
| Virtual model modal | `Max Attempts` | Optional per-model cap on failover tries |
| Virtual model modal | `Failure Threshold` | Optional per-model override for circuit threshold |
| Virtual model modal | `Cooldown Seconds` | Optional per-model override for circuit cooldown |

### Activity Visibility

When failover substitutes a route, Activity shows:

- virtual model and routed model (`virtual_model -> actual_model`)
- routed endpoint
- failover note in the activity details row

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `API_PORT` | OpenAI-compatible API port | `8002` |
| `FLASK_PORT` | Admin UI port | `5001` |
| `DATABASE_PATH` | SQLite database path | `/data/proxy.db` |
| `TIMEOUT` | Request timeout (seconds) | `300` |
| `AUTH_ENABLED` | Enable admin authentication | `true` |
| `AIMENU_URL` | Auth service URL | `http://localhost:5000` |

### Authentication

By default, the admin dashboard requires authentication. See [docs/authentication.md](docs/authentication.md) for:
- How to disable authentication for fresh installs
- How to implement your own auth service
- Full API specification for the `/session/validate` endpoint

### Tool Pattern Matching (Patterns Tab)

The admin dashboard includes a **Patterns** tab for fixing model-specific tool call formats without editing code.

- Add/update/delete regex-based extraction patterns
- Control match priority (higher first)
- Map tool names and parameter keys into schema-compatible names
- Support malformed or non-standard XML/bracket/inline formats

See [docs/tool_patterns.md](docs/tool_patterns.md) for full details and examples.

#### Qwen 3.5 tool-call compatibility

Qwen 3.5 may emit XML-style tool calls instead of OpenAI JSON function calls, for example:

```xml
<tool_call>
<function=read>
<parameter=filePath>
/mnt/ai/ai-queue-master/app/config.py
</parameter>
</function>
</tool_call>
```

or:

```xml
<tool_call>
<function=bash>
<parameter=command>
ls -la /mnt/ai/ai-queue-master/app/
</parameter>
<parameter=description>
List app directory
</parameter>
</function>
</tool_call>
```

The proxy supports these via DB-backed `tool_patterns` records (not hardcoded), so compatibility can be adjusted from the Patterns UI/API without code edits.

### Docker Ports

| Port | Service |
|------|---------|
| `8002` | OpenAI-compatible API |
| `5001` | Admin UI API |

In internet-facing mode, the recommended public path is HTTPS through your reverse proxy hostname instead of direct raw port access.

## Admin Dashboard

Access the admin dashboard at `/proxy-dashboard`. Authentication is handled by the AI Menu System.

### Features

- **Endpoint Management**: Add, edit, delete backend endpoints
- **Virtual Model Mapping**: Map virtual model names to actual backend models
- **API Keys Tab**: Generate labeled inbound API keys for external clients
- **Deployment Mode Setting**: Switch between `internal_only` and `internet_facing`
- **Configurable Dashboard Timezone**: Choose `Server Local Time` or a common IANA timezone for dashboard/report exports
- **Activity Tab**: Recent request feed (route/model/IP/source/status/latency) with filters and auto-refresh
- **Per-Key Filtering**: Usage and Activity can be filtered by inbound API key so teams can isolate traffic by client key label
- **Patterns Tab**: Manage tool-call translation patterns in the UI
- **Model Discovery**: Fetch available models from endpoints
- **Enable/Disable**: Toggle endpoints and virtual models

### Settings Notes

- **Display Timezone** defaults to `Server Local Time`
- You can switch to common world timezones like `America/New_York`, `UTC`, `Europe/London`, or `Asia/Tokyo`
- Dashboard activity timestamps, API key timestamps, and usage CSV export timestamps use the selected timezone
- Usage/day grouping is based on the configured display timezone, so DST-aware zones like `America/New_York` are handled correctly
- Raw Unix timestamps stored in SQLite are unchanged

### Endpoint Configuration

Configure backend endpoints with:

- **Name**: Friendly identifier
- **URL**: Base URL (e.g., `http://localhost:11434`, `https://api.runpod.ai/v2/xxxx`)
- **API Key**: Authorization token (if required)
- **Type**: `openwebui`, `openai`, `openai_oauth`, `ollama`, `vllm`, `together`, `runpod`, `anthropic`, `deepinfra`, `queue`
- **Priority**: Higher priority endpoints are preferred
- **Enabled**: Enable/disable endpoint

### OAuth Endpoint Configuration (`openai_oauth`)

Use `openai_oauth` when your provider requires OAuth instead of a static API key.

Important reference guide:

- See `docs/openai-oauth-setup.md` for the full current setup flow, web OAuth instructions, Codex `auth.json` import path, model fallback guidance, token estimation notes, and reverse-proxy/Caddy requirements.

When selected in the dashboard, the form auto-fills OpenAI-compatible defaults:

- `url`: `https://chatgpt.com`
- `oauth_enabled`: `true`
- `oauth_grant_type`: `refresh_token`
- `oauth_token_url`: `https://auth.openai.com/oauth/token`
- `oauth_token_request_format`: `json`
- `oauth_client_auth_method`: `client_secret_post`

You can override all fields for non-OpenAI providers.

OAuth helper buttons in endpoint form:

- **Start Web OAuth** - Launches browser PKCE login (then paste redirect URL/code to complete)
- **Import from Codex auth.json** - Imports OAuth fields from local Codex/ChatGPT auth cache

#### Supported OAuth grant types

- `refresh_token`
- `client_credentials`

#### Supported token request compatibility options

- **Request format**: `json`, `form` (`application/x-www-form-urlencoded`)
- **Client auth method**: `client_secret_post`, `client_secret_basic`

#### Authentication precedence

For an endpoint, the proxy resolves auth in this order:

1. OAuth bearer token (if OAuth is enabled and fully configured)
2. Static API key bearer token (fallback)
3. No Authorization header

#### Token lifecycle and persistence

- Access tokens are cached in memory for runtime efficiency
- Refresh token rotations returned by provider are persisted to SQLite immediately
- `oauth_token_expires_at` metadata is persisted
- Behavior survives container restarts because durable OAuth state is stored in the DB

#### OpenAI OAuth routing behavior

`openai_oauth` is routed to the Codex/ChatGPT-style endpoint and payload by default:

- `POST /backend-api/codex/responses`
- Converts incoming OpenAI Chat Completions payload to OAuth-compatible responses payload
- Converts responses back to OpenAI-style chat completion output for clients

Model listing is best-effort for OAuth backends; some tokens/scopes may not expose `/models` routes.
If model discovery fails, set the model manually in your virtual model mapping (for this setup, `gpt-5.4` is confirmed to work).

#### Security and encryption-ready schema

OAuth and encryption-ready columns are provisioned in `endpoints` for future at-rest encryption rollout.
Current behavior remains plaintext secret storage (same model as existing `api_key` handling).

Detailed implementation and migration runbook:

- `docs/oauth-encryption-secrets-storage.md`
- `docs/openai-oauth-setup.md`

### Virtual Models

Map virtual model names to actual backend models:

- **Virtual Name**: What clients will request (e.g., `gpt-4`, `prod-llama`)
- **Endpoint**: Which backend to route to
- **Actual Model**: The model name on the backend (e.g., `gpt-4o`, `llama3:70b`)
- **Show Reasoning**: Toggle chain-of-thought display (for models like MiniMax that output thinking separately)
- **Cost per 1M Input Tokens ($)**: Price per 1M input tokens you send
- **Cost per 1M Output Tokens ($)**: Price per 1M output tokens you receive
- **Cost per 1M Cached Input Tokens ($)**: Discounted price per 1M cached input tokens (see provider pricing)
- **Cost per 1M Cached Output Tokens ($)**: Discounted price per 1M cached output tokens

### Cached Token Pricing

The proxy supports tracking and pricing for cached tokens:

- **How it works**: When you make repeated requests with similar prompts, providers cache the input tokens
- **Pricing**: Cached tokens are billed at a significantly discounted rate (typically 10-90% cheaper)
- **Configuration**: Enter your provider's cached token pricing in the virtual model settings
- **Tracking**: The Usage page displays cached token counts and costs separately
- **Supported Providers**: OpenAI, DeepInfra, and Anthropic APIs return cached token information

To configure:
1. Look up your provider's pricing (e.g., DeepInfra pricing page shows "$0.26 / $0.13 cached")
2. Enter the base price in "Cost per 1M Input Tokens"
3. Enter the cached price in "Cost per 1M Cached Input Tokens"

### Cost Tracking & Usage Monitoring

The proxy provides comprehensive cost tracking per model:

- **Per-model pricing**: Configure input/output/cached token rates for each virtual model
- **Usage dashboard**: View token counts, costs, and response times in the admin UI
- **Daily breakdown**: Track usage patterns over time
- **Per-key visibility**: Filter Usage and Activity by inbound API key label to measure requests, tokens, and cost per client/team key
- **Cost estimation**: Automatic calculation based on configured rates

Configure pricing per virtual model:
- **Input tokens**: Tokens sent in requests (prompt)
- **Output tokens**: Tokens received in responses (completion)
- **Cached tokens**: Discounted rate for cached input tokens (when providers support caching)

The Usage page shows:
- Total requests and token counts
- Input vs Output token breakdown
- Cached token counts and costs
- Average response times
- Cost per model and daily trends

### Activity Feed (Admin)

The admin dashboard includes an `Activity` tab for quick operational visibility.

- Recent traffic table (newest first)
- Default view: latest 100 rows, `/health` excluded
- Filter by status, model, IP, path, and inbound API key
- Auto-refresh every 10 seconds (toggleable)
- Metadata-only storage (no prompts/tool args/response bodies)

When inbound API keys are in use, Activity rows show the key label beneath the caller IP, and both `Activity` and `Usage & Cost` support filtering by key. This lets development teams measure traffic, tokens, and spend per client key while preserving the default all-traffic view for installs that do not use keys.

## API Endpoints

### OpenAI-Compatible API (port 8002)

```bash
# List models
curl http://localhost:8002/v1/models

# Chat completions
curl -X POST http://localhost:8002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "my-virtual-model", "messages": [{"role": "user", "content": "Hello!"}]}'
```

#### Supported Endpoints

- `GET /v1/models` - List available models (virtual models + default)
- `POST /v1/chat/completions` - Chat completions
- `POST /v1/completions` - Text completions
- `POST /v1/embeddings` - Embeddings
- `GET /health` - Health check

### Ollama Compatibility API (port 8002)

The proxy now supports both Ollama-native routes and OpenAI-compatible routes when a virtual model maps to an `ollama` endpoint.

#### Runtime inference routes (Phase 1)

- `GET /api/tags`
- `GET /api/version`
- `POST /api/chat`
- `POST /api/generate`
- `POST /api/embed`
- `POST /api/embeddings` (alias)

#### OpenAI client compatibility aliases

- `POST /chat/completions` -> OpenAI handler alias
- `POST /api/chat/completions` -> OpenAI handler alias
- `GET /models`, `GET /api/models`, `GET /api/v1/models` -> model listing aliases
- `POST /embeddings`, `POST /api/v1/embeddings` -> embeddings aliases

#### Full native surface compatibility (Phase 2)

For Ollama-backed virtual models, these routes are forwarded to the configured Ollama endpoint. For non-Ollama backends, the proxy translates where practical and otherwise returns an explicit compatibility response:

- `POST /api/show`
- `GET|POST /api/ps`
- `POST /api/pull`
- `POST /api/push`
- `POST /api/create`
- `POST /api/copy`
- `DELETE|POST /api/delete`
- `HEAD /api/blobs/{digest}`
- `POST /api/blobs/{digest}`

#### Behavior details

- **Upstream selection for ollama virtual models**:
  - tries Ollama OpenAI-compatible upstream `POST /v1/chat/completions` first
  - falls back to native `POST /api/chat` if upstream returns `404/405`
- **Translated native compatibility for non-Ollama backends**:
  - `/api/chat` and `/api/generate` preserve Ollama-style request/response shapes while translating upstream to the selected backend as faithfully as possible
  - `/api/show` returns a synthetic metadata response for non-Ollama virtual models so Ollama-oriented clients can still inspect model details
  - `/api/show` accepts either `model` or `name` in the request body for improved client compatibility
  - some model-management routes such as pull/push/create/copy/delete/blob operations remain true Ollama-surface operations and are only meaningful for real Ollama upstreams
- **Message normalization**:
  - converts OpenAI block-style `messages[].content` arrays to Ollama-safe string content for native `/api/chat`
- **Streaming**:
  - native Ollama routes stream as NDJSON (`application/x-ndjson`)
  - OpenAI routes stream as SSE (`text/event-stream`)
- **Embeddings**:
  - for Ollama endpoints, proxy tries `/api/embed` then falls back to `/api/embeddings`
  - capability errors from upstream are returned as non-200 responses (for example `501` if model lacks embedding support)

#### Verified translated behavior for stream-required upstreams

- For `openai_oauth`-backed virtual models such as `gpt-5.4-oauth`, the proxy now buffers upstream streaming internally and synthesizes non-stream responses for:
  - Ollama `POST /api/chat`
  - Ollama `POST /api/generate`
  - Anthropic `POST /v1/messages`
  - OpenAI `POST /v1/chat/completions`
  - OpenAI `POST /v1/completions`

#### Embeddings compatibility behavior

- `POST /api/embed` and `POST /api/embeddings` now resolve virtual models first (including `name` and `name:latest` alias forms), so embedding backends can live on non-Ollama hosts.
- `POST /api/embed` accepts both `input` and `prompt` fields for broader Ollama-client compatibility.
- If a model is not mapped in virtual models, embedding routes fall back to Ollama passthrough when an Ollama endpoint exists.
- If no mapping and no Ollama endpoint are available, embedding routes return a structured `model_not_found` compatibility error.
- Non-embedding models now return structured `unsupported_operation` errors on Ollama embedding routes instead of leaking upstream auth/capability noise.
- `POST /api/show` now supports passthrough for backend-loaded Ollama models not explicitly present in virtual model mappings.
- `GET|POST /api/ps` now merges embedding-capable virtual models into model discovery, and returns synthetic model discovery when Ollama passthrough is unavailable.

#### Compatibility validation (latest)

Validated on live proxy with inbound API key and current `main` runtime:

- **OpenAI-compatible**
  - `POST /v1/chat/completions` with `gpt-5.4-oauth`:
    - `stream:true` returns valid SSE chunks + `[DONE]`
    - `stream:false` returns valid JSON `chat.completion` body
  - `POST /v1/chat/completions` with required tool call (`tool_choice:"required"`) returns streamed tool-call deltas and `finish_reason:"tool_calls"`
  - `POST /v1/embeddings` with `qwen3-embedding` returns valid embedding vector payload
- **Anthropic-compatible**
  - `POST /v1/messages` with `gpt-5.4-oauth`:
    - `stream:false` returns valid Anthropic message shape (`type:"message"`, `content`, `stop_reason`, `usage`)
    - `stream:true` continues to emit Anthropic event stream correctly
  - `POST /v1/messages` with tools on `gpt-5.4-oauth` returns `tool_use` blocks with parsed tool input and `stop_reason:"tool_use"`
  - `POST /v1/messages` with `gemma4-e4b` non-stream remains non-regressed (`200`)
- **Ollama-compatible**
  - `POST /api/embed` with `nomic-embed-text` and alias forms returns embeddings (`200`)
  - `POST /api/embeddings` with `prompt` field returns single-vector shape (`{"embedding":[...]}`)
  - `POST /api/embed` with non-embedding model (`gpt-5.4-oauth`) returns structured `unsupported_operation` error (`400`)
  - `GET /api/ps` includes discovered Ollama models and merged embedding-capable virtual models (for example `qwen3-embedding`)
  - `POST /api/embed` with `qwen3-embedding` works for single and batch inputs

#### Activity and key attribution behavior

- In `internet_facing` mode, trusted internal callers can still access the proxy without an inbound API key.
- If a trusted-internal caller does send a valid inbound API key, the proxy now records that key label/id in Activity and Usage so the dashboard can attribute the request correctly.

#### Diagnostics for compatibility debugging

- Request/response diagnostics under debug mode:
  - `[HTTP_IN]`, `[HTTP_OUT]`, `[HTTP_ERR]`
- Ollama upstream payload diagnostics:
  - `[OLLAMA_400] ... body=... payload=...`
- Response marker header:
  - `X-Proxy: serverless-proxy`

#### Conformance and roadmap docs

- Runtime conformance smoke script: `scripts/ollama_runtime_conformance.sh`
- Full-surface conformance script: `scripts/ollama_full_surface_conformance.sh`
- Compatibility roadmap and checklist: `docs/ollama-compatibility-roadmap.md`
- Version-dependent compatibility notes: `docs/ollama-version-notes.md`

Run conformance checks:

```bash
OLLAMA_PROXY_BASE_URL=http://localhost:8002 \
OLLAMA_TEST_MODEL=gemma4:26b \
./scripts/ollama_runtime_conformance.sh

# Full-surface non-mutating checks
./scripts/ollama_full_surface_conformance.sh

# Full-surface including mutating lifecycle checks
OLLAMA_RUN_MUTATING=1 ./scripts/ollama_full_surface_conformance.sh
```

### Admin API

Admin endpoints are split between the Flask admin service on `5001` and FastAPI on `8002`. The table below lists the routes, and the reverse-proxy section later in this README shows which backend serves each path.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/admin/endpoints` | GET, POST | List/create endpoints |
| `/api/admin/activity` | GET | Recent activity feed (FastAPI) |
| `/api/admin/endpoints/activity` | GET | Recent activity feed (Flask/admin-compatible alias) |
| `/endpoints` | GET, POST | Manage endpoints |
| `/endpoints/<id>` | PUT | Update endpoint |
| `/endpoints/<id>/delete` | GET, DELETE | Delete endpoint |
| `/endpoints/<id>/test` | POST | Test endpoint connection |
| `/endpoints/<id>/models` | GET | Fetch available models |
| `/api/admin/oauth/openai/start-web-auth` | POST | Start OpenAI web OAuth flow |
| `/api/admin/oauth/openai/complete-web-auth` | POST | Complete OpenAI web OAuth with pasted URL/code |
| `/api/admin/oauth/openai/import-codex` | POST | Import OAuth fields from Codex `auth.json` |
| `/api/admin/oauth/openai/auth-result` | GET | Poll OAuth popup result by state |
| `/api/admin/oauth/openai/callback` | GET | OAuth callback handler |
| `/api/admin/inbound-api-keys` | GET, POST | List/create inbound API keys |
| `/api/admin/inbound-api-keys/<id>` | PUT, DELETE | Update/delete inbound API keys |
| `/api/admin/virtual-models` | GET | List virtual models |
| `/virtual-models` | POST | Create virtual model |
| `/virtual-models/<id>` | PUT | Update virtual model |
| `/virtual-models/<id>/delete` | GET, DELETE | Delete virtual model |
| `/api/admin/tool-patterns` | GET, POST | List/create tool patterns |
| `/api/admin/tool-patterns/<id>` | PUT, DELETE | Update/delete tool pattern |

OAuth fields are accepted by endpoint create/update APIs (`/endpoints`, `/endpoints/<id>`, `/api/admin/endpoints`, `/api/admin/endpoints/<id>`).

Common OAuth payload fields:

- `oauth_enabled` (bool)
- `oauth_grant_type` (`refresh_token` or `client_credentials`)
- `oauth_token_url`
- `oauth_client_id`
- `oauth_client_secret`
- `oauth_scope`
- `oauth_refresh_token`
- `oauth_token_request_format` (`json` or `form`)
- `oauth_client_auth_method` (`client_secret_post` or `client_secret_basic`)

### Reverse Proxy Routing Notes (Caddy)

If you front the dashboard with Caddy (or another reverse proxy), route admin paths to the correct backend services before catch-all `/api/*` rules.

- `127.0.0.1:5001` (Flask/admin):
  - `/api/admin/endpoints*`
  - `/api/admin/inbound-api-keys*`
  - `/api/admin/virtual-models*`
  - `/api/admin/oauth/*`
  - `/api/admin/endpoints/activity`
  - `/endpoints*`, `/virtual-models*`
- `127.0.0.1:8002` (FastAPI/API):
  - `/api/admin/usage*`
  - `/api/admin/activity`

Example Caddy matchers:

```caddy
@proxy-api-usage path /api/admin/usage*
handle @proxy-api-usage {
    reverse_proxy 127.0.0.1:8002
}

@proxy-api-oauth path /api/admin/oauth/*
handle @proxy-api-oauth {
    reverse_proxy 127.0.0.1:5001
}

@proxy-api path /api/admin/endpoints* /api/admin/inbound-api-keys* /api/admin/virtual-models* /api/admin/endpoints/activity /endpoints* /virtual-models*
handle @proxy-api {
    reverse_proxy 127.0.0.1:5001
}
```

For a full public-runtime Caddy example with route-by-route explanations, see:

- `docs/external-authentication-setup.md`

## Backend Types

| Type | Description |
|------|-------------|
| `openwebui` | OpenWebUI API (`/api/chat/completions`, `/api/models`, `/api/v1/embeddings`) |
| `openai` | OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`, `/v1/embeddings`) |
| `openai_oauth` | OpenAI OAuth/Codex style backend (`/backend-api/codex/responses`) with OpenAI chat-completions request/response translation |
| `ollama` | Ollama API (native `/api/*` + OpenAI-compatible `/v1/*` bridging) |
| `vllm` | vLLM API |
| `together` | Together AI |
| `runpod` | RunPod Serverless |
| `anthropic` | Anthropic Messages API (`/v1/messages`) |
| `deepinfra` | DeepInfra OpenAI-compatible API (`/v1/openai/chat/completions`) |
| `queue` | AI Queue endpoint (`/v1/chat/completions`, `/v1/embeddings`) |

## AI Queue Integration (Optional)

Route requests through AI Queue Master for priority queuing and request tracking.

```bash
USE_AI_QUEUE=true
AI_QUEUE_URL=http://host.docker.internal:8102
AI_QUEUE_API_KEY=your_queue_api_key
AI_QUEUE_PRIORITY=NORMAL
```

## Supporting AI Coding Assistants (Claude Code, OpenCode, Cursor, etc.)

AI coding assistants require specific configurations to work properly. The proxy includes special handling to ensure compatibility:

### Proxy Adjustments for AI Coding Assistants

- **Tool call normalization** — Automatically fixes malformed tool calls from models
- **System prompt preservation** — Maintains context across code generation sessions
- **Streaming optimization** — Real-time tool execution for interactive coding
- **Response format conversion** — Ensures OpenAI-compatible format for tool results
- **Error handling** — Graceful fallbacks when models produce unexpected output
- **Claude Code compatibility** — Claude Code works best with OpenAI-compatible endpoints through the proxy, even when using non-OpenAI models

### Model Requirements

Use models with strong tool-calling capabilities. Recommended:
- **Qwen series** (e.g., Qwen3-80B, Qwen3-Coder) - Excellent tool calling
- **Claude 3.5+** - Native tool support via Anthropic API
- **DeepSeek-V3** - Good tool calling performance

### Endpoint Configuration

For best results with coding assistants:
1. Use **OpenAI-compatible** or **DeepInfra** endpoint types
2. Enable **streaming** for real-time tool execution
3. Configure adequate **max_tokens** (8192-128000 for code generation)

### Virtual Model Setup

When creating virtual models for coding assistants:
- Set appropriate `max_tokens` to allow long code outputs
- Use models that support tool calls (check provider docs)
- For Anthropic models, ensure endpoint type is set to `anthropic`

### Troubleshooting

**Tools not executing:**
- Check model supports tool calls (not all models do)
- Verify streaming is enabled
- Check response format in logs

**Code execution errors:**
- Verify model output is valid JSON for tool calls
- Check custom headers if required by your setup

```bash
# View container logs
docker logs serverless-proxy

# Restart container
docker restart serverless-proxy

# Check health
curl http://localhost:8002/health
```

## Project Structure

```
.
├── simple_bridge.py          # Main proxy application (FastAPI + Flask)
├── docker-compose.yml        # Docker Compose configuration
├── Dockerfile                # Container image definition
├── requirements.txt          # Python dependencies
├── templates/
│   └── admin_dashboard.html # Admin UI (static HTML)
├── .env.example              # Environment variable template
├── README.md
└── CHANGELOG.md
```

## License

MIT License — see [LICENSE.md](LICENSE.md)

## Acknowledgments

Based on RunPod serverless API patterns. Extended with virtual model configuration, Anthropic API compatibility, and admin UI capabilities.
