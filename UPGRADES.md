# Serverless Proxy - Upgrade Opportunities

## Current Issues (Needs Immediate Attention)

### 1. ~~Timeout/Failure Issue~~ - RESOLVED
- **Status**: ✅ Working as of 2026-03-28
- **What was fixed**: Proxy now successfully routes requests through AI Queue Master
- **Test Result**: POST to `/v1/chat/completions` returns 200 with valid response
- **Notes**: Timeout issue was transient, likely related to container startup timing or network race conditions
- **Priority**: ~~CRITICAL~~ - Now operational

## OpenAI Compatibility Features

### OA-1: ~~Missing Chat Completion Parameters~~ - RESOLVED
**Priority: HIGH** - Many tools expect these parameters

**Now supported:**
- `messages` ✅
- `model` ✅
- `temperature` ✅
- `max_tokens` ✅
- `top_p` ✅
- `stream` ✅
- `tools` ✅
- `stop` ✅
- `presence_penalty` ✅
- `frequency_penalty` ✅
- `logit_bias` ✅
- `user` ✅
- `tool_choice` ✅
- `response_format` ✅
- `seed` ✅
- `parallel_tool_calls` ✅

### OA-2: Additional OpenAI Endpoints
**Priority: MEDIUM** - Some tools use these

**Missing endpoints:**
- `GET /v1/models` ✅ (already implemented)
- `POST /v1/completions` - Legacy text completions (some tools use)
- `POST /v1/embeddings` - Embedding generation
- `POST /v1/images/generations` - Image generation
- `POST /v1/images/edits` - Image editing
- `POST /v1/images/variations` - Image variations
- `POST /v1/audio/transcriptions` - Speech to text
- `POST /v1/audio/speech` - Text to speech

**Implementation**: Either proxy to queue or return proper error indicating unsupported

### OA-3: ~~OpenAI Error Response Format~~ - PARTIALLY RESOLVED
**Priority: HIGH** - Tools expect consistent error format

**Status**: ✅ Fixed in proxy code, but see AQM-1 for ai-queue-master issue

**Current**: Error responses now return in OpenAI format from proxy

**OpenAI error format:**
```json
{
  "error": {
    "message": "The model 'gpt-4' does not exist",
    "type": "invalid_request_error",
    "param": "model",
    "code": "model_not_found"
  }
}
```

**Implementation**: ✅ All proxy errors now return in OpenAI format:
- 400 Bad Request ✅
- 401 Unauthorized ✅ (via queue)
- 403 Forbidden ✅ (via queue)
- 404 Not Found ✅
- 408 Request Timeout ✅
- 422 Unprocessable Entity ✅ (via queue)
- 429 Too Many Requests ✅ (via queue)
- 500 Internal Server Error ✅
- 503 Service Unavailable ✅

**Note**: See AQM-1 for ai-queue-master error format issue

### OA-4: Response Field Completeness
**Priority: MEDIUM** - Some tools validate response fields

**Missing/incorrect fields:**
- `system_fingerprint` - Should pass through from queue
- `finish_reason` - Should be "stop", "length", "tool_calls", or "content_filter"
- `logprobs` - Log probabilities (optional)
- `index` - Choice index (for multiple choices)

### OA-5: Streaming Response Integrity
**Priority: HIGH** - Known bug in current code

**Issues:**
- Line 717: Always sets `finish_reason: "tool_calls"` for normal responses when `tool_calls_data` is truthy (bug)
- Should only use `"stop"` when there are NO tool calls
- `delta` field may be missing `role` on first chunk

**Fix:**
```python
# Current (buggy):
"finish_reason": "tool_calls" if tool_calls_data else "stop"

# Should check actual choice content:
"finish_reason": "tool_calls" if (tool_calls_data and not text_content) else "stop"
```

### OA-6: Rate Limit Headers
**Priority: LOW** - Nice to have

**OpenAI headers:**
- `X-RateLimit-Limit-Requests`
- `X-RateLimit-Limit-Tokens`
- `X-RateLimit-Remaining-Requests`
- `X-RateLimit-Remaining-Tokens`
- `X-RateLimit-Reset-Requests`
- `X-RateLimit-Reset-Tokens`

**Implementation**: Add headers when available from queue

### OA-7: Content-Type Headers
**Priority: MEDIUM** - Some clients are strict

**Current**: Returns application/json

**Should support:**
- `application/json` (default)
- `text/event-stream` for streaming (already set)

### OA-8: CORS Headers
**Priority: MEDIUM** - Needed for browser-based tools

**Missing headers for cross-origin requests:**
- `Access-Control-Allow-Origin`
- `Access-Control-Allow-Methods`
- `Access-Control-Allow-Headers`

**Implementation**: Add CORS middleware for browser-based clients

## Code Quality Improvements

### 2. Type Hints & Static Analysis
- **Current**: Minimal typing (lines 1-60 have no type hints)
- **Recommendations**:
  - Add type hints to all function parameters and return types
  - Configure mypy/linting in CI
  - Add Pydantic models for request/response validation
- **Benefit**: Catch errors before runtime, better IDE support

### 3. Configuration Management
- **Current**: Hardcoded strings, env vars scattered throughout
- **Issues**:
  - `MODEL_NAME` fallback in code vs env var
  - `queue_timeout=1200` hardcoded in function signature
  - `timeout=300.0` for direct mode hardcoded
- **Recommendations**:
  - Use `pydantic-settings` for config validation
  - Centralize all configuration in a config class
  - Validate required env vars on startup
- **Benefit**: Fail fast with clear error messages

### 4. Logging Improvements
- **Current**: Mixed `logger.warning()` and no structured logging
- **Recommendations**:
  - Structured logging with correlation IDs for requests
  - Log request/response sizes, latencies
  - Separate log levels for different concerns (routing, AI Queue, RunPod)
  - Add request tracing (request_id -> response_time)
- **Benefit**: Easier debugging, better observability

### 5. Error Handling
- **Current**: Limited error handling in `handle_ai_queue_request`
- **Gaps**:
  - No retry logic for transient failures
  - No circuit breaker pattern
  - Exceptions silently caught in some paths
- **Recommendations**:
  - Add retry with exponential backoff
  - Implement circuit breaker for AI Queue/RunPod endpoints
  - Clear error categorization (client vs server errors)
- **Benefit**: More resilient service

## Architecture Improvements

### 6. Health Check Endpoint
- **Current**: No `/health` or `/ready` endpoints
- **Recommendations**:
  - Add `/health` endpoint checking:
    - Application status
    - AI Queue connectivity
    - RunPod connectivity (when not in queue mode)
  - Implement readiness probe for Docker/K8s
- **Benefit**: Easy status checks, better orchestration

### 7. Graceful Shutdown
- **Current**: No explicit shutdown handling
- **Recommendations**:
  - Handle SIGTERM/SIGINT gracefully
  - Stop accepting new requests
  - Drain existing requests
  - Close httpx client connections
- **Benefit**: No lost requests on restart

### 8. Network Configuration
- **Current**: Uses `host.docker.internal` which is unreliable from containers
- **Issues**:
  - Works from host machine but not from containers on same network
  - DNS resolution inconsistent
- **Recommendations**:
  - Use Docker network service name (`ai-queue-master`) when on same network
  - Fallback to `host.docker.internal` for local development
  - Add network topology detection
- **Benefit**: Reliable cross-container communication

### 9. Docker Compose Improvements
- **Current**: Basic setup, no health checks, no logging config
- **Recommendations**:
  - Add healthcheck to docker-compose.yml
  - Configure log rotation
  - Add restart policy with backoff
  - Add resource limits (CPU, memory)
- **Benefit**: Production-ready deployment

## Missing Features

### 10. Rate Limiting
- **Current**: No rate limiting at all
- **Recommendations**:
  - Per-IP rate limiting using Redis or in-memory
  - Priority-based rate limiting (HIGH > NORMAL > LOW)
  - Burst allowance for batch operations
- **Benefit**: Prevents abuse and overload

### 11. Metrics & Monitoring
- **Current**: No metrics endpoint, no observability
- **Recommendations**:
  - Add `/metrics` endpoint (Prometheus format)
  - Track:
    - Request latency (p50, p95, p99)
    - Request count by endpoint
    - Error rate by type
    - Queue depth (if accessible)
    - Cache hit rate (if caching added)
- **Benefit**: Visibility into system health

### 12. Request Tracing
- **Current**: No request IDs, hard to trace requests
- **Recommendations**:
  - Generate UUID for each request
  - Propagate through all internal calls
  - Include in response headers
  - Log correlation with requests
- **Benefit**: Easier debugging, better user experience

### 13. Configuration Validation
- **Current**: Invalid configs accepted until runtime
- **Recommendations**:
  - Validate AI_QUEUE_URL on startup
  - Validate RUNPOD_API_KEY format
  - Validate MODEL_NAME exists in AI Queue
  - Test connectivity to dependent services
- **Benefit**: Fail fast with clear errors

### 14. Streaming Bug Fix
- **Current**: `finish_reason` logic is broken
- **Issues**:
  - Line 717: Always sets `finish_reason: "tool_calls"` for normal responses
  - Line 717: Should only use `"stop"` when no tool calls
- **Recommendations**:
  - Fix the conditional logic
  - Add tests for streaming responses
- **Benefit**: Correct OpenAI-compatible responses

## Testing Gaps

### 15. Unit Tests
- **Current**: No test files
- **Recommendations**:
  - Test `process_content()` with various outputs
  - Test tool call extraction edge cases
  - Test chain-of-thought stripping
  - Mock external API calls
- **Benefit**: Catch regressions

### 16. Integration Tests
- **Current**: No integration tests
- **Recommendations**:
  - Test with mock AI Queue endpoint
  - Test with mock RunPod endpoint
  - Test streaming vs non-streaming responses
  - Test timeout behavior
- **Benefit**: Catch integration issues

### 17. Load Testing
- **Current**: No performance testing
- **Recommendations**:
  - Test with k6 or locust
  - Identify throughput limits
  - Measure latency under load
- **Benefit**: Know system limits

## Documentation

### 18. Developer Documentation
- **Current**: Good README, but no developer docs
- **Recommendations**:
  - Add CONTRIBUTING.md
  - Add architecture decision records (ADRs)
  - Document deployment steps
  - Add troubleshooting guide
- **Benefit**: Easier onboarding

### 19. API Documentation
- **Current**: Manual curl examples in README
- **Recommendations**:
  - Generate OpenAPI/Swagger docs
  - Document all error codes
  - Document rate limits and quotas
- **Benefit**: Better developer experience

## Performance Optimizations

### 20. Caching
- **Current**: No caching at all
- **Recommendations**:
  - Cache model metadata (list of available models)
  - Cache AI Queue connectivity check
  - Consider response caching for identical requests
- **Benefit**: Faster responses, less load

### 21. Connection Pooling
- **Current**: httpx client created per request
- **Recommendations**:
  - Share httpx clients across requests
  - Configure connection pool sizes
  - Use persistent connections to AI Queue
- **Benefit**: Better performance, fewer connections

### 22. Async Improvements
- **Current**: Mixed sync/async patterns
- **Recommendations**:
  - Make all I/O async
  - Use async context managers for client reuse
  - Optimize await points
- **Benefit**: Better throughput

## Future Enhancements (Not Now)

### 23. Caching Layer
- Redis-backed caching for:
  - Model availability
  - Rate limit state
  - Request rate limiting state

### 24. Multi-Queue Support
- Support routing to different queues based on:
  - Model name
  - Priority
  - Request characteristics

### 25. Request queuing
- Queue requests internally when:
  - AI Queue is at capacity
  - RunPod endpoints are busy
  - Rate limit exceeded

### 26. Fallback Mode
- Automatically fall back to:
  - Direct RunPod if queue is down
  - Different queue if primary is overloaded

### 27. Batch Operations
- Support batch requests:
  - Multiple messages in one call
  - Return all completions

### 28. Custom Headers
- Support custom headers passthrough:
  - `X-User-ID`
  - `X-Session-ID`
  - `X-Custom-Header`

### 29. Model Fallback
- Support multiple models per request:
  - Try first model, fall back to second
  - Useful for high-availability

---

## Priority Summary (Updated for OpenAI Compatibility Focus)

### Blocker (Do Now)
1. ~~**Timeout issue**~~ - RESOLVED
2. **OA-5: Streaming bug** - Returns wrong finish_reason (HIGH for tools)
3. **OA-3: Error response format** - Tools expect OpenAI error format

### High (Next Sprint) - OpenAI Compatibility
4. **OA-1: Missing parameters** - stop, presence_penalty, frequency_penalty, user, tool_choice
5. **OA-4: Response field completeness** - system_fingerprint, proper finish_reason

### Medium - OpenAI Compatibility
6. **OA-2: Additional endpoints** - /v1/completions, /v1/embeddings (if needed)
7. **OA-7: CORS headers** - For browser-based tools
8. **OA-6: Rate limit headers** - Nice to have

### Medium - Infrastructure
9. Health endpoints - Essential for deployment
10. Config validation - Fail fast on bad config

### Low (Backlog)
11. Type hints and static analysis
12. Logging improvements
13. Rate limiting (at proxy level)
14. Metrics and monitoring
15. Request tracing
16. Caching
17. Code quality improvements

---

## Implementation Order (OpenAI Compatibility Focus)

1. ~~**Fix timeout issue**~~ - RESOLVED
2. **Fix streaming bug (OA-5)** - 15 min - HIGH PRIORITY for OpenCode/tools
3. **Add OpenAI error format (OA-3)** - 30 min - Tools expect this
4. **Pass through missing parameters (OA-1)** - 1 hour
5. **Fix response fields (OA-4)** - 30 min
6. **Add CORS headers (OA-7)** - 30 min
7. **Add health endpoints** - 30 min
8. **Add config validation** - 2 hours
9. **Add rate limiting** - 3-4 hours
10. **Add metrics** - 3-4 hours
11. **Add tests** - 4-6 hours
12. **Code quality improvements** - ongoing

---

## Context: What This Proxy Does

This proxy bridges **queue-based systems** (AI Queue Master, RunPod Serverless) to appear as **direct OpenAI-compatible endpoints**. This allows:

- **OpenCode** to work with slower queue-based systems
- **RunPod Serverless** to work without direct endpoint management
- **Multiple users** to queue requests that get processed in order

Key insight: The more OpenAI-compatible this proxy is, the more tools will work seamlessly with queue-based backends without modification.

---

---

## Issues for ai-queue-master (Different Repository)

These issues should be fixed in ai-queue-master, not in this proxy:

### AQM-1: Error Response Format Consistency
- **Location**: ai-queue-master error handling
- **Issue**: Some error responses are double-encoded as strings inside JSON
- **Current**: `{"error": "{\"error\": {\"message\": \"...\", ...}}"}`
- **Expected**: `{"error": {"message": "...", "type": "...", "param": "...", "code": "..."}}`
- **Impact**: Clients receive malformed error JSON that requires extra parsing

### AQM-2: Missing Response Fields
- **Location**: ai-queue-master response generation
- **Issue**: May not return all standard OpenAI fields
- **Missing fields**:
  - `system_fingerprint` - Should be passed through when available
  - Some models return additional fields that should be preserved

### AQM-3: Streaming Response Timing
- **Location**: ai-queue-master streaming
- **Issue**: Stream may be slower than necessary
- **Note**: This is acceptable for queue-based systems but worth optimizing

### AQM-4: Health Endpoint Response
- **Location**: ai-queue-master `/health` endpoint
- **Issue**: Should return more detailed health info for proxy monitoring
- **Recommendation**: Include queue depth, connected workers, upstream health

---

---

## Completed Improvements

### Backend Abstraction Layer ✅
**Added**: Abstract backend system for easy extension

**New architecture:**
```python
class LLMBackend(ABC):
    async def chat_completion(...) -> (result, error, status_code)
    async def health_check(...) -> bool

class AIQueueBackend(LLMBackend):
    """Routes to AI Queue Master"""

class RunPodBackend(LLMBackend):
    """Routes directly to RunPod Serverless"""
```

**Benefits:**
- Unified code path for all backends
- Easy to add new backends (Anyscale, Together AI, etc.)
- Consistent error handling
- Health checks per backend

**Adding a new backend:**
```python
class MyCustomBackend(LLMBackend):
    async def chat_completion(self, ...):
        # Your implementation
        pass
    
    async def health_check(self):
        return True

# In get_backend():
if config == "my-custom":
    return MyCustomBackend()
```

### OA-8: Health Endpoint ✅
**Added**: `/health` endpoint for container orchestration

**Response:**
```json
{
  "status": "healthy",
  "backend": "AIQueueBackend",
  "timestamp": 1774708167
}
```

---

*This document should be reviewed and prioritized by the maintainers. Individual items can be broken down into GitHub issues for tracking.*
