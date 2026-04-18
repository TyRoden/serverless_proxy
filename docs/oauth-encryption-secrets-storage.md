# OAuth Secrets Storage and Encryption Guide

> **Status**: This document describes an encryption-ready storage architecture.  
> **Current State**: All OAuth secrets are stored in plaintext. Encryption is NOT yet enabled.  
> **Purpose**: Provides clear migration path for future AI agent to enable at-rest encryption without disruptive schema changes.

---

## Overview

The Serverless Proxy supports OAuth 2.0 authentication for endpoint connections. This document covers:

1. **Current State**: Plaintext storage (production)
2. **Encryption-Ready Schema**: Fields added to support future encryption
3. **Storage Abstraction**: Helper functions that abstract secret reads/writes
4. **Migration Path**: Step-by-step process to enable encryption
5. **Key Rotation**: Process for rotating encryption keys
6. **Disaster Recovery**: How to recover if keys are lost

---

## 1. Current State (Plaintext - Production)

### What's Stored Today

| Field | Storage | Description |
|-------|---------|-------------|
| `api_key` | Plaintext | API key for endpoint authentication |
| `oauth_client_id` | Plaintext | OAuth client ID |
| `oauth_client_secret` | Plaintext | OAuth client secret |
| `oauth_refresh_token` | Plaintext | OAuth refresh token (persisted for restart safety) |

### Storage Location

- Database: `/mnt/ai/serverless-proxy/data/proxy.db`
- Table: `endpoints`
- All values stored as TEXT columns in plaintext

### Current Behavior

- OAuth refresh tokens persist in DB for restart safety
- Access tokens cached in memory only (not persisted)
- No encryption applied

---

## 2. Encryption-Ready Schema

### New Columns Added

The following columns have been ADDED to the `endpoints` table (see `simple_bridge.py` migration section around line 1075):

```sql
-- OAUTH ENCRYPTION-READY COLUMNS (ADDITIVE - NOT YET USED)
ALTER TABLE endpoints ADD COLUMN oauth_enabled INTEGER DEFAULT 0;
ALTER TABLE endpoints ADD COLUMN oauth_grant_type TEXT DEFAULT 'refresh_token';
ALTER TABLE endpoints ADD COLUMN oauth_token_url TEXT;
ALTER TABLE endpoints ADD COLUMN oauth_client_id TEXT;
ALTER TABLE endpoints ADD COLUMN oauth_client_secret TEXT;
ALTER TABLE endpoints ADD COLUMN oauth_scope TEXT;
ALTER TABLE endpoints ADD COLUMN oauth_refresh_token TEXT;
ALTER TABLE endpoints ADD COLUMN oauth_token_expires_at INTEGER DEFAULT 0;
ALTER TABLE endpoints ADD COLUMN oauth_token_request_format TEXT DEFAULT 'json';
ALTER TABLE endpoints ADD COLUMN oauth_client_auth_method TEXT DEFAULT 'client_secret_post';

-- ENCRYPTION-READY STORAGE MODE COLUMNS (ADDITIVE - NOT YET USED)
ALTER TABLE endpoints ADD COLUMN oauth_secret_storage_mode TEXT DEFAULT 'plain';
ALTER TABLE endpoints ADD COLUMN oauth_secret_key_version TEXT DEFAULT '';
ALTER TABLE endpoints ADD COLUMN oauth_client_secret_ciphertext TEXT DEFAULT '';
ALTER TABLE endpoints ADD COLUMN oauth_client_secret_nonce TEXT DEFAULT '';
ALTER TABLE endpoints ADD COLUMN oauth_client_secret_tag TEXT DEFAULT '';
ALTER TABLE endpoints ADD COLUMN oauth_refresh_token_ciphertext TEXT DEFAULT '';
ALTER TABLE endpoints ADD COLUMN oauth_refresh_token_nonce TEXT DEFAULT '';
ALTER TABLE endpoints ADD COLUMN oauth_refresh_token_tag TEXT DEFAULT '';
ALTER TABLE endpoints ADD COLUMN oauth_secret_last_migrated_at INTEGER DEFAULT 0;
```

### Column Meanings

| Column | Type | Description | Values |
|--------|------|-------------|--------|
| `oauth_enabled` | INTEGER | Whether OAuth is enabled for this endpoint | 0, 1 |
| `oauth_grant_type` | TEXT | OAuth grant flow | `refresh_token`, `client_credentials` |
| `oauth_token_url` | TEXT | Token endpoint URL | e.g., `https://auth.openai.com/oauth/token` |
| `oauth_client_id` | TEXT | OAuth client ID | Any string |
| `oauth_client_secret` | TEXT | OAuth client secret (CURRENTLY USED) | Any string |
| `oauth_scope` | TEXT | Space-separated OAuth scopes | e.g., `openid profile email offline_access` |
| `oauth_refresh_token` | TEXT | Refresh token (persisted for restart) | Any string |
| `oauth_token_expires_at` | INTEGER | Unix timestamp when token expires | Unix timestamp |
| `oauth_token_request_format` | TEXT | Request body encoding | `json`, `form` |
| `oauth_client_auth_method` | TEXT | Client authentication method | `client_secret_post`, `client_secret_basic` |
| `oauth_secret_storage_mode` | TEXT | Storage mode for secrets | `plain`, `enc` |
| `oauth_secret_key_version` | TEXT | Key version used for encryption | Key identifier |
| `oauth_client_secret_ciphertext` | TEXT | Encrypted client secret | Base64-encoded ciphertext |
| `oauth_client_secret_nonce` | TEXT | Nonce for client secret encryption | Base64-encoded nonce |
| `oauth_client_secret_tag` | TEXT | Auth tag for client secret (AEAD) | Base64-encoded tag |
| `oauth_refresh_token_ciphertext` | TEXT | Encrypted refresh token | Base64-encoded ciphertext |
| `oauth_refresh_token_nonce` | TEXT | Nonce for refresh token encryption | Base64-encoded nonce |
| `oauth_refresh_token_tag` | TEXT | Auth tag for refresh token (AEAD) | Base64-encoded tag |
| `oauth_secret_last_migrated_at` | INTEGER | Unix timestamp of last migration | Unix timestamp |

### Why These Fields

1. **`oauth_secret_storage_mode`**: Per-row mode allows gradual migration. Rows can be `plain` (current) or `enc` (encrypted).

2. **`oauth_secret_key_version`**: Supports future key rotation without re-encrypting all rows immediately.

3. **Per-secret `{ciphertext, nonce, tag}`**: Clean AEAD model (AES-GCM or ChaCha20-Poly1305). Each secret has its own nonce/tag.

4. **`oauth_secret_last_migrated_at`**: Audit trail for migrations, helpful for troubleshooting.

---

## 3. Storage Abstraction (Helper Functions)

All secret reads and writes MUST go through these helpers. This is the key to enabling encryption later.

### Helper Signatures (CURRENT)

```python
# Read helpers - always use these instead of direct column access
def read_oauth_client_secret(endpoint_row: dict) -> str:
    """Read client secret from endpoint row. Currently returns plaintext."""
    return endpoint_row.get("oauth_client_secret", "")

def read_oauth_refresh_token(endpoint_row: dict) -> str:
    """Read refresh token from endpoint row. Currently returns plaintext."""
    return endpoint_row.get("oauth_refresh_token", "")

# Write helpers - always use these instead of direct column assignment
def write_oauth_client_secret(endpoint_id: int, value: str, mode: str = "plain") -> None:
    """Write client secret to endpoint. Currently plaintext only."""
    # TODO: When encryption enabled, encrypt based on mode
    pass

def write_oauth_refresh_token(endpoint_id: int, value: str, mode: str = "plain") -> None:
    """Write refresh token to endpoint. Currently plaintext only."""
    # TODO: When encryption enabled, encrypt based on mode
    pass
```

### Future Helper Signatures (WHEN ENCRYPTION ENABLED)

```python
def read_oauth_client_secret(endpoint_row: dict) -> str:
    """Read client secret from endpoint row. Handles both plain and enc modes."""
    storage_mode = endpoint_row.get("oauth_secret_storage_mode", "plain")
    if storage_mode == "enc":
        # TODO: Decrypt from ciphertext fields
        ciphertext = endpoint_row.get("oauth_client_secret_ciphertext", "")
        nonce = endpoint_row.get("oauth_client_secret_nonce", "")
        tag = endpoint_row.get("oauth_client_secret_tag", "")
        return decrypt(ciphertext, nonce, tag)
    else:
        return endpoint_row.get("oauth_client_secret", "")

def read_oauth_refresh_token(endpoint_row: dict) -> str:
    """Read refresh token from endpoint row. Handles both plain and enc modes."""
    storage_mode = endpoint_row.get("oauth_secret_storage_mode", "plain")
    if storage_mode == "enc":
        # TODO: Decrypt from ciphertext fields
        ciphertext = endpoint_row.get("oauth_refresh_token_ciphertext", "")
        nonce = endpoint_row.get("oauth_refresh_token_nonce", "")
        tag = endpoint_row.get("oauth_refresh_token_tag", "")
        return decrypt(ciphertext, nonce, tag)
    else:
        return endpoint_row.get("oauth_refresh_token", "")

def write_oauth_client_secret(endpoint_id: int, value: str, mode: str = "plain") -> None:
    """Write client secret to endpoint. Encrypts if mode='enc'."""
    if mode == "enc":
        ciphertext, nonce, tag = encrypt(value)
        # Write to ciphertext fields
        # Update storage_mode to 'enc'
    else:
        # Write to plaintext field oauth_client_secret
        # Keep storage_mode as 'plain'

def write_oauth_refresh_token(endpoint_id: int, value: str, mode: str = "plain") -> None:
    """Write refresh token to endpoint. Encrypts if mode='enc'."""
    # Same pattern as write_oauth_client_secret
```

### Critical Rule

> **ALL OAuth token flows MUST call these helpers only.** Never read/write directly from `oauth_client_secret` or `oauth_refresh_token` columns. This ensures encryption can be enabled later with minimal changes.

---

## 4. Migration Path (How to Enable Encryption)

### Phase A: Deploy Encryption-Supporting Code (No User Impact)

**Before starting**: Ensure you have a secure key management solution.

1. **Add encryption helpers** to `simple_bridge.py`:

```python
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import base64
import json

# Key management - load from environment or secret manager
def get_oauth_encryption_key() -> bytes:
    """Load encryption key from environment or secret manager."""
    key_b64 = os.environ.get("PROXY_OAUTH_MASTER_KEY", "")
    if not key_b64:
        raise ValueError("PROXY_OAUTH_MASTER_KEY not set - cannot encrypt")
    return base64.b64decode(key_b64)

def encrypt(plaintext: str) -> tuple[str, str, str]:
    """Encrypt plaintext using AES-GCM. Returns (ciphertext, nonce, tag) as base64."""
    key = get_oauth_encryption_key()
    nonce = os.urandom(12)  # 96-bit nonce for AES-GCM
    aesgcm = AESGCM(key)
    ciphertext_tag = aesgcm.encrypt(nonce, plaintext.encode(), None)
    # Last 16 bytes are the tag
    ciphertext = ciphertext_tag[:-16]
    tag = ciphertext_tag[-16:]
    return (
        base64.b64encode(ciphertext).decode(),
        base64.b64encode(nonce).decode(),
        base64.b64encode(tag).decode(),
    )

def decrypt(ciphertext_b64: str, nonce_b64: str, tag_b64: str) -> str:
    """Decrypt ciphertext using AES-GCM."""
    key = get_oauth_encryption_key()
    ciphertext = base64.b64decode(ciphertext_b64)
    nonce = base64.b64decode(nonce_b64)
    tag = base64.b64decode(tag_b64)
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext + tag, None)
    return plaintext.decode()
```

2. **Update helper functions** to check storage mode and decrypt/encrypt as needed.

3. **Test that plaintext mode still works** after the change (backward compatibility).

**Verification**:
- Existing endpoints work unchanged
- `curl http://localhost:8002/v1/models` works
- `POST /v1/chat/completions` works

---

### Phase B: Generate and Configure Master Key

1. **Generate a 256-bit key**:

```bash
# Generate a random 256-bit key and encode as base64
python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
```

Output example: `aGf7K9xL2mN4pQ6rS8uV0wX1yZ3aB5cD7eF9gH1jK3lM5nO7pQ`

2. **Store as Docker secret or environment variable**:

```bash
# Option A: Environment variable (less secure)
PROXY_OAUTH_MASTER_KEY=aGf7K9xL2mN4pQ6rS8uV0wX1yZ3aB5cD7eF9gH1jK3lM5nO7pQ

# Option B: Docker secrets (recommended)
echo "aGf7K9xL2mN4pQ6rS8uV0wX1yZ3aB5cD7eF9gH1jK3lM5nO7pQ" | docker secret create proxy_oauth_master_key -
```

3. **Test key availability in container**:

```bash
docker compose exec serverless-proxy python3 -c "from simple_bridge import get_oauth_encryption_key; print('Key loaded:', get_oauth_encryption_key()[:8] b'...')"
```

---

### Phase C: Background Migration (Batch Conversion)

Migrate rows in batches to avoid locking the database.

1. **Create migration script** (`migrate_oauth_secrets.py`):

```python
#!/usr/bin/env python3
"""Migrate OAuth secrets from plaintext to encrypted."""

import sqlite3
import os
import sys
sys.path.insert(0, "/mnt/ai/serverless-proxy")
from simple_bridge import get_oauth_encryption_key, encrypt

def migrate_batch(batch_size: int = 10, dry_run: bool = True):
    conn = sqlite3.connect("/mnt/ai/serverless-proxy/data/proxy.db")
    cursor = conn.cursor()
    
    # Get plaintext rows that have secrets
    cursor.execute("""
        SELECT id, oauth_client_secret, oauth_refresh_token
        FROM endpoints
        WHERE oauth_enabled = 1
          AND oauth_secret_storage_mode = 'plain'
          AND (oauth_client_secret != '' OR oauth_refresh_token != '')
        LIMIT ?
    """, (batch_size,))
    
    rows = cursor.fetchall()
    migrated = 0
    
    for endpoint_id, client_secret, refresh_token in rows:
        print(f"Migrating endpoint {endpoint_id}...")
        
        # Encrypt client_secret
        if client_secret:
            ciphertext, nonce, tag = encrypt(client_secret)
            cursor.execute("""
                UPDATE endpoints
                SET oauth_client_secret_ciphertext = ?,
                    oauth_client_secret_nonce = ?,
                    oauth_client_secret_tag = ?
                WHERE id = ?
            """, (ciphertext, nonce, tag, endpoint_id))
            # Clear plaintext
            cursor.execute("UPDATE endpoints SET oauth_client_secret = '' WHERE id = ?", (endpoint_id,))
        
        # Encrypt refresh_token
        if refresh_token:
            ciphertext, nonce, tag = encrypt(refresh_token)
            cursor.execute("""
                UPDATE endpoints
                SET oauth_refresh_token_ciphertext = ?,
                    oauth_refresh_token_nonce = ?,
                    oauth_refresh_token_tag = ?
                WHERE id = ?
            """, (ciphertext, nonce, tag, endpoint_id))
            # Clear plaintext
            cursor.execute("UPDATE endpoints SET oauth_refresh_token = '' WHERE id = ?", (endpoint_id,))
        
        # Update storage mode
        cursor.execute("""
            UPDATE endpoints
            SET oauth_secret_storage_mode = 'enc',
                oauth_secret_key_version = 'v1',
                oauth_secret_last_migrated_at = strftime('%s', 'now')
            WHERE id = ?
        """, (endpoint_id,))
        
        migrated += 1
    
    conn.commit()
    conn.close()
    
    print(f"Migrated {migrated} endpoints (dry_run={dry_run})")
    return migrated

if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    batch = 10
    for arg in sys.argv[1:]:
        if arg.isdigit():
            batch = int(arg)
    migrate_batch(batch, dry)
```

2. **Run migration in dry-run mode first**:

```bash
docker compose exec serverless-proxy python3 migrate_oauth_secrets.py --dry-run
```

3. **Run actual migration** (monitor for errors):

```bash
docker compose exec serverless-proxy python3 migrate_oauth_secrets.py
```

4. **Repeat until all rows migrated**:

```bash
# Check remaining plaintext rows
docker compose exec serverless-proxy sqlite3 /mnt/ai/serverless-proxy/data/proxy.db \
  "SELECT COUNT(*) FROM endpoints WHERE oauth_enabled = 1 AND oauth_secret_storage_mode = 'plain' AND (oauth_client_secret != '' OR oauth_refresh_token != '')"
```

---

### Phase D: Optional - Block New Plaintext Writes

Once all rows are migrated, you can optionally enforce encryption for new writes.

```python
def write_oauth_client_secret(endpoint_id: int, value: str, mode: str = None) -> None:
    """Write client secret. If mode not specified, defaults to current global setting."""
    global DEFAULT_STORAGE_MODE
    
    # If mode not specified, use global default (or 'enc' after full migration)
    if mode is None:
        mode = DEFAULT_STORAGE_MODE
    
    if mode == "enc" and not value:
        raise ValueError("Cannot write empty secret with encryption mode")
    
    # ... encryption logic
```

---

### Phase E: Optional - Cleanup Legacy Fields

After all rows are migrated and you're confident everything works:

```sql
-- Cleanup: Set plaintext fields to NULL for encrypted rows
UPDATE endpoints 
SET oauth_client_secret = NULL, 
    oauth_refresh_token = NULL 
WHERE oauth_secret_storage_mode = 'enc';

-- Verify no plaintext secrets remain
SELECT id, name FROM endpoints 
WHERE oauth_secret_storage_mode = 'plain' 
  AND (oauth_client_secret IS NOT NULL AND oauth_client_secret != '' 
    OR oauth_refresh_token IS NOT NULL AND oauth_refresh_token != '');
```

---

## 5. Key Rotation

### Prerequisites

- Current key version tracked in `oauth_secret_key_version`
- New key available alongside old key

### Rotation Process

1. **Add new key** (e.g., `PROXY_OAUTH_MASTER_KEY_V2`)

2. **Support multiple keys in helper**:

```python
def get_oauth_encryption_key(version: str = None) -> bytes:
    """Load encryption key. If version not specified, returns current key."""
    if version is None or version == "v1":
        key_b64 = os.environ.get("PROXY_OAUTH_MASTER_KEY", "")
    elif version == "v2":
        key_b64 = os.environ.get("PROXY_OAUTH_MASTER_KEY_V2", "")
    else:
        raise ValueError(f"Unknown key version: {version}")
    
    if not key_b64:
        raise ValueError(f"Key version {version} not configured")
    return base64.b64decode(key_b64)
```

3. **Re-encrypt on write** (lazy rotation):

```python
def write_oauth_client_secret(endpoint_id: int, value: str, mode: str = "enc") -> None:
    """Write client secret. Re-encrypts with current key version."""
    current_version = "v2"  # After rotation
    
    # Encrypt with current key version
    ciphertext, nonce, tag = encrypt(value)
    
    # Update row
    cursor.execute("""
        UPDATE endpoints
        SET oauth_client_secret_ciphertext = ?,
            oauth_client_secret_nonce = ?,
            oauth_client_secret_tag = ?,
            oauth_secret_key_version = ?,
            oauth_secret_storage_mode = 'enc'
        WHERE id = ?
    """, (ciphertext, nonce, tag, current_version, endpoint_id))
```

4. **Proactively rotate all rows** (optional admin task):

```bash
docker compose exec serverless-proxy python3 rotate_oauth_keys.py --from v1 --to v2
```

---

## 6. Disaster Recovery

### Scenario 1: Key Lost / Corrupted

**Symptoms**:
- `PROXY_OAUTH_MASTER_KEY` not set or invalid
- Endpoints with `oauth_secret_storage_mode = 'enc'` fail to authenticate

**Recovery Steps**:

1. **Check which endpoints are affected**:

```bash
sqlite3 /mnt/ai/serverless-proxy/data/proxy.db \
  "SELECT id, name, oauth_secret_storage_mode FROM endpoints WHERE oauth_enabled = 1"
```

2. **Reset affected endpoints to plaintext mode** (if you have the original secrets):

```sql
-- If you have the plaintext secrets stored elsewhere, restore them
UPDATE endpoints
SET oauth_secret_storage_mode = 'plain',
    oauth_client_secret = 'your-plaintext-secret',
    oauth_refresh_token = 'your-plaintext-refresh-token'
WHERE id = <endpoint_id>;
```

3. **If you don't have plaintext secrets**, users must re-authenticate with the OAuth provider to get new tokens.

### Scenario 2: Migration Failure Mid-Way

**Symptoms**:
- Some rows have `oauth_secret_storage_mode = 'enc'`
- Others have `oauth_secret_storage_mode = 'plain'`
- Mixed behavior, confusing errors

**Recovery Steps**:

1. **Pause migration**:

```bash
# Stop any running migration scripts
pkill -f migrate_oauth_secrets
```

2. **Identify broken rows**:

```sql
SELECT id, name, oauth_secret_storage_mode,
       CASE 
         WHEN oauth_client_secret_ciphertext IS NULL OR oauth_client_secret_ciphertext = '' THEN 'missing ciphertext'
         WHEN oauth_client_secret IS NOT NULL AND oauth_client_secret != '' THEN 'plaintext not cleared'
         ELSE 'ok'
       END as issue
FROM endpoints
WHERE oauth_enabled = 1 AND oauth_secret_storage_mode = 'enc';
```

3. **Fix broken rows manually** or revert to plaintext:

```sql
-- Revert specific endpoint to plaintext mode
UPDATE endpoints
SET oauth_secret_storage_mode = 'plain',
    oauth_client_secret = 'restore-original-plaintext-here',
    oauth_refresh_token = 'restore-original-refresh-here'
WHERE id = <broken_endpoint_id>;
```

4. **Retry migration** after fixing:

```bash
docker compose exec serverless-proxy python3 migrate_oauth_secrets.py --batch 1
```

### Scenario 3: Database Backup Without Keys

**Symptoms**:
- You have a DB backup from before encryption was enabled
- Current keys are different/lost
- Can't decrypt encrypted rows in the backup

**Recovery Steps**:

1. **Never restore an old backup over an encrypted database** - this will lose data.

2. **If you must restore backup**:
   - After restoring, re-run OAuth authentication flow for each endpoint
   - Or keep a copy of plaintext secrets in a secure password manager

3. **Best Practice**: Document that DB backups depend on key backups. Include key version metadata in backup logs.

---

## 7. Security Considerations

### What to Protect

| Asset | Risk | Mitigation |
|-------|------|------------|
| Master key | Lost = all secrets unrecoverable | Store in secret manager, backup securely |
| Key version metadata | Exposed = attacker knows encryption version | Don't log key versions |
| Ciphertext | Exposed alone = useless without key | Requires key to decrypt |
| Nonce | Reused = potential attack vector | Always generate fresh nonce per encryption |

### What to Log

- [OK] Key version when rotating
- [OK] Number of rows migrated
- [OK] Migration start/end times
- [OK] Errors (without secrets)

### What NOT to Log

- [NO] Any plaintext secrets
- [NO] Any ciphertext
- [NO] Any nonces
- [NO] Any auth tags
- [NO] Key material (even in base64)

### Network Security

- Token exchange endpoint MUST use HTTPS
- Validate TLS certificates (no skip verification)
- Set timeouts on token requests (recommended: 10 seconds)

---

## 8. Troubleshooting Matrix

| Symptom | Likely Cause | Resolution |
|---------|-------------|------------|
| `PROXY_OAUTH_MASTER_KEY not set` | Env var missing | Set environment variable |
| `Key version v1 not configured` | Old key rotated | Add old key or migrate rows to new version |
| `decrypt() invalid tag` | Ciphertext/tag mismatch | Row may be corrupted; restore plaintext |
| `auth fails after restart` (encrypted rows) | Key not loaded | Verify key available in container |
| `auth fails after restart` (plaintext rows) | Token expired | Re-authenticate with provider |
| Mixed plain + enc behavior | Migration incomplete | Finish migration or revert |
| `oauth_secret_storage_mode = 'enc'` but plaintext in `oauth_client_secret` | Migration bug | Identify and fix row |

---

## 9. Code References

### Key Locations in `simple_bridge.py`

| Location | Purpose |
|----------|---------|
| ~line 782-793 | `endpoints` table schema (CREATE) |
| ~line 1075-1150 | Migration section (ADD COLUMN for OAuth) |
| ~line 2214-2225 | `get_virtual_model()` - endpoint data for auth |
| ~line 2337-2374 | `create_backend_from_virtual_model()` - auth header resolution |
| ~line 5553-5644 | Admin API - endpoint CRUD |
| ~line 5884-5920 | Admin API - endpoint CRUD (alt) |

### Helper Functions to Modify

Look for these functions (TODO after encryption is enabled):

- `read_oauth_client_secret()`
- `read_oauth_refresh_token()`
- `write_oauth_client_secret()`
- `write_oauth_refresh_token()`
- `get_oauth_encryption_key()`
- `encrypt()`
- `decrypt()`

---

## 10. Quick Reference Checklist

### Before Enabling Encryption

- [ ] Generate master key (256-bit, base64-encoded)
- [ ] Store key in secret manager or environment
- [ ] Test key availability in container
- [ ] Verify plaintext mode works (baseline)
- [ ] Review helper functions are being used (not direct column access)

### Migration Steps

- [ ] Phase A: Deploy encryption code (no user impact)
- [ ] Phase B: Configure master key
- [ ] Phase C: Run background migration
- [ ] Phase D: (Optional) Block plaintext writes
- [ ] Phase E: (Optional) Clean up legacy fields

### Post-Migration

- [ ] Verify all `enc` rows authenticate correctly
- [ ] Test restart behavior
- [ ] Test key rotation
- [ ] Document key backup procedures
- [ ] Update runbook with new troubleshooting steps

---

## 11. Future AI Agent Notes

When implementing this feature, follow the phases in order:

1. **Start small**: Add helpers only, verify plaintext still works
2. **Test in dry-run**: Migration script with `--dry-run`
3. **Monitor closely**: Watch logs during migration
4. **Roll back capability**: Keep plaintext fallback until confident
5. **Document as you go**: Update this file with actual commands/outputs

Good luck!
