# Serverless Proxy - Project Configuration

Project-specific configuration for the Serverless Proxy.

**Reference**: [deft/main.md](deft/main.md) | [deft/languages/python.md](deft/languages/python.md) | [deft/coding/serverless-proxy.md](deft/coding/serverless-proxy.md)

## Project Overview

| Item | Value |
|------|-------|
| **Name** | Serverless Proxy |
| **Purpose** | Bridge OpenAI/Anthropic API requests to OpenAI/Anthropic endpoints with support for queue servers |
| **Port** | 8002 |
| **Model** | `project-system-ai` |
| **Stack** | Python (FastAPI), Docker |

## Current Status

- **Proxy**: Running in Docker
- **Container**: `serverless-proxy`
- **Queue Mode**: AI Queue Master enabled

## Tech Stack

- Python (FastAPI)
- Docker
- RunPod Serverless API
- AI Queue Master (optional)

## Strategy

- **Process**: Light (single-pass implementation)
- **Coverage**: ≥75% (project-specific override)
- **Languages**: Python

## Files

| File | Purpose |
|------|---------|
| `simple_bridge.py` | Main proxy application |
| `docker-compose.yml` | Docker Compose config |
| `Dockerfile` | Container image |
| `Taskfile.yml` | Task automation |

## Commands

```bash
# Docker
task docker:up        # Start proxy
task docker:stop      # Stop proxy
task docker:logs      # View logs

# Testing
task test:models      # Test /v1/models
task test:chat        # Test chat completions
```

---

For detailed configuration (environment variables, endpoints, troubleshooting), see [deft/PROJECT.md](deft/PROJECT.md).