# Health + Failover + Caching Implementation Plan

## Scope

- Add endpoint health tracking and rate-limit-aware failover.
- Add non-streaming cache for chat/embeddings.
- Keep behavior unchanged when failover is not configured.
- Keep cache opt-in at virtual model level (`cache_enabled`).

## Key Decisions

- Health polling is optional per endpoint (`health_check_url`) and only runs if at least one virtual model has failover configured.
- Failover is configured per virtual model in `virtual_model_failover`.
- Backup targets are selected by virtual model.
- Duplicate strategy picks other enabled virtual models with the same `actual_model`.
- Cache TTLs are settings, not hardcoded.
- Cache savings use both input and output token cost.
- Per-model cache bypass via checkbox (`cache_enabled=0`) disables all cache behavior for that model.

## DB Changes

### New tables

1. `endpoint_health`
   - `endpoint_id` (PK/FK)
   - `status` (`healthy|unhealthy|circuit_open`)
   - `failure_count`
   - `last_error`
   - `last_failure_at`
   - `circuit_until`
   - `rate_limit_info` (JSON)

2. `virtual_model_failover`
   - `virtual_model_id` (PK/FK)
   - `strategy` (`backup|duplicate|rotational`)
   - `targets` (JSON)
   - `max_attempts` (nullable override)
   - `cooldown_seconds` (nullable override)
   - `failure_threshold` (nullable override)

3. `response_cache`
   - `cache_key` (PK)
   - `response_json`
   - `created_at`
   - `expires_at`
   - `hit_count`
   - `model`
   - `cost_in`
   - `cost_out`

### New columns

- `endpoints.health_check_url` TEXT default `''`
- `virtual_models.cache_enabled` INTEGER default `1`
- `request_usage.cache_attempted` INTEGER default `0`
- `request_usage.cache_hit` INTEGER default `0`

### New settings defaults

- `cache_ttl_chat=300`
- `cache_ttl_embeddings=3600`
- `health_check_interval=60`
- `circuit_failure_threshold=3`
- `circuit_failure_window=60`
- `circuit_cooldown_seconds=300`

## Runtime Components

### Health system

- In-memory `health_cache` keyed by `endpoint_id`.
- `load_health_cache()` on startup.
- `update_health()` and `increment_health_failure()` helpers persist to DB.
- `health_check_runner()`:
  - starts only if failover exists
  - loops on `health_check_interval`
  - polls endpoints with non-empty `health_check_url`
  - marks healthy on 2xx or JSON `{status:"ok"}` / `{healthy:true}`

### Failover wrapper

- `FailoverBackend(LLMBackend)` wraps primary backend only when failover config exists.
- `chat_completion()` tries primary then alternatives by strategy.
- `duplicate`: dynamically computes alternatives by same `actual_model`.
- respects circuit-open status and configured/global thresholds.

### Cache system

- Deterministic cache key from normalized request payload.
- Non-streaming only.
- Skip cache for:
  - `cache_enabled=0`
  - tool-call responses
  - errors
  - `Cache-Control: no-store`
- `get_cached()` increments `hit_count`.
- `store_cached()` writes response + expires_at + cost rates.

## API/UI Changes

### Settings tab

Add editable fields with descriptions:

- Chat cache TTL (seconds)
- Embeddings cache TTL (seconds)
- Health check interval (seconds)
- Circuit failure threshold
- Circuit failure window (seconds)
- Circuit cooldown (seconds)

### Endpoint modal

- Add optional `Health Check URL` field with note:
  - "Optional. Leave blank to disable active polling."
  - Accepts JSON health payloads like:
    - `{ "healthy": true }`
    - `{ "status": "ok" }`
    - `{ "healthy": false }`
    - `{ "status": "unhealthy" }`

### Virtual model modal

- Add `Enable caching for this model` checkbox.
- Add failover section (strategy + target selectors + optional threshold overrides).

### Usage page

- Add cache metrics:
  - attempts
  - hits
  - hit rate
  - savings (`sum(cost_estimate where cache_hit=1)`)

## Rollout Sequence

1. DB schema/settings migrations.
2. Health cache helpers + runner (gated by failover presence).
3. Failover wrapper + backend selection integration.
4. Cache key/get/store + chat/embeddings integration.
5. Usage metrics wiring for cache fields.
6. Settings/UI updates.
7. Docs updates.
8. Validation:
   - `python3 -m py_compile simple_bridge.py`
   - container build/start
   - smoke tests (`task test:models`, `task test:chat`, `task test:stream`)

## Safety Constraints

- If no `virtual_model_failover` row exists for a model, no failover is attempted.
- If no model has failover enabled, health runner is not started.
- OAuth endpoint behavior remains backend-specific; failover only wraps around existing backend calls and error statuses.
