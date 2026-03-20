#!/bin/bash
# Start the RunPod Serverless Proxy

cd /mnt/ai/serverless-proxy

# Load environment variables
source .env

# Start the proxy with your vLLM endpoint
python main.py \
  --endpoint "$RUNPOD_ENDPOINT_ID" \
  --api_key "$RUNPOD_API_KEY" \
  --model "$MODEL_NAME" \
  --timeout 150 \
  --use_openai_format 1 \
  --host 0.0.0.0 \
  --port 8000
