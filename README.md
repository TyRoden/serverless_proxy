# Serverless Proxy - Universal LLM Gateway

A universal OpenAI-compatible API proxy that bridges standard API requests to multiple backend providers (RunPod, Ollama, OpenAI-compatible APIs, Together AI, etc.). Configure endpoints through a web admin UI and map virtual model names to actual backend models.

## Overview

```
Client (OpenAI format) → Serverless Proxy (port 8002) → Configured Backends
```

- **Universal**: Connect to any LLM backend (RunPod, Ollama, OpenAI, Together AI, etc.)
- **Virtual Models**: Map user-facing model names to actual backend models
- **Admin UI**: Configure endpoints and virtual models via web interface
- **OpenAI-compatible**: Works with any OpenAI client library

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
# Optional: If using RunPod or another provider, add your API key and endpoint below
# RUNPOD_API_KEY=your_api_key_here
# RUNPOD_ENDPOINT_ID=your_endpoint_id_here
# MODEL_NAME=model-name-on-your-backend
# ENDPOINT_TYPE=ollama  (or: openai, runpod, together, vllm)
```

### Step 3: Start the Proxy

```bash
# Build and start the container
docker compose up -d --build
```

### Step 4: Configure in the Admin UI

1. Open your browser and go to: **http://localhost:5001/proxy-dashboard**
2. You'll see the admin dashboard (no login needed since AUTH_ENABLED=false)

#### Add an Endpoint
3. Click **+ Add Endpoint** under Endpoints
4. Fill in:
   - **Name**: Something like "My Ollama" or "RunPod Production"
   - **URL**: Your backend URL (e.g., `http://localhost:11434` for local Ollama, or your RunPod endpoint URL)
   - **API Key**: Your API key if required (leave blank for local Ollama)
   - **Type**: Select the type (`ollama`, `openai`, `runpod`, etc.)
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
API Key: any-key-works (or your endpoint's key)
Model: the-virtual-model-name-you-created
```

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

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_PATH` | SQLite database path | `/data/proxy.db` |
| `FLASK_PORT` | Admin UI port | `5001` |
| `TIMEOUT` | Request timeout (seconds) | `300` |
| `AUTH_ENABLED` | Enable admin authentication | `true` |
| `AIMENU_URL` | Auth service URL | `http://localhost:5000` |

### Authentication

By default, the admin dashboard requires authentication. See [docs/authentication.md](docs/authentication.md) for:
- How to disable authentication for fresh installs
- How to implement your own auth service
- Full API specification for the `/session/validate` endpoint

### Docker Ports

| Port | Service |
|------|---------|
| `8002` | OpenAI-compatible API |
| `5001` | Admin UI API |

## Admin Dashboard

Access the admin dashboard at `/proxy-dashboard`. Authentication is handled by the AI Menu System.

### Features

- **Endpoint Management**: Add, edit, delete backend endpoints
- **Virtual Model Mapping**: Map virtual model names to actual backend models
- **Model Discovery**: Fetch available models from endpoints
- **Enable/Disable**: Toggle endpoints and virtual models

### Endpoint Configuration

Configure backend endpoints with:

- **Name**: Friendly identifier
- **URL**: Base URL (e.g., `http://localhost:11434`, `https://api.runpod.ai/v2/xxxx`)
- **API Key**: Authorization token (if required)
- **Type**: `openai`, `ollama`, `vllm`, `together`, `runpod`
- **Priority**: Higher priority endpoints are preferred
- **Enabled**: Enable/disable endpoint

### Virtual Models

Map virtual model names to actual backend models:

- **Virtual Name**: What clients will request (e.g., `gpt-4`, `prod-llama`)
- **Endpoint**: Which backend to route to
- **Actual Model**: The model name on the backend (e.g., `gpt-4o`, `llama3:70b`)
- **Show Reasoning**: Toggle chain-of-thought display (for models like MiniMax that output thinking separately)

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

### Admin API (port 5001)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/admin/endpoints` | GET | List all endpoints |
| `/endpoints` | GET, POST | Manage endpoints |
| `/endpoints/<id>` | PUT | Update endpoint |
| `/endpoints/<id>/delete` | POST | Delete endpoint |
| `/endpoints/<id>/test` | POST | Test endpoint connection |
| `/endpoints/<id>/models` | GET | Fetch available models |
| `/api/admin/virtual-models` | GET | List virtual models |
| `/virtual-models` | GET, POST | Manage virtual models |
| `/virtual-models/<id>` | PUT | Update virtual model |
| `/virtual-models/<id>/delete` | POST | Delete virtual model |

## Backend Types

| Type | Description |
|------|-------------|
| `openai` | OpenAI-compatible API |
| `ollama` | Ollama API |
| `vllm` | vLLM API |
| `together` | Together AI |
| `runpod` | RunPod Serverless |

## AI Queue Integration (Optional)

Route requests through AI Queue Master for priority queuing and request tracking.

```bash
USE_AI_QUEUE=true
AI_QUEUE_URL=http://host.docker.internal:8102
AI_QUEUE_API_KEY=your_queue_api_key
AI_QUEUE_PRIORITY=NORMAL
```

## Features

- **Tool call parsing** — Automatically extracts tool calls from model output
- **Chain-of-thought stripping** — Removes reasoning prefixes
- **Streaming & non-streaming** — Full SSE streaming support
- **Job polling** — Automatically polls for queued job completion
- **Session-based auth** — Uses AI Menu System for admin authentication

## Troubleshooting

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
