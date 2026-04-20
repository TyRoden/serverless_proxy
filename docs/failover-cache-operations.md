# Failover and Cache Operations Guide

This guide explains exactly how failover and caching behave at runtime, and what each related dashboard setting controls.

## How Failover Works

Failover is configured per virtual model and is disabled unless that virtual model has a failover configuration row.

### Request flow

1. Client calls a virtual model.
2. Proxy routes to that model's primary endpoint/model mapping.
3. If failover is configured for that virtual model:
   - proxy evaluates candidate backends by strategy
   - skips endpoints with open circuit
   - retries only retryable failures (`429`, `500`, `502`, `503`, `504`)
4. On success, proxy returns that backend response and records routed model/endpoint in Activity.

### Strategies

- `backup`
  - Order: primary first, then configured target virtual models in listed order.
  - Best for strict primary/secondary failover.

- `rotational`
  - Order: primary first, then configured target virtual models with a rotating start index.
  - Best for spreading retry load across alternates.

- `duplicate`
  - Order: primary first, then other enabled virtual models using the same `actual_model`.
  - No manual target list required.
  - Best when multiple endpoints serve the same backend model and you want automatic fallback.

### Circuit behavior

- Circuits are tracked per endpoint.
- Retryable failures increment endpoint failure counters.
- When threshold is reached, endpoint is temporarily marked circuit-open.
- Circuit-open endpoints are skipped until cooldown expires.

Global defaults come from Settings and can be overridden in failover config for each virtual model.

## Health Checks

Health polling is optional and endpoint-specific.

- Polling only runs when at least one virtual model has failover configured.
- Endpoint `Health Check URL` is optional. If blank, that endpoint is not actively polled.

Accepted health responses:

- healthy:
  - any `2xx` with empty/non-JSON body
  - `2xx` JSON with `{ "healthy": true }`
  - `2xx` JSON with `{ "status": "ok" }`
- unhealthy:
  - `2xx` JSON with `{ "healthy": false }`
  - `2xx` JSON with `{ "status": "down" | "error" | "unhealthy" }`
  - any non-`2xx`, timeout, or network error

## Cache Behavior

Cache is non-stream only and applied to chat/completions + embeddings paths.

### Cache apply rules

- Cache considered only when:
  - request is non-stream
  - virtual model has `cache_enabled = 1`
  - request header does not include `Cache-Control: no-store`
- Cache is not stored for tool-call responses.
- Errors are not cached.
- TTL comes from settings (`cache_ttl_chat`, `cache_ttl_embeddings`).

### Cache and cost metrics

Usage metrics include:

- cache attempts
- cache hits
- cache hit rate
- estimated cache savings

## Dashboard Settings Reference

### Settings tab

- `Chat Cache TTL (seconds)`
  - lifetime for non-stream chat/completions cache entries.

- `Embeddings Cache TTL (seconds)`
  - lifetime for embeddings cache entries.

- `Health Check Interval (seconds)`
  - polling cadence for configured endpoint health URLs.

- `Circuit Failure Threshold`
  - retryable failures needed before opening circuit.

- `Circuit Failure Window (seconds)`
  - rolling window used by failure counter logic.

- `Circuit Cooldown (seconds)`
  - time endpoint remains circuit-open before retry eligibility.

### Endpoint modal

- `Health Check URL (optional)`
  - custom health endpoint to poll for this endpoint.
  - leave blank to disable active polling for that endpoint.

### Virtual Model modal

- `Enable Non-Stream Cache`
  - enables/disables cache participation for that virtual model.

- `Enable Failover`
  - master switch for failover config on this virtual model.

- `Failover Strategy`
  - one of `backup`, `rotational`, `duplicate`.

- `Failover Targets`
  - list of virtual models used by `backup` and `rotational` strategies.

- `Max Attempts (optional)`
  - cap on candidate attempts for this model before returning failure.

- `Failure Threshold (optional)`
  - per-model override for circuit open threshold.

- `Cooldown Seconds (optional)`
  - per-model override for circuit-open cooldown.

## Activity Visibility

Activity rows show:

- virtual model and routed model (`virtual -> actual`)
- endpoint used for the handled request
- failover note when substitution occurred (for example `failover:backup -> backup-vm`)
