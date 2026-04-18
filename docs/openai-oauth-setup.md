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
3. Keep or verify defaults:
   - URL: `https://api.openai.com`
   - OAuth enabled: `true`
   - Grant type: `refresh_token`
   - Token URL: `https://auth.openai.com/oauth/token`
   - Token request format: `json`
   - Client auth method: `client_secret_post`
4. Fill in:
   - `oauth_client_id`
   - `oauth_refresh_token`
   - `oauth_client_secret` (if required by your token source)
   - `oauth_scope` (optional)
5. Save endpoint
6. Use **Fetch Models** on that endpoint to validate token exchange
7. Map a virtual model to this endpoint and test with `POST /v1/chat/completions`

## Troubleshooting

- `401/invalid_grant`: refresh token is expired/revoked; re-authenticate and update token
- `401/invalid_client`: incorrect client ID/secret or wrong auth method
- `404` on token URL: wrong token endpoint (verify `https://auth.openai.com/oauth/token`)
- Works with API key but not OAuth: verify `oauth_enabled` and required OAuth fields are populated

## Security notes

- Treat refresh tokens and client secrets like passwords
- Do not paste secrets into logs, tickets, or screenshots
- Serverless Proxy persists refresh-token rotations in SQLite for restart safety
- Access tokens are cached in memory and not persisted
