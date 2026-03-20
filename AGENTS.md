# Serverless Proxy - RunPod to OpenAI Bridge

## Overview

This proxy bridges OpenAI-compatible API requests to RunPod Serverless endpoints, making queue-based serverless LLM inference appear as a standard OpenAI API endpoint.

## Architecture

```
Client (OpenAI format) → Proxy (port 8002) → RunPod Serverless API → LLM Worker
```

## Current Status

- **Proxy Status**: Running in Docker
- **Container Name**: `runpod-serverless-proxy`
- **Port**: 8002
- **Model**: `qwen3.5:27b`
- **Endpoint ID**: `your_endpoint_id`
- **Endpoint Type**: `ollama`

## Quick Start

```bash
cd /mnt/ai/serverless-proxy

# Start Docker
docker compose up -d

# Test the proxy
curl http://localhost:8002/v1/models | jq .
curl -X POST http://localhost:8002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen3.5:27b", "messages": [{"role": "user", "content": "Hello!"}]}' | jq .
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `RUNPOD_API_KEY` | RunPod API key (Bearer token) | (your API key) |
| `RUNPOD_ENDPOINT_ID` | RunPod serverless endpoint ID | (your endpoint ID) |
| `MODEL_NAME` | Model identifier | `qwen3.5:27b` |
| `ENDPOINT_TYPE` | Endpoint format: `ollama` or `vllm` | `ollama` |
| `TIMEOUT` | Request timeout (seconds) | `300` |

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
Parses tool calls from text format and converts to OpenAI function calling format.

### Job Polling
If RunPod returns `IN_QUEUE` status, the proxy polls for completion (up to 300 seconds).

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
