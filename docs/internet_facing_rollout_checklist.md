# Internet-Facing Rollout Checklist

This document is the working checklist for adding optional internet-facing support to the proxy while preserving the current default behavior for internal/LAN users.

The design goal is:

- Default behavior remains exactly as it is today for downloadable installs.
- Internet-facing behavior is explicit, opt-in, and operator-configured.
- LAN users should not break when the feature is introduced.

## Working Rules

- Implement in small phases.
- Test after every phase before continuing.
- Do not batch unrelated security, routing, and UX changes together.
- Preserve current behavior whenever `deployment_mode=internal_only`.
- Treat client IP resolution as the highest-risk compatibility area.

## Final Design Summary

### Deployment modes

- `internal_only`
  - Default.
  - Must preserve current behavior exactly.
  - No new inbound runtime API auth checks.
- `internet_facing`
  - Opt-in mode.
  - External callers require inbound API key.
  - Trusted internal CIDRs may bypass inbound API key.
  - Private/internal-only routes remain restricted.

### Hostnames

- `ai.completeupdates.com`
  - Existing UI and current routing remain unchanged.
- `api.completeupdates.com`
  - Public runtime API hostname.
  - Explicit Caddy path allowlist only.

### Trusted internal CIDRs

- Settings key: `trusted_internal_cidrs`
- Format: comma-delimited CIDRs
- Generic default: `127.0.0.1/32`
- Intended deployment value: `192.168.50.0/24,127.0.0.1/32`
- Optional exact Docker subnet may be added only if validation proves necessary.
- In deployments where tools call the proxy via `host.docker.internal`, exact Docker/internal gateway ranges may need to be included in `trusted_internal_cidrs` for compatibility.

### Private-only routes

- `/health`
- `/proxy-dashboard`
- `/api/admin/*`
- `/endpoints*`
- `/virtual-models*`
- `/docs*`
- `/openapi*`

### Public runtime routes in `internet_facing`

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/completions`
- `POST /v1/embeddings`
- `POST /v1/messages`
- `POST /chat/completions`
- `POST /api/chat/completions`
- `GET /models`
- `GET /api/models`
- `GET /api/v1/models`
- `POST /embeddings`
- `POST /api/v1/embeddings`
- `GET /api/version`
- `GET /api/tags`
- `POST /api/chat`
- `POST /api/generate`
- `POST /api/embed`
- `POST /api/embeddings`
- `POST /api/show`
- `GET /api/ps`
- `POST /api/ps`

### Internal-only lifecycle routes in `internet_facing`

- `POST /api/pull`
- `POST /api/push`
- `POST /api/create`
- `POST /api/copy`
- `DELETE /api/delete`
- `POST /api/delete`
- `POST /api/blobs/{digest}`

## Current-State Notes

These observations were gathered before implementation and should be preserved as rollout context.

### Current settings observed

- `api_port = 8002`
- `flask_port = 5001`
- `aimenu_url = http://localhost:5000`
- `use_ai_queue = false`
- `ai_queue_url = http://host.docker.internal:8102`
- `debug_mode = full`
- `payload_audit_enabled = true`
- `auth_enabled = false`
- `cache_ttl_chat = 300`
- `cache_ttl_embeddings = 3600`
- `health_check_interval = 60`
- `circuit_failure_threshold = 3`
- `circuit_failure_window = 60`
- `circuit_cooldown_seconds = 300`

### Current route activity observed

Top observed routes:

- `POST /v1/chat/completions`
- `POST /chat/completions`
- `GET /v1/models`
- `POST /v1/completions`
- `POST /v1/embeddings`

Observed LAN activity from `192.168.50.x`:

- `POST /chat/completions`
- `POST /v1/chat/completions`
- `GET /v1/models`

No recent observed activity for:

- `/health`
- public lifecycle Ollama routes

### Important risk note

Most recorded activity currently appears from `172.21.0.1`, which is likely Docker or reverse-proxy infrastructure. This means trusted-proxy-aware effective client IP resolution is essential before enabling `internet_facing` mode.

### Docker compatibility note

The current deployment uses Docker networking and `host.docker.internal`.

Observed environment facts:

- The proxy container is on `172.21.0.2/16`
- The Docker gateway for the proxy network is `172.21.0.1`
- `host.docker.internal` resolves inside the proxy container to `172.17.0.1`

Implication:

- Some existing internal tools may reach the proxy through Docker/host-gateway paths rather than appearing as `192.168.50.x` directly.
- In `internet_facing` mode, those tools may need exact Docker/internal CIDRs added to `trusted_internal_cidrs`.
- This should be done only for exact ranges confirmed by testing, not by broadly trusting all RFC1918 Docker-like ranges.

Verified behavior during rollout:

- Public/internal traffic should use `https://api.completeupdates.com`, not direct `host.docker.internal:8002` access.
- Requests arriving through Caddy come from the Docker peer `172.21.0.1` and must be treated as trusted proxy traffic.
- Direct `host.docker.internal:8002` calls do not carry the forwarded client IP and will be evaluated as non-internal in `internet_facing` mode.
- Once internal tools were switched to `https://api.completeupdates.com`, internal access worked correctly without an inbound API key.

Likely deployment-specific trusted internal CIDR candidates:

- `192.168.50.0/24`
- `127.0.0.1/32`
- `172.17.0.0/16`

Verified deployment-specific split:

- Trusted proxy CIDRs:
  - `127.0.0.1/32`
  - `172.21.0.0/16`
- Trusted internal CIDRs:
  - `192.168.50.0/24`
  - `127.0.0.1/32`
  - `172.17.0.0/16`

These are deployment-specific compatibility candidates, not generic project defaults.

## Master Checklist

### 1. Policy Freeze

- [x] Add `deployment_mode`
- [x] Allowed values are `internal_only` and `internet_facing`
- [x] Default is `internal_only`
- [x] `internal_only` preserves current behavior exactly
- [x] `api.completeupdates.com` is the public runtime hostname
- [x] `ai.completeupdates.com` remains unchanged
- [x] `/health` remains private-only
- [x] Public routing uses explicit path allowlists only
- [x] Lifecycle Ollama routes are LAN-only in `internet_facing`
- [x] Inbound API keys are dashboard-managed only

### 2. Database and Settings

- [x] Add `deployment_mode` setting in DB bootstrap
- [x] Add `trusted_internal_cidrs` setting in DB bootstrap
- [x] Default `deployment_mode` to `internal_only`
- [x] Default `trusted_internal_cidrs` to `127.0.0.1/32`
- [x] Add `inbound_api_keys` table
- [x] Add indexes for inbound API keys
- [x] Keep migrations additive and safe for existing installs

### 3. Dashboard and Settings UI

- [x] Add `Deployment Mode` field to Settings tab
- [x] Add `Trusted Internal CIDRs` field to Settings tab
- [x] Add help text for comma-delimited CIDRs
- [x] De-emphasize CIDR field when in `internal_only`
- [x] Add `API Keys` tab
- [x] Add key list UI
- [x] Add dashboard-only key generation flow
- [x] Show full key once only after creation
- [x] Add enable/disable control
- [x] Add revoke/delete control
- [x] Show label, prefix, notes, created time, last-used metadata

### 4. Inbound API Key System

- [x] Add `inbound_api_keys` schema
- [x] Store only key prefix and secure hash
- [x] Never store full plaintext key
- [x] Never return full key after creation
- [x] Support multiple active labeled keys
- [ ] Track `last_used_at`
- [ ] Track `last_used_ip`
- [ ] Track `last_used_user_agent`
- [x] Never log full inbound key values

### 5. Client IP and Trust Handling

- [x] Add helper to read deployment mode safely
- [x] Add helper to parse `trusted_internal_cidrs`
- [x] Validate CIDRs strictly
- [x] Ignore malformed CIDRs safely with warnings
- [x] Fall back safely if CIDR parsing fails
- [x] Add trusted-proxy-aware effective client IP resolver
- [x] Trust `X-Forwarded-For` only from known reverse-proxy peers
- [x] Keep trusted proxy peers separate from `trusted_internal_cidrs`
- [x] Log direct peer IP and effective client IP during validation
- [x] Only add exact Docker subnet if testing proves it is needed

### 6. Runtime Auth Behavior

- [x] In `internal_only`, keep current runtime behavior unchanged
- [x] In `internal_only`, do not enforce new inbound runtime auth
- [x] In `internet_facing`, trusted internal CIDRs bypass inbound key for public runtime routes
- [x] In `internet_facing`, non-trusted callers require inbound API key for public runtime routes
- [x] In `internet_facing`, lifecycle routes are trusted-internal only
- [x] In `internet_facing`, `/health` remains private-only
- [ ] Add auth outcome markers in activity/logging

### 7. Public Runtime Route Coverage

- [x] Protect `GET /v1/models`
- [x] Protect `POST /v1/chat/completions`
- [x] Protect `POST /v1/completions`
- [x] Protect `POST /v1/embeddings`
- [x] Protect `POST /v1/messages`
- [ ] Protect `POST /chat/completions`
- [ ] Protect `POST /api/chat/completions`
- [x] Protect `GET /models`
- [x] Protect `GET /api/models`
- [x] Protect `GET /api/v1/models`
- [ ] Protect `POST /embeddings`
- [ ] Protect `POST /api/v1/embeddings`
- [x] Protect `GET /api/version`
- [x] Protect `GET /api/tags`
- [x] Protect `POST /api/chat`
- [x] Protect `POST /api/generate`
- [x] Protect `POST /api/embed`
- [x] Protect `POST /api/embeddings`
- [x] Protect `POST /api/show`
- [x] Protect `GET /api/ps`
- [x] Protect `POST /api/ps`

### 8. Internal-Only Lifecycle Route Coverage

- [x] Restrict `POST /api/pull`
- [x] Restrict `POST /api/push`
- [x] Restrict `POST /api/create`
- [x] Restrict `POST /api/copy`
- [x] Restrict `DELETE /api/delete`
- [x] Restrict `POST /api/delete`
- [x] Restrict `POST /api/blobs/{digest}`

### 9. Caddy and Edge Routing

- [x] Add dedicated Caddy site block for `api.completeupdates.com`
- [x] Use explicit path allowlists only
- [x] Add explicit public runtime route matchers
- [x] Add explicit LAN-only lifecycle route matchers
- [x] Restrict lifecycle matchers by LAN source IP
- [x] Do not publicly route `/health`
- [x] Do not publicly route admin/dashboard/docs/openapi paths
- [x] Return `404` for everything else
- [x] Preserve forwarding headers correctly
- [x] Add request logging for the new hostname

### 10. Firewall and Port Hardening

- [ ] Keep localhost access to `127.0.0.1:8002`
- [ ] Keep localhost access to `127.0.0.1:5001`
- [ ] Block public direct access to `:8002`
- [ ] Block public direct access to `:5001`
- [ ] Decide whether raw LAN `:8002` remains temporarily allowed
- [ ] Plan migration of LAN clients to `https://api.completeupdates.com` if needed

### 11. Hardening Before Public Rollout

- [ ] Reduce `debug_mode` before public rollout
- [ ] Disable `payload_audit_enabled` before public rollout
- [ ] Tighten CORS for `internet_facing`
- [ ] Add rate limiting plan for public routes
- [ ] Add request size and timeout controls

### 12. LAN Compatibility Validation

- [ ] Test from real `192.168.50.x` clients
- [ ] Confirm LAN callers can still use `/chat/completions`
- [ ] Confirm LAN callers can still use `/v1/chat/completions`
- [ ] Confirm LAN callers can still use `/v1/models`
- [ ] Confirm LAN callers are resolved to effective internal IPs correctly
- [ ] Confirm no unexpected LAN auth failures in `internal_only`
- [ ] Confirm expected LAN bypass works in `internet_facing`
- [x] Confirm Docker-based internal callers using `host.docker.internal` do not carry forwarded client identity directly in `internet_facing`
- [x] Confirm internal callers work when switched to `https://api.completeupdates.com`
- [x] Confirm exact Docker/internal gateway CIDRs needed for compatibility

### 13. External Validation

- [ ] Confirm public runtime routes reject missing key in `internet_facing`
- [ ] Confirm invalid keys are rejected
- [ ] Confirm valid keys succeed
- [ ] Confirm lifecycle routes are not externally accessible
- [x] Confirm `/health` is not externally accessible
- [ ] Confirm admin routes are not externally accessible

### 14. Client Compatibility Validation

- [ ] Test OpenAI-compatible clients
- [ ] Test Anthropic-compatible clients
- [ ] Test Ollama-compatible clients
- [ ] Verify explicit path allowlist covers real client methods and aliases
- [x] Verify internal tools work when targeting `https://api.completeupdates.com` without an explicit port

### 15. Documentation

- [ ] Update README with `internal_only` mode
- [ ] Update README with `internet_facing` mode
- [ ] Document `deployment_mode`
- [ ] Document `trusted_internal_cidrs`
- [ ] Document optional exact Docker subnet addition
- [ ] Document `api.completeupdates.com`
- [ ] Document same-IP multi-hostname behavior
- [ ] Document private-only `/health`
- [ ] Document internal-only lifecycle routes
- [ ] Document dashboard-only key management
- [ ] Document firewall/reverse-proxy expectations

## Ordered Execution Plan

### Phase 1: App and DB groundwork

- [x] Add settings and inbound key schema
- [x] Verify app starts cleanly
- [x] Verify defaults exist in settings API/UI
- [x] Verify no behavior changes in `internal_only`

### Phase 2: Dashboard key management

- [x] Add API Keys tab
- [x] Add generation flow
- [x] Verify full key is shown once only
- [x] Verify DB stores prefix/hash only

### Phase 3: Client IP and trust helpers

- [x] Add CIDR parsing/validation
- [x] Add trusted-proxy-aware effective client IP resolution
- [x] Validate direct peer vs effective client IP behavior

### Phase 4: Mode-gated runtime auth

- [x] Implement runtime auth helper
- [x] Verify `internal_only` remains unchanged
- [ ] Verify `internet_facing` public route behavior
- [ ] Verify `internet_facing` lifecycle route behavior

### Phase 5: Caddy public hostname

- [x] Add `api.completeupdates.com`
- [x] Add explicit public matchers
- [x] Add explicit LAN-only lifecycle matchers
- [x] Verify non-allowed paths return `404`

### Phase 6: Port hardening and public rollout

- [ ] Block public direct access to raw `8002`
- [ ] Block public direct access to raw `5001`
- [ ] Verify Caddy remains functional
- [ ] Verify LAN compatibility still holds

### Phase 7: Production hardening and docs

- [ ] Lower debug exposure
- [ ] Disable payload audit by default for public use
- [ ] Finish docs updates

## Definition of Done

- [ ] `internal_only` preserves current behavior
- [ ] `internet_facing` works as designed
- [ ] LAN users do not unexpectedly need keys
- [ ] External users do need valid inbound API keys
- [ ] Lifecycle routes are LAN-only in `internet_facing`
- [ ] `/health` is private-only
- [ ] Admin routes remain private
- [ ] `api.completeupdates.com` uses explicit allowlisting only
- [ ] Raw `:8002` is not publicly reachable
- [ ] Raw `:5001` is not publicly reachable
- [ ] Effective client IP handling is validated with real LAN traffic
- [ ] No plaintext inbound key storage
- [ ] No full key logging
- [ ] Documentation reflects both deployment modes and operator responsibilities
