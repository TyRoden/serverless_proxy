# Serverless Proxy - RunPod to OpenAI Bridge

> **⚠️ DEPRECATED**: This file has been migrated to the Deft framework.
> 
> Content has been moved to:
> - `deft/PROJECT.md` - Project configuration, status, env vars, deployment
> - `deft/coding/serverless-proxy.md` - Proxy features (CoT stripping, tool calls, etc.)
> - `deft/interfaces/rest.md` - API endpoints and authentication integration
> 
> See `AGENTS.md` for the new configuration entry point.

## Overview

This proxy bridges OpenAI-compatible API requests to RunPod Serverless endpoints, making queue-based serverless LLM inference appear as a standard OpenAI API endpoint.

## Architecture

```
Client (OpenAI format) → Proxy (port 8002) → RunPod Serverless API → LLM Worker
```

## Current Status

- **Proxy Status**: Running in Docker
- **Container Name**: `serverless-proxy`
- **Port**: 8002
- **Model**: `project-system-ai`
- **Queue Mode**: AI Queue Master (USE_AI_QUEUE=true)
- **Queue URL**: `http://host.docker.internal:8102`

## Quick Start

```bash
cd /mnt/ai/serverless-proxy

# Start Docker
docker compose up -d

# Test the proxy
curl http://localhost:8002/v1/models | jq .
curl -X POST http://localhost:8002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "project-system-ai", "messages": [{"role": "user", "content": "Hello!"}]}' | jq .
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `RUNPOD_API_KEY` | RunPod API key (Bearer token) | (your API key) |
| `RUNPOD_ENDPOINT_ID` | RunPod serverless endpoint ID | (your endpoint ID) |
| `MODEL_NAME` | Model identifier | `project-system-ai` |
| `ENDPOINT_TYPE` | Endpoint format: `ollama` or `vllm` | `ollama` |
| `TIMEOUT` | Request timeout (seconds) | `300` |

### AI Queue Master Integration

When `USE_AI_QUEUE=true`, requests are routed through AI Queue Master instead of directly to RunPod.

| Variable | Description | Default |
|----------|-------------|---------|
| `USE_AI_QUEUE` | Enable AI Queue routing | `false` |
| `AI_QUEUE_URL` | AI Queue Master URL | `http://host.docker.internal:8102` |
| `AI_QUEUE_API_KEY` | API key for AI Queue | (required) |
| `AI_QUEUE_PRIORITY` | Priority: `HIGH`, `NORMAL`, `LOW` | `NORMAL` |
| `AI_QUEUE_SOURCE` | Source identifier | `runpod-proxy` |

### Authentication

The admin dashboard can be protected by authentication. When enabled, requests to admin endpoints must include a valid session.

#### Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `AUTH_ENABLED` | Enable/disable authentication | `true` |
| `AIMENU_URL` | Authentication service URL | `http://localhost:5000` |

**To disable authentication** (for fresh installs):
```bash
AUTH_ENABLED=false
```

**To enable authentication**:
```bash
AUTH_ENABLED=true
AIMENU_URL=http://your-auth-service:5000
```

#### Authentication Service Integration

The proxy calls your auth service's `/session/validate` endpoint to verify sessions.

**Request:**
```
GET /session/validate
Host: your-auth-service:5000
Cookie: session=your-session-token; ...
```

**Expected Response:**
```json
{
  "valid": true,
  "user": "username"
}
```

If `valid` is `true`, access is granted. If `false` or the endpoint returns an error, access is denied.

#### Implementing Custom Authentication

To use your own authentication system, implement a service that:

1. Exposes a `GET /session/validate` endpoint
2. Accepts session cookies from the request
3. Returns `{"valid": true, "user": "..."}` for valid sessions
4. Returns `{"valid": false}` for invalid/missing sessions

**Example minimal Flask implementation:**

```python
from flask import Flask, request, jsonify

app = Flask(__name__)

# In production, validate against your session store/database
VALID_SESSIONS = {
    "user1-token": "user1",
    "user2-token": "user2",
}

@app.route("/session/validate")
def validate_session():
    session_token = request.cookies.get("session") or request.headers.get("X-Session-Token")
    
    if session_token and session_token in VALID_SESSIONS:
        return jsonify({
            "valid": True,
            "user": VALID_SESSIONS[session_token]
        })
    
    return jsonify({"valid": False}), 401
```

**Key requirements:**
- Endpoint must accept cookies (standard `Cookie` header)
- Return JSON with `valid: true/false`
- Optionally include `user` field for auditing
- Response should be fast (5 second timeout)

#### Code Integration Points

If you want to modify the authentication logic, these functions in `simple_bridge.py` handle validation:

- `validate_session()` (line ~2768) - Flask routes
- `validate_session_fastapi()` (line ~2800) - FastAPI routes

Both check `AUTH_ENABLED` and call your auth service. Modify these functions to integrate different auth providers.

### Endpoint Types

**Ollama** (current):
- Converts OpenAI messages to prompt format
- Suitable for RunPod Ollama endpoints

**vLLM**:
- Passes messages directly with sampling_params
- Suitable for RunPod vLLM endpoints

## API Endpoints

### POST /v1/chat/completions

OpenAI-compatible chat completion endpoint. Supports both streaming and non-streaming responses.

**Request:**
```json
{
  "model": "qwen3.5:27b",
  "messages": [
    {"role": "user", "content": "Hello!"}
  ],
  "temperature": 0.7,
  "max_tokens": 256,
  "stream": false
}
```

**Response:**
```json
{
  "id": "sync-xxx-u1",
  "object": "chat.completion",
  "model": "qwen3.5:27b",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "..."},
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  }
}
```

### GET /v1/models

Lists available models.

**Response:**
```json
{
  "object": "list",
  "data": [{"id": "qwen3.5:27b", "object": "model"}]
}
```

## Features

### Chain-of-Thought Stripping
Strips reasoning prefixes from model outputs:
- `analysis:` prefix → removed
- `final:` content → extracted
- `assistantfinal` content → extracted

### Tool Call Preservation
Parses tool calls from various model output formats and converts to OpenAI function calling format.

**Supported Input Formats:**
- `<tool_call>{"name": "read", "arguments": {"filePath": "/path"}}</tool_call>` - Qwen3 Next
- `<tool_code>{"name": "read", "arguments": {"filePath": "/path"}}</tool_code>` - XML format
- `<tool_use code name="read">{"filePath": "/path"}</tool_use>` - Tool use tag
- Code fences: ```bash\nread /path\n```
- Bracket notation: `[Use the read_file tool to read /path]`

**Tool Name Mapping:**
- `read_file` → `read`, `write_file` → `write`, `edit_file` → `edit`
- `ls`/`glob` → `glob`, `grep`/`search` → `grep`, `bash` → `bash`

Works for both streaming and non-streaming responses.

### Auto-Fix Malformed Tool Calls

The proxy automatically fixes common tool call mistakes from models that don't follow the exact schema:

**Handled Patterns:**
- Models using `action`, `description`, or `query` fields instead of proper parameters
- Empty arguments with unknown tool names
- Wrong/missing tool names

**How it works:**
1. Detects intended tool by analyzing text in `action`/`description`/`query` fields
2. Parses natural language to extract proper parameters
3. Falls back to `task` tool with the original content as prompt if detection fails

This significantly improves compatibility with various model sizes (e.g., 80B models) that may output non-standard tool call formats.

### Job Polling
If RunPod returns `IN_QUEUE` status, the proxy polls for completion (up to 300 seconds).

## Anthropic API Compatibility

The proxy supports Anthropic API format (`/v1/messages`), enabling use with Claude Code and other Anthropic-compatible clients. This allows routing Claude Code through your serverless backend instead of directly to Anthropic.

### Configuration for Claude Code

Create or edit your Claude Code wrapper configuration (e.g., `/mnt/ai/claude-code-local/config/env.sh`):

```bash
export ANTHROPIC_BASE_URL=http://localhost:8002
export ANTHROPIC_AUTH_TOKEN=your-api-key
export CLAUDE_CODE_MODEL="your-model-name"
```

Then use your wrapper script:
```bash
/mnt/ai/claude-code-local/bin/claude-local -p "Your prompt here"
```

### Configuration for Other Anthropic-Compatible Clients

For clients that use the `/v1` base path:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8002/v1
export ANTHROPIC_AUTH_TOKEN=your-api-key
```

### Features Supported

| Feature | Status |
|---------|--------|
| `/v1/messages` endpoint | ✅ |
| `/v1/messages?beta=true` | ✅ |
| `/v1` HEAD health check | ✅ |
| System messages (top-level) | ✅ |
| System messages (in array) | ✅ |
| Content blocks (text, tool_use, tool_result) | ✅ |
| Tool calls | ✅ |
| Tool results | ✅ |
| Streaming responses | ✅ |
| Claude Code tool format | ✅ |

### Tool Format Conversion

The proxy automatically converts between formats:

**Claude Code sends:**
```json
{"name": "bash", "description": "Run shell commands", "parameters": {...}}
```

**Converted to OpenAI:**
```json
{"type": "function", "function": {"name": "bash", "description": "Run shell commands", "parameters": {...}}}
```

### Model Configuration

Ensure your virtual model in the admin dashboard is configured with:
- Correct endpoint type (e.g., `deepinfra` for DeepInfra)
- Streaming enabled (for best Claude Code experience)

## Files

| File | Purpose |
|------|---------|
| `simple_bridge.py` | Main proxy application (FastAPI) - supports both Ollama and vLLM |
| `docker-compose.yml` | Docker Compose configuration |
| `Dockerfile` | Container image definition |
| `CHANGELOG.md` | Version history |

## Docker Deployment

```bash
# Build and run
cd /mnt/ai/serverless-proxy
docker compose up -d --build

# Check status
docker ps | grep runpod

# View logs
docker logs -f runpod-serverless-proxy

# Restart
docker compose restart

# Stop
docker compose down
```

## OpenCode Integration

### OpenCode Config Location

```
/home/troden/.config/opencode/opencode.json
```

### Provider Configuration

```json
"runpod-serverless": {
  "name": "RunPod Serverless (Qwen3.5-27B)",
  "npm": "@ai-sdk/openai-compatible",
  "options": {
    "baseURL": "http://localhost:8002/v1"
  },
  "models": {
    "qwen3.5:27b": {
      "name": "Qwen 3.5 27B"
    }
  }
}
```

### Using with OpenCode

1. Ensure Docker proxy is running: `docker ps | grep runpod`
2. Start OpenCode
3. Select "RunPod Serverless (Qwen3.5-27B)" as your provider
4. Choose "Qwen 3.5 27B" as the model

## Testing

```bash
# Test models endpoint
curl http://localhost:8002/v1/models | jq .

# Test non-streaming chat completion
curl -X POST http://localhost:8002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen3.5:27b", "messages": [{"role": "user", "content": "Hello!"}]}' | jq .

# Test streaming chat completion
curl -X POST http://localhost:8002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen3.5:27b", "messages": [{"role": "user", "content": "Hi"}], "stream": true}'
```

## Troubleshooting

### Connection Errors
- Verify `RUNPOD_API_KEY` is valid
- Check `RUNPOD_ENDPOINT_ID` matches your RunPod endpoint
- Ensure RunPod endpoint is active: `curl -s -H "Authorization: Bearer $RUNPOD_API_KEY" https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/health | jq .`
- Check Docker is running: `docker ps | grep runpod`

### Timeout Issues
- Default client timeout: 300 seconds
- For long responses, increase `max_tokens` in request

### Docker Issues
```bash
# View container logs
docker logs runpod-serverless-proxy

# Restart container
docker restart runpod-serverless-proxy

# Rebuild if needed
docker compose up -d --build
```

### OpenCode Not Connecting
- Verify proxy is running on port 8002: `curl http://localhost:8002/v1/models`
- Check OpenCode config has correct baseURL: `http://localhost:8002/v1`
- Restart OpenCode after config changes
