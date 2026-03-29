# Authentication Setup Guide

The Serverless Proxy admin dashboard can be protected by authentication. This guide explains how to set up authentication.

## Quick Start

### Disable Authentication (for fresh installs)

If you're just getting started and don't have an authentication service:

```bash
AUTH_ENABLED=false
```

The admin dashboard will be open to everyone.

### Enable Authentication

```bash
AUTH_ENABLED=true
AIMENU_URL=http://your-auth-service:5000
```

## How Authentication Works

The proxy validates sessions by calling your auth service's `/session/validate` endpoint.

### Request Format

```
GET /session/validate
Host: your-auth-service:5000
Cookie: session=your-session-token
```

### Response Format

**Valid session:**
```json
{
  "valid": true,
  "user": "username"
}
```

**Invalid session:**
```json
{
  "valid": false
}
```

## Implementing Your Own Auth Service

Create a simple Flask service that implements the `/session/validate` endpoint:

```python
from flask import Flask, request, jsonify

app = Flask(__name__)

# In production, validate against your session store/database
VALID_SESSIONS = {
    "user1-token": "user1",
    "user2-token": "user2",
}

@app.route("/session/validate")
def validate_session():
    # Check session cookie or custom header
    session_token = request.cookies.get("session") or request.headers.get("X-Session-Token")
    
    if session_token and session_token in VALID_SESSIONS:
        return jsonify({
            "valid": True,
            "user": VALID_SESSIONS[session_token]
        })
    
    return jsonify({"valid": False}), 401

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
```

### Key Requirements

1. **Endpoint:** `GET /session/validate`
2. **Accept:** Session cookie (`Cookie` header) or custom header (`X-Session-Token`)
3. **Response:** JSON with `valid: true/false`
4. **Optional:** Include `user` field for auditing
5. **Timeout:** Response should be fast (proxy has 5 second timeout)

### More Advanced: Database-Backed Sessions

For production use, validate sessions against a database:

```python
from flask import Flask, request, jsonify
import sqlite3

app = Flask(__name__)

def get_db():
    conn = sqlite3.connect('sessions.db')
    return conn

@app.route("/session/validate")
def validate_session():
    session_token = request.cookies.get("session") or request.headers.get("X-Session-Token")
    
    if not session_token:
        return jsonify({"valid": False}), 401
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id FROM sessions WHERE token = ? AND expires_at > datetime('now')",
        (session_token,)
    )
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return jsonify({"valid": True, "user": row[0]})
    
    return jsonify({"valid": False}), 401
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AUTH_ENABLED` | Enable/disable authentication | `true` |
| `AIMENU_URL` | Auth service URL | `http://localhost:5000` |

## Code Integration

To modify authentication logic, edit these functions in `simple_bridge.py`:

- `validate_session()` - for Flask admin routes
- `validate_session_fastapi()` - for FastAPI routes

Both check `AUTH_ENABLED` and call your auth service at `AIMENU_URL/session/validate`.
