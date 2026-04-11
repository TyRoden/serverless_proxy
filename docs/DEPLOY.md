# Deployment Guide

## Running with Uvicorn (Recommended)

The Serverless Proxy uses FastAPI for its API endpoints. To serve these correctly, **Uvicorn must be used** as the ASGI server.

### Updated `docker-compose.yml`

Ensure your `docker-compose.yml` includes:

```yaml
services:
  serverless-proxy:
    build: .
    ports:
      - "8002:8002"
      - "5001:5001"
    command: uvicorn simple_bridge:app --host 0.0.0.0 --port 8002
    volumes:
      - ./data:/data
    environment:
      - API_PORT=8002
      - FLASK_PORT=5001
```

### Verify Server Status

After starting:

```bash
# Check if Uvicorn is running
 docker compose exec serverless-proxy sh -c "ps aux | grep uvicorn"

# Test API endpoint
 curl http://localhost:8002/v1/models
```

> ⚠️ **Do not use `python simple_bridge.py`** — it will start Flask but fail to serve FastAPI routes like `/api/admin/usage` and `/v1/chat/completions`.

## Troubleshooting 404 on API Routes

If you see `404 Not Found` on `/v1/...` or `/api/admin/...` routes:

1. Confirm `command: uvicorn simple_bridge:app --host 0.0.0.0 --port 8002` is set
2. Ensure `uvicorn` is installed in the Docker image: add `RUN pip install uvicorn` to `Dockerfile`
3. Rebuild and restart: `docker compose down && docker compose up -d --build`

## OpenWebUI Upstream Configuration

If you are adding OpenWebUI as a backend endpoint in the admin dashboard, use endpoint type `openwebui`.

- Chat route: `/api/chat/completions`
- Model discovery: `/api/models` (with fallback support in the proxy)
- Embeddings: `/api/v1/embeddings`

This avoids path mismatches that can happen if OpenWebUI is configured as generic `openai`.

## Why This Matters

- `python simple_bridge.py` launches Flask only → admin UI works, API fails
- `uvicorn simple_bridge:app` launches FastAPI → both API and admin work

The proxy combines Flask (for UI) and FastAPI (for API). Only Uvicorn serves FastAPI correctly.

For more details, see the [FastAPI deployment docs](https://fastapi.tiangolo.com/deployment/).
