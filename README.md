# RunPod Serverless Proxy

An OpenAI-compatible API proxy that bridges standard API requests to RunPod Serverless endpoints, making queue-based serverless LLM inference work as a drop-in OpenAI API replacement.

## Tested With

- **RunPod Worker Ollama** `ollama@0.18.2` on RunPod Serverless
- **Model**: Qwen 3.5 27B (`qwen3.5:27b`)

## Features

- **OpenAI-compatible endpoints** — Works with any OpenAI client library (`openai`, `AI SDK`, etc.)
- **Tool call parsing** — Automatically extracts tool calls from model output in multiple formats:
  - Fenced JSON: ` ```tool_call {"name": "...", "arguments": {...}} ``` `
  - XML-style: `<tool_code>{"name":"...","arguments":{...}}</tool_code>` (OpenCode task format)
  - `<tool_use code name="...">` format
  - Bare Python calls: `task(description="...", prompt="...")`
  - Multiple calls per fence: `{"name":"x"}{"name":"y"}`
- **Chain-of-thought stripping** — Removes `analysis:`, `final:`, `assistantfinal` prefixes from responses
- **Streaming & non-streaming** — Full SSE streaming support with proper `chat.completion.chunk` format
- **Job polling** — Automatically polls for queued job completion (configurable timeout)
- **Dual endpoint support** — Works with both Ollama and vLLM endpoints via `ENDPOINT_TYPE`

## Quick Start

### Prerequisites

- Docker & Docker Compose
- A RunPod serverless endpoint with an LLM worker (tested with [RunPod Worker Ollama](https://hub.docker.com/r/ollama/ollama))
- RunPod API key

### Run with Docker

```bash
git clone https://github.com/TyRoden/serverless_proxy.git
cd serverless_proxy
cp .env.example .env
# Edit .env with your RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID
docker compose up -d --build
curl http://localhost:8002/v1/models | jq .
```

### Configuration

Copy `.env.example` to `.env` and set your values:

```bash
RUNPOD_API_KEY=your_runpod_api_key
RUNPOD_ENDPOINT_ID=your_endpoint_id
MODEL_NAME=qwen3.5:27b
ENDPOINT_TYPE=ollama
TIMEOUT=300
```

The `docker-compose.yml` uses `env_file: .env` to load these automatically.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `RUNPOD_API_KEY` | RunPod API key | (required) |
| `RUNPOD_ENDPOINT_ID` | RunPod serverless endpoint ID | (required) |
| `MODEL_NAME` | Model identifier exposed by the API | `qwen3.5:27b` |
| `ENDPOINT_TYPE` | Endpoint format: `ollama` or `vllm` | `ollama` |
| `TIMEOUT` | Request timeout in seconds | `300` |

## API Endpoints

### `GET /v1/models`

```bash
curl http://localhost:8002/v1/models
```

### `POST /v1/chat/completions`

```bash
curl -X POST http://localhost:8002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen3.5:27b", "messages": [{"role": "user", "content": "Hello!"}]}'
```

#### Streaming

```bash
curl -X POST http://localhost:8002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen3.5:27b", "messages": [{"role": "user", "content": "Hi"}], "stream": true}'
```

## Endpoint Types

### Ollama (`ENDPOINT_TYPE=ollama`)

Converts OpenAI message format to a prompt format suitable for RunPod Ollama endpoints.

### vLLM (`ENDPOINT_TYPE=vllm`)

Passes messages directly with `sampling_params`. Suitable for RunPod vLLM endpoints.

## Troubleshooting

```bash
# View container logs
docker logs runpod-serverless-proxy

# Restart container
docker restart runpod-serverless-proxy

# Check endpoint health
curl -s -H "Authorization: Bearer $RUNPOD_API_KEY" \
  https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/health | jq .
```

## Development

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your values
python simple_bridge.py
```

## Project Structure

```
.
├── simple_bridge.py      # Main proxy application (FastAPI)
├── docker-compose.yml    # Docker Compose configuration
├── Dockerfile            # Container image definition
├── requirements.txt      # Python dependencies
├── .env.example          # Environment variable template
├── .env                  # Your secrets (not committed)
├── README.md
├── CHANGELOG.md
└── LICENSE.md
```

## License

MIT License — see [LICENSE.md](LICENSE.md)

## Acknowledgments

- Based on [runpod-serverless-proxy](https://github.com/dannysemi/runpod-serverless-proxy) by [Daniel Semanisin](https://github.com/dannysemi) — the original proxy implementation
- Built with [FastAPI](https://fastapi.tiangolo.com/)
