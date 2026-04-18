# OpenAI OAuth Setup Guide

This guide covers the current `openai_oauth` flow in Serverless Proxy, including web auth, Codex import, model setup, usage behavior, and reverse-proxy requirements.

## When to Use `openai_oauth`

Use endpoint type `openai_oauth` when you want Serverless Proxy to talk to ChatGPT/Codex-style OpenAI endpoints that require OAuth tokens instead of a standard API key.

Use endpoint type `openai` instead when you have a normal OpenAI API key from `platform.openai.com`.

## What `openai_oauth` Does

For this endpoint type, the proxy:

- uses `https://chatgpt.com` as the default upstream base URL
- exchanges OAuth credentials against `https://auth.openai.com/oauth/token`
- forwards chat requests to `POST /backend-api/codex/responses`
- translates OpenAI Chat Completions requests into Responses-style payloads
- translates Responses-style streaming events back into OpenAI-compatible chat output

This path is specifically designed for ChatGPT/Codex OAuth-backed access, not standard API-key access.

## Recommended Endpoint Defaults

When you select `openai_oauth` in the dashboard, the form should default to:

- `url`: `https://chatgpt.com`
- `oauth_enabled`: enabled
- `oauth_grant_type`: `refresh_token`
- `oauth_token_url`: `https://auth.openai.com/oauth/token`
- `oauth_token_request_format`: `json`
- `oauth_client_auth_method`: `client_secret_post`

Keep those defaults unless you have a provider-specific reason to change them.

## Supported Ways to Populate OAuth Fields

There are two practical ways to populate an OpenAI OAuth endpoint:

1. `Start Web OAuth`
2. `Import from Codex auth.json`

Both are supported in the endpoint modal.

## Important Notes About Client Secret

The client secret is often the most confusing part of this flow.

- The OAuth callback does **not** fetch or invent a client secret for you.
- If your flow requires a client secret, it only comes from what you already have and provide.
- For PKCE/public-client style flows, the client secret may legitimately remain blank.
- Leaving the client secret blank during endpoint edits will now preserve the existing stored value instead of wiping it.

The same preservation behavior also applies to the refresh token when editing an existing endpoint.

## Flow A: Start Web OAuth

Use this when you want the dashboard to help you complete a browser login flow.

### Steps

1. Open the admin dashboard: `http://localhost:5001/proxy-dashboard`
2. Add or edit an endpoint
3. Set **Endpoint Type** to `openai_oauth`
4. Verify the defaults shown above
5. Optionally enter:
   - `oauth_client_id` if you already have one for the flow you are using
   - `oauth_client_secret` only if your flow requires it
6. Click **Start Web OAuth**
7. Complete login/consent in the opened browser window or popup
8. After redirect, either:
   - let the proxy callback handle it automatically, or
   - copy the full redirect URL or auth code and paste it into **Paste Redirect URL or Auth Code**
9. Click **Complete Web OAuth** if manual completion is needed
10. Save the endpoint

### What This Fills In

After successful completion, the endpoint form should contain at least:

- `oauth_client_id` if provided by the flow/import
- `oauth_refresh_token`
- `oauth_access_token` runtime state in DB/cache as needed
- `oauth_token_expires_at`

It will **not** automatically create a client secret unless one was already part of the flow input.

## Flow B: Import from Codex `auth.json`

Use this when you already logged into Codex/ChatGPT locally and want to import the resulting OAuth state.

### Typical Source Files

- `~/.codex/auth.json`
- `~/.chatgpt-local/auth.json`
- `/root/.codex/auth.json`
- `/root/.chatgpt-local/auth.json`

### Steps

1. Open endpoint modal
2. Set type to `openai_oauth`
3. Click **Import from Codex auth.json**
4. Leave the path blank to try default discovery, or provide a specific file path
5. Review imported values
6. Save endpoint

### Container Path Reminder

If Serverless Proxy runs in Docker, the file must exist inside the container filesystem.

If the file only exists on the host machine, either:

- mount it into the container, or
- use the web OAuth flow instead

## Recommended End-to-End Setup

After OAuth credentials are in place:

1. Save the endpoint
2. Create a virtual model that points to this endpoint
3. Set the virtual model's **Actual Model** manually if needed
4. Test with `POST /v1/chat/completions`

### Model Discovery Note

Model discovery is best-effort for OAuth-backed endpoints.

Some ChatGPT/Codex OAuth tokens do not expose readable `/models` routes even though chat works correctly.

If the endpoint **Models** button does not return models:

- save the endpoint anyway
- set the virtual model's **Actual Model** manually
- `gpt-5.4` is confirmed working for this setup

The proxy now also falls back to already-configured virtual model mappings when upstream model listing is unavailable.

## OpenAI OAuth Request Behavior

There are a few important differences from normal OpenAI API-key mode:

- the upstream path is Responses-style, not `POST /v1/chat/completions`
- this path expects instruction-style payload handling
- tool calls are streamed via Responses events and translated back into OpenAI chat tool calls
- `tool_choice: "required"` is preserved and no longer downgraded to `auto`

## Tool Calling Support

The proxy now has dedicated handling for OAuth tool-call streaming.

It assembles tool calls from:

- `response.output_item.added`
- `response.function_call_arguments.delta`
- `response.output_item.done`

This fixes common issues where OAuth-backed tool calls previously arrived with missing names, fragmented arguments, or empty `{}` payloads.

## Usage and Cost Tracking Behavior

OpenAI OAuth upstreams often do not return token counts in the same way as API-key endpoints.

Current behavior:

- if upstream usage is present, the proxy uses it
- if usage is missing, the proxy estimates prompt/completion/total tokens for `openai_oauth`
- the Usage page marks those numbers with `*`
- footer note shown in UI:
  - `* OpenAI OAuth endpoint does not provide token counts, these are estimates only.`

## Editing Existing OAuth Endpoints Safely

Sensitive OAuth fields are intentionally not prefilled in the edit modal.

That is expected.

Current behavior when editing:

- `oauth_client_secret` field appears blank unless you re-enter it
- `oauth_refresh_token` field appears blank unless you re-enter it
- leaving them blank preserves the existing stored values

This prevents accidental secret/token loss during routine edits.

## OAuth Token Lifecycle

The proxy handles token lifecycle as follows:

- access tokens are cached in memory for runtime use
- refresh token rotations returned by upstream are persisted to SQLite
- expiry metadata is persisted in `oauth_token_expires_at`

If a refresh is rejected, the proxy now logs parsed OAuth error details when available.

Example useful diagnostic code:

- `refresh_token_reused`

If you see that, re-authenticate and obtain a fresh refresh token.

## Reverse Proxy / Caddy Requirements

If you front the dashboard with Caddy or another reverse proxy, make sure OAuth admin routes do not get swallowed by broader `/api/*` rules.

These routes must reach the Flask admin service on `127.0.0.1:5001`:

- `/api/admin/oauth/*`
- `/api/admin/endpoints*`
- `/api/admin/virtual-models*`
- `/api/admin/endpoints/activity`
- `/endpoints*`
- `/virtual-models*`

These routes should go to the FastAPI service on `127.0.0.1:8002`:

- `/api/admin/usage*`
- `/api/admin/activity`

### Example Caddy Snippet

```caddy
@proxy-api-usage path /api/admin/usage*
handle @proxy-api-usage {
    reverse_proxy 127.0.0.1:8002
}

@proxy-api-activity path /api/admin/activity
handle @proxy-api-activity {
    reverse_proxy 127.0.0.1:8002
}

@proxy-api-oauth path /api/admin/oauth/*
handle @proxy-api-oauth {
    reverse_proxy 127.0.0.1:5001
}

@proxy-api path /api/admin/endpoints* /api/admin/virtual-models* /api/admin/endpoints/activity /endpoints* /virtual-models*
handle @proxy-api {
    reverse_proxy 127.0.0.1:5001
}
```

If `/api/admin/oauth/openai/start-web-auth` is hitting unrelated auth middleware or basic auth first, your reverse-proxy routing order is wrong.

## Troubleshooting

### `401` / `invalid_grant`

- refresh token expired, revoked, or no longer valid
- re-authenticate and replace the refresh token

### `401` / `invalid_client`

- wrong client ID or client secret
- wrong `oauth_client_auth_method`

### `refresh_token_reused`

- the refresh token has already been consumed/rotated by upstream
- complete the web OAuth flow again or import a fresh `auth.json`

### Client secret seems to disappear when editing

- the edit form intentionally leaves secret fields blank
- blank values are now preserved on save
- this does **not** mean the secret was deleted

### Endpoint model fetch fails

- expected for some OAuth token scopes
- set the model manually instead
- `gpt-5.4` is known to work here

### Chat works but tools do not

- make sure you are on current code with updated OAuth tool streaming fixes
- OAuth tool calls are handled differently than normal OpenAI API-key mode
- current proxy logic now assembles streamed tool args correctly

### Chat request returns `Instructions are required`

- this is specific to the upstream OAuth/Responses-style behavior
- ensure you are using the current proxy translation layer rather than hitting upstream directly

### Chat request returns `Stream must be set to true`

- OAuth-backed upstream requires streaming in this path
- the proxy already handles this behavior for translated chat-completions flows

### Endpoint URL is set to `https://api.openai.com`

- for `openai_oauth`, use `https://chatgpt.com`
- `https://api.openai.com` is for normal API-key mode, not this OAuth flow

## Security Notes

- Treat refresh tokens and client secrets like passwords
- Do not paste secrets into logs, screenshots, or tickets
- Refresh token rotations are persisted in SQLite for restart safety
- Access tokens are cached in memory
- Secrets are currently stored in plaintext DB columns, consistent with current `api_key` handling

## Related Docs

- `README.md`
- `docs/oauth-encryption-secrets-storage.md`
- `docs/diagnostics.md`
