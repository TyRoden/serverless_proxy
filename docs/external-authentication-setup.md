# Setting Up Serverless Proxy for External Authentication and Usage

This guide explains how to keep Serverless Proxy easy to install by default while also supporting a secure internet-facing deployment when you need it.

The two most important ideas are:

- The default mode should stay simple for users: `internal_only`
- Internet-facing mode is opt-in and adds inbound API key protection for external callers

This document covers:

- what the deployment modes do
- how to expose the proxy safely to the internet
- how to configure trusted internal networks
- how to route traffic with Caddy
- how to generate and use inbound API keys
- how to make internal tools and external tools work together cleanly

## 1. Deployment Modes

Serverless Proxy now supports two deployment modes.

### Internal Only

This is the default mode and should remain the default for normal installs.

Purpose:

- keep setup simple
- preserve existing local/LAN behavior
- avoid forcing authentication changes on fresh installs

Behavior:

- no inbound runtime API key is required
- local and LAN usage behaves like previous versions
- best for private-only installs

This is the correct default for users who clone the repo and want a quick Docker-based install.

### Internet Facing

This mode is for operators who want to publish the proxy through a reverse proxy such as Caddy.

Behavior:

- external callers must provide a valid inbound API key
- trusted internal callers can bypass the inbound API key
- health and admin routes stay private
- mutating Ollama lifecycle routes stay internal-only

## 2. Recommended Architecture

Recommended hostnames:

- `ai.yourdomain.com`
  - existing UI/admin environment
- `api.yourdomain.com`
  - public runtime API hostname

Recommended public entrypoint:

- public traffic should enter through Caddy on `443`
- internal tools should also use `https://api.yourdomain.com`
- avoid direct use of raw `:8002` in internet-facing mode

Why:

- Caddy preserves the real client identity through forwarded headers
- the proxy can distinguish internal callers from external callers
- routing stays explicit and easier to reason about

## 3. Important Default Behavior

The project should be committed and distributed with these defaults:

- `deployment_mode = internal_only`
- `trusted_internal_cidrs = 127.0.0.1/32`

This keeps first-time setup easy.

Users who want public exposure can then deliberately switch to `internet_facing` in the dashboard.

## 4. What the Proxy Can Do in Internet-Facing Mode

Serverless Proxy is not only a backend router. It can act as a secure internal and external gateway for tools.

Examples:

- connect OpenAI-compatible tools through one stable endpoint
- connect Anthropic-compatible tools through the same proxy
- connect Ollama-compatible tools through the same proxy
- configure OAuth-backed upstreams once, then let many tools consume them through one local proxy endpoint
- generate inbound API keys for external systems that should be allowed to call your proxy

Practical example:

1. configure an OAuth-backed upstream endpoint in the dashboard
2. create one or more virtual models
3. point your coding tools, automations, or private apps at `https://api.yourdomain.com`
4. use dashboard-generated inbound API keys for external systems

That lets you centralize model routing, OAuth, compatibility, and access control in one place.

## 5. Trusted Internal Networks

The proxy uses a setting named:

- `trusted_internal_cidrs`

Format:

- comma-delimited CIDRs

Example:

```text
192.168.50.0/24,127.0.0.1/32,172.17.0.0/16
```

### What should go into this setting

Only put real internal client identity ranges here.

Typical values:

- your LAN subnet, for example `192.168.50.0/24`
- loopback `127.0.0.1/32`
- exact Docker internal ranges only if those ranges represent real trusted internal callers in your deployment

### What should not go here

Do not use this setting for reverse-proxy transport peers.

For example:

- if Caddy reaches the app from `172.21.0.1`, that is a trusted proxy path, not automatically a trusted internal client identity

That distinction matters.

## 6. Docker and Caddy CIDR Guidance

In Docker-based deployments, two different kinds of internal network ranges often appear:

### Trusted proxy CIDRs

These are IP ranges used by reverse-proxy or container transport paths.

In the verified deployment used during rollout:

- Caddy/proxy traffic reached the app from `172.21.0.1`
- this belongs to the `172.21.0.0/16` Docker network

This range should be treated as a trusted proxy path for forwarded headers.

### Trusted internal client CIDRs

These are the real caller identities allowed to bypass inbound API key checks.

Verified working internal set for the deployment:

- `192.168.50.0/24`
- `127.0.0.1/32`
- `172.17.0.0/16`

In that deployment:

- `172.17.0.0/16` represented Docker/host-gateway based internal tool calls
- `172.21.0.0/16` represented the Caddy/proxy transport path

Do not blindly trust all `172.16.0.0/12` space. Use exact ranges that you have verified.

## 7. Internal Tools Should Use the Hostname

In internet-facing mode, internal tools should use:

```text
https://api.yourdomain.com
```

Do not prefer:

```text
http://host.docker.internal:8002
```

Why:

- requests through Caddy carry the forwarded client identity needed for correct internal/external classification
- direct raw `:8002` traffic does not necessarily carry the real client identity
- internal tools using the hostname were verified to work correctly without an inbound API key once trusted internal CIDRs were configured properly

No explicit port is needed for HTTPS access:

```text
https://api.yourdomain.com/v1/chat/completions
```

## 8. Public and Private Route Policy

### Public runtime routes

These are appropriate to expose publicly in `internet_facing` mode:

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

### Internal-only lifecycle routes

These should stay internal-only:

- `POST /api/pull`
- `POST /api/push`
- `POST /api/create`
- `POST /api/copy`
- `DELETE /api/delete`
- `POST /api/delete`
- `POST /api/blobs/{digest}`

### Private-only routes

These should not be public on the runtime hostname:

- `/health`
- `/proxy-dashboard`
- `/api/admin/*`
- `/endpoints*`
- `/virtual-models*`
- `/docs*`
- `/openapi*`

## 9. Step-by-Step Setup for Internet-Facing Mode

### Step 1: Start with the default install

Use the normal Docker setup first.

```bash
git clone https://github.com/TyRoden/serverless_proxy.git
cd serverless_proxy
cp .env.example .env
docker compose up -d --build
```

At this point, the default should remain internal-only.

### Step 2: Configure your upstream endpoints and virtual models

Open the dashboard:

```text
http://localhost:5001/proxy-dashboard
```

Add:

- endpoints
- virtual models
- OAuth-backed upstreams if needed

### Step 3: Switch to internet-facing mode

In the Settings tab:

1. Set `Deployment Mode` to `Internet Facing`
2. Set `Trusted Internal CIDRs`

Example verified value:

```text
192.168.50.0/24,127.0.0.1/32,172.17.0.0/16
```

### Step 4: Add Caddy routing

Create a dedicated public hostname such as:

```text
api.yourdomain.com
```

Use explicit route allowlisting only.

### Step 5: Generate inbound API keys

In the dashboard:

1. Open the `API Keys` tab
2. Click `Generate Key`
3. Enter a label such as:
   - `partner-a`
   - `external-tooling`
   - `automation-runner`
4. Copy the generated key immediately

Important:

- the full key is shown only once
- only a prefix and secure hash are stored afterward

### Step 6: Point tools at the proxy

Internal tools:

- use `https://api.yourdomain.com`
- no key needed if they come from trusted internal CIDRs

External tools:

- use `https://api.yourdomain.com`
- provide the generated inbound API key

## 10. Recommended Caddy Configuration

Below is the recommended runtime site block shape for `api.yourdomain.com`.

### Example

```caddy
api.yourdomain.com {
    encode gzip zstd

    log {
        output stdout
        format console
    }

    @proxy_public_models {
        path /v1/models /models /api/models /api/v1/models /api/version /api/tags
    }
    handle @proxy_public_models {
        reverse_proxy 127.0.0.1:8002
    }

    @proxy_public_runtime {
        path /v1/chat/completions /chat/completions /api/chat/completions /v1/completions /v1/messages /v1/embeddings /embeddings /api/v1/embeddings /api/chat /api/generate /api/embed /api/embeddings /api/show /api/ps
    }
    handle @proxy_public_runtime {
        reverse_proxy 127.0.0.1:8002
    }

    @proxy_ps_get {
        method GET
        path /api/ps
    }
    handle @proxy_ps_get {
        reverse_proxy 127.0.0.1:8002
    }

    @proxy_lifecycle_lan {
        path /api/pull /api/push /api/create /api/copy /api/delete /api/blobs/*
        remote_ip 192.168.50.0/24 127.0.0.1/32 172.17.0.0/16 172.21.0.0/16
    }
    handle @proxy_lifecycle_lan {
        reverse_proxy 127.0.0.1:8002
    }

    respond 404
}
```

### Explanation of each section

#### `encode gzip zstd`

Enables compression for responses.

#### `log`

Enables request logging for the public runtime hostname.

#### `@proxy_public_models`

Explicitly allows model-listing and Ollama model/version routes.

#### `@proxy_public_runtime`

Explicitly allows the public runtime inference endpoints.

#### `@proxy_ps_get`

Separately allows the GET variant of `/api/ps` so it is explicitly covered.

#### `@proxy_lifecycle_lan`

Allows mutating Ollama lifecycle routes only from internal source ranges.

#### `respond 404`

Everything not explicitly allowed returns `404`.

This prevents accidental exposure of newly added routes.

## 11. API Key Usage Examples

### OpenAI-compatible client

Base URL:

```text
https://api.yourdomain.com/v1
```

Headers:

```text
Authorization: Bearer spk_...
```

### Curl example

```bash
curl https://api.yourdomain.com/v1/models \
  -H "Authorization: Bearer spk_your_generated_key"
```

```bash
curl -X POST https://api.yourdomain.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer spk_your_generated_key" \
  -d '{
    "model": "your-virtual-model-name",
    "messages": [{"role": "user", "content": "Hello from outside the LAN"}]
  }'
```

### Internal tool example

Internal tools on trusted internal networks can use the same hostname without a key:

```bash
curl https://api.yourdomain.com/v1/models
```

## 12. Recommended Validation Checklist

### Internal validation

- internal tools using `https://api.yourdomain.com` succeed without a key
- internal tools no longer rely on direct `host.docker.internal:8002`
- internal-only lifecycle routes remain available only to internal callers

### External validation

- public request without key returns `401`
- public request with invalid key returns `401`
- public request with valid key succeeds
- `/health` returns `404` on the public hostname
- admin routes are not exposed on the public hostname

## 13. Production Recommendations

Before real public exposure:

- set `debug_mode` to `basic` or `off`
- set `payload_audit_enabled` to `false`
- keep raw `:8002` and `:5001` off the public internet
- document that internal tools should use the hostname, not raw port access

## 14. Summary

Recommended defaults for the project:

- keep `internal_only` as the default mode
- keep first-run setup easy

Recommended configuration for internet-facing deployments:

- use `api.yourdomain.com`
- use explicit Caddy route allowlists
- use dashboard-generated inbound API keys
- keep trusted proxy CIDRs separate from trusted internal CIDRs
- use exact Docker/internal ranges only when verified in your environment
