# OpenAI OAuth Setup Guide (for `openai_oauth` endpoints)

This guide explains where OpenAI OAuth values come from and how to configure them in Serverless Proxy.

## Important: where OpenAI client ID/secret come from

For standard OpenAI API usage, OpenAI uses API keys (`sk-...`) from `platform.openai.com`.

- There is no general self-service page in the OpenAI API dashboard to create arbitrary OAuth client credentials for API calls in the same way many IdPs do.
- If you are configuring GPT Actions or Apps SDK authentication, those OAuth client credentials are typically created in **your own identity provider** (Auth0, Okta, etc.), not issued by OpenAI for general API access.

## When to use `openai_oauth`

Use `openai_oauth` only when you already have a valid OAuth token source compatible with OpenAI token exchange.

If you only need normal OpenAI API access, use endpoint type `openai` with an API key.

## Practical source for OpenAI OAuth refresh-token flow

For local ChatGPT/Codex-based OAuth workflows, credentials are commonly sourced from your local Codex/ChatGPT auth cache after login.

Typical files:

- `~/.codex/auth.json`
- `~/.chatgpt-local/auth.json`

Common values you will map into Serverless Proxy:

- `oauth_client_id`
- `oauth_refresh_token`
- (optional) `oauth_client_secret` if your OAuth client uses one

## Step-by-step setup in Serverless Proxy

1. Open Admin Dashboard: `http://localhost:5001/proxy-dashboard`
2. Add endpoint and set **Type** to `openai_oauth`
3. Optional quick import:
   - Click **Import from Codex auth.json**
   - Leave path blank to try default discovery paths, or provide a specific path
   - If successful, OAuth fields are auto-populated
4. Optional interactive login:
   - Click **Start Web OAuth**
   - Complete login/consent in the popup window
   - Copy the redirected URL (or just the `code` value)
   - Paste it into **Paste Redirect URL or Auth Code** and click **Complete Web OAuth**
5. Keep or verify defaults:
   - URL: `https://api.openai.com`
   - OAuth enabled: `true`
   - Grant type: `refresh_token`
   - Token URL: `https://auth.openai.com/oauth/token`
   - Token request format: `json`
   - Client auth method: `client_secret_post`
6. Fill in (or verify imported values):
   - `oauth_client_id`
   - `oauth_refresh_token`
   - `oauth_client_secret` (if required by your token source)
   - `oauth_scope` (optional)
7. Save endpoint
8. Use **Fetch Models** on that endpoint to validate token exchange
9. Map a virtual model to this endpoint and test with `POST /v1/chat/completions`

## About web authorization from the dashboard

Serverless Proxy supports an in-app browser PKCE helper via **Start Web OAuth**.

- The helper starts authorization and supports manual redirect/code paste completion
- For headless/remote setups, import from local `auth.json` remains the fallback path
- You can still complete OAuth via official client flows (for example, Codex login) and then import credentials

## Container path note

If Serverless Proxy runs in Docker, the app can only read files visible inside the container.

- If your `auth.json` is only on your host machine, mount it into the container or paste/import via a path that exists in-container
- Default discovery paths include:
  - `~/.codex/auth.json`
  - `~/.chatgpt-local/auth.json`
  - `/root/.codex/auth.json`
  - `/root/.chatgpt-local/auth.json`

## Troubleshooting

- `401/invalid_grant`: refresh token is expired/revoked; re-authenticate and update token
- `401/invalid_client`: incorrect client ID/secret or wrong auth method
- `404` on token URL: wrong token endpoint (verify `https://auth.openai.com/oauth/token`)
- Works with API key but not OAuth: verify `oauth_enabled` and required OAuth fields are populated
- Endpoint **Models** button returns `404`: some OpenAI/Codex OAuth tokens do not expose OpenAI-compatible `/models` routes. You can still save the endpoint and map known model IDs manually.
- OpenAI auth page shows `AuthApiFailure` / `unknown_error` immediately after clicking Start Web OAuth:
  - This is usually authorize-request compatibility (not URL-encoding on your side)
  - Verify redirect URI and originator compatibility
  - You can override defaults with environment variables:
    - `OPENAI_WEB_OAUTH_DEFAULT_REDIRECT_URI` (default: `http://localhost:1455/auth/callback`)
    - `OPENAI_WEB_OAUTH_ORIGINATOR` (default: `pi`)
    - `OPENAI_WEB_OAUTH_CLIENT_ID` (override client id if your flow requires another one)

## Security notes

- Treat refresh tokens and client secrets like passwords
- Do not paste secrets into logs, tickets, or screenshots
- Serverless Proxy persists refresh-token rotations in SQLite for restart safety
- Access tokens are cached in memory and not persisted
