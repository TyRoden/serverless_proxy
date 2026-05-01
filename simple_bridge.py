#!/usr/bin/env python3
"""
Universal LLM Gateway - OpenAI and Anthropic API compatible proxy

Features:
- Virtual model mapping to multiple backends (RunPod, DeepInfra, Ollama, etc.)
- OpenAI-compatible API (/v1/chat/completions, /v1/models, etc.)
- Anthropic-compatible API (/v1/messages) - works with Claude Code
- Admin UI for endpoint and model configuration
- Token usage tracking and cost calculation
- Tool call extraction from various model output formats
- Streaming support for both APIs
"""

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request as FastAPIRequest
import httpx
import os
import time
import time as time_module
import json
import asyncio
import base64
import re
import hashlib
import sqlite3
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from abc import ABC, abstractmethod
from typing import Any, Optional
from contextlib import contextmanager
import ipaddress

# Flask for admin UI
from flask import (
    Flask,
    render_template,
    request as flask_request,
    jsonify as flask_jsonify,
    redirect,
)
import secrets

# Flask app for admin routes
FLASK_PORT = int(os.getenv("FLASK_PORT", 5001))
flask_app = Flask(__name__, template_folder="templates", static_folder="static")
flask_app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))

AIMENU_URL = os.getenv("AIMENU_URL", "http://localhost:5000")


def is_auth_enabled():
    """Check if auth is enabled - reads from env at call time, not load time."""
    return os.getenv("AUTH_ENABLED", "true").lower() == "true"


# Backwards compatibility
AUTH_ENABLED = is_auth_enabled()

# Database setup
DATABASE_PATH = os.getenv("DATABASE_PATH", "/data/proxy.db")


def _get_server_timezone_name() -> str:
    tzinfo = datetime.now().astimezone().tzinfo
    if isinstance(tzinfo, ZoneInfo):
        return tzinfo.key
    name = str(tzinfo or "").strip()
    return name or "UTC"


COMMON_TIMEZONES = [
    "Server Local Time",
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Phoenix",
    "America/Anchorage",
    "Pacific/Honolulu",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Athens",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Bangkok",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Asia/Seoul",
    "Australia/Sydney",
    "Pacific/Auckland",
]


def get_db_connection():
    """Get database connection."""
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_db():
    """Context manager for database."""
    conn = get_db_connection()
    try:
        yield conn
    finally:
        conn.close()


def get_setting(key, default=None):
    """Get a setting from the database, fallback to default."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row:
                return row["value"]
    except Exception as e:
        print(f"Error getting setting {key}: {e}")
    return default


def get_display_timezone_name() -> str:
    raw_value = str(
        get_setting("display_timezone", "Server Local Time") or "Server Local Time"
    ).strip()
    if not raw_value or raw_value == "Server Local Time":
        return _get_server_timezone_name()
    try:
        ZoneInfo(raw_value)
        return raw_value
    except Exception:
        return _get_server_timezone_name()


def _get_display_timezone() -> ZoneInfo:
    return ZoneInfo(get_display_timezone_name())


def _display_date_key(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=_get_display_timezone()).strftime(
            "%Y-%m-%d"
        )
    except Exception:
        return "1970-01-01"


def _display_datetime_string(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=_get_display_timezone()).strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )
    except Exception:
        return "1970-01-01 00:00:00 UTC"


def get_api_port():
    """Get API port from DB, fallback to env var or default."""
    return int(get_setting("api_port", os.getenv("API_PORT", "8002")))


def get_flask_port():
    """Get Flask/Admin port from DB, fallback to env var or default."""
    return int(get_setting("flask_port", os.getenv("FLASK_PORT", "5001")))


def is_debug_mode():
    """Check if debug mode is enabled. Returns 'off', 'basic', or 'full'."""
    return get_setting("debug_mode", "off")


def is_payload_audit_enabled() -> bool:
    """Check if persisted payload audit snapshots are enabled."""
    value = get_setting(
        "payload_audit_enabled", os.getenv("PAYLOAD_AUDIT_ENABLED", "false")
    )
    return str(value).lower() in ("1", "true", "yes", "on")


def get_deployment_mode() -> str:
    """Return deployment mode. Defaults to internal_only for compatibility."""
    value = str(get_setting("deployment_mode", "internal_only") or "internal_only")
    value = value.strip().lower()
    if value not in ("internal_only", "internet_facing"):
        return "internal_only"
    return value


def _parse_cidr_list(raw_value: Any, fallback: Optional[list[str]] = None) -> list[ipaddress._BaseNetwork]:
    networks: list[ipaddress._BaseNetwork] = []
    entries = [part.strip() for part in str(raw_value or "").split(",") if part.strip()]
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            debug_log("warn", f"[TRUSTED_CIDR] ignoring invalid CIDR '{entry}'")
    if networks:
        return networks
    fallback = fallback or ["127.0.0.1/32"]
    parsed_fallback: list[ipaddress._BaseNetwork] = []
    for entry in fallback:
        try:
            parsed_fallback.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return parsed_fallback


def get_trusted_internal_networks() -> list[ipaddress._BaseNetwork]:
    return _parse_cidr_list(
        get_setting("trusted_internal_cidrs", "127.0.0.1/32"),
        fallback=["127.0.0.1/32"],
    )


def get_trusted_proxy_networks() -> list[ipaddress._BaseNetwork]:
    raw = os.getenv("TRUSTED_PROXY_CIDRS", "127.0.0.1/32,172.21.0.0/16")
    return _parse_cidr_list(raw, fallback=["127.0.0.1/32"])


def _ip_in_networks(ip_text: str, networks: list[ipaddress._BaseNetwork]) -> bool:
    try:
        ip_obj = ipaddress.ip_address((ip_text or "").strip())
    except ValueError:
        return False
    return any(ip_obj in network for network in networks)


def _extract_forwarded_client_ip(forwarded_for: str) -> str:
    parts = [part.strip() for part in str(forwarded_for or "").split(",") if part.strip()]
    return parts[0] if parts else ""


def resolve_effective_client_ip(request: Request) -> tuple[str, str, str]:
    direct_client_ip = (getattr(getattr(request, "client", None), "host", "") or "").strip()
    forwarded_for = (request.headers.get("x-forwarded-for") or "").strip()
    effective_client_ip = direct_client_ip
    if direct_client_ip and _ip_in_networks(direct_client_ip, get_trusted_proxy_networks()):
        forwarded_client_ip = _extract_forwarded_client_ip(forwarded_for)
        if forwarded_client_ip:
            effective_client_ip = forwarded_client_ip
    return (effective_client_ip or "-"), forwarded_for, (direct_client_ip or "-")


def is_trusted_internal_request(request: Request) -> bool:
    effective_client_ip, _, _ = resolve_effective_client_ip(request)
    return _ip_in_networks(effective_client_ip, get_trusted_internal_networks())


def extract_inbound_api_key(request: Request) -> str:
    auth_header = (request.headers.get("authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return (request.headers.get("x-api-key") or "").strip()


def verify_inbound_api_key(api_key: str) -> Optional[dict[str, Any]]:
    if not api_key:
        return None
    key_hash = _hash_inbound_api_key(api_key)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, label, key_prefix, enabled, notes, created_at, updated_at,
                   last_used_at, last_used_ip, last_used_user_agent, revoked_at, key_hash
            FROM inbound_api_keys
            WHERE enabled = 1 AND key_hash = ?
            LIMIT 1
            """,
            (key_hash,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def mark_inbound_api_key_used(key_id: int, client_ip: str, user_agent: str) -> None:
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE inbound_api_keys
                SET last_used_at = strftime('%s', 'now'),
                    last_used_ip = ?,
                    last_used_user_agent = ?,
                    updated_at = strftime('%s', 'now')
                WHERE id = ?
                """,
                (client_ip, user_agent[:255], key_id),
            )
            conn.commit()
    except Exception as e:
        debug_log("warn", f"[INBOUND_KEY] failed to update last-used metadata: {e}")


def _get_request_inbound_key_meta(request: Request) -> tuple[Optional[int], str]:
    key_id = getattr(request.state, "inbound_api_key_id", None)
    try:
        key_id = int(key_id) if key_id is not None else None
    except Exception:
        key_id = None
    label = str(getattr(request.state, "inbound_api_key_label", "") or "").strip()
    return key_id, label


def enforce_runtime_access(request: Request, internal_only: bool = False) -> Optional[JSONResponse]:
    if get_deployment_mode() == "internal_only":
        return None

    effective_client_ip, forwarded_for, direct_client_ip = resolve_effective_client_ip(request)
    debug_log(
        "info",
        f"[INGRESS_AUTH] path={request.url.path} direct_ip={direct_client_ip} effective_ip={effective_client_ip} forwarded_for={forwarded_for or '-'} mode={get_deployment_mode()} internal_only={internal_only}",
    )

    api_key = extract_inbound_api_key(request)
    key_record = verify_inbound_api_key(api_key) if api_key else None
    if key_record:
        mark_inbound_api_key_used(
            int(key_record.get("id") or 0),
            effective_client_ip,
            (request.headers.get("user-agent") or ""),
        )
        request.state.inbound_api_key_id = int(key_record.get("id") or 0) or None
        request.state.inbound_api_key_label = str(key_record.get("label") or "").strip()

    if is_trusted_internal_request(request):
        return None

    if internal_only:
        return JSONResponse(status_code=404, content={"error": "Not found"})

    if not key_record:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return None


def debug_log(level, msg):
    """Log message if debug mode is enabled. level: 'info', 'warn', 'error'."""
    mode = is_debug_mode()
    if mode == "off":
        return
    if level == "error" or level == "warn" or mode == "full":
        print(f"[DEBUG:{level.upper()}] {msg}", flush=True)


def _truncate_debug_text(value: Any, max_len: int = 240) -> str:
    text = str(value or "").replace("\n", " ").replace("\r", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _tool_args_preview(args: Any, max_len: int = 240) -> tuple[int, str]:
    if isinstance(args, (dict, list)):
        try:
            raw = json.dumps(args, ensure_ascii=False)
        except Exception:
            raw = str(args)
    elif args is None:
        raw = ""
    else:
        raw = str(args)
    raw = raw.strip()
    return len(raw), _truncate_debug_text(raw, max_len=max_len)


def _summarize_tool_calls_for_debug(
    tool_calls: list[dict[str, Any]], max_items: int = 8
) -> str:
    entries: list[str] = []
    for tc in (tool_calls or [])[:max_items]:
        fn = tc.get("function") or {}
        name = _truncate_debug_text(fn.get("name") or "", max_len=64)
        call_id = _truncate_debug_text(tc.get("id") or "", max_len=64)
        args_len, args_preview = _tool_args_preview(fn.get("arguments"), max_len=200)
        entries.append(
            f"id={call_id} name={name} args_len={args_len} args={args_preview}"
        )
    return " | ".join(entries)


# ============================================================================
# Endpoint Health Helpers
# ============================================================================

health_cache: dict[int, dict[str, Any]] = {}
health_runner_task: Optional[asyncio.Task] = None


def load_health_cache() -> None:
    """Load endpoint health records from DB into memory."""
    global health_cache
    health_cache = {}
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM endpoint_health")
            for row in cursor.fetchall():
                rec = dict(row)
                endpoint_id = int(rec.get("endpoint_id") or 0)
                if endpoint_id > 0:
                    health_cache[endpoint_id] = rec
    except Exception as e:
        debug_log("warn", f"[HEALTH] failed to load health cache: {e}")


def _persist_health_record(endpoint_id: int, rec: dict[str, Any]) -> None:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO endpoint_health
            (endpoint_id, status, failure_count, last_error, last_failure_at, circuit_until, rate_limit_info)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(endpoint_id) DO UPDATE SET
                status = excluded.status,
                failure_count = excluded.failure_count,
                last_error = excluded.last_error,
                last_failure_at = excluded.last_failure_at,
                circuit_until = excluded.circuit_until,
                rate_limit_info = excluded.rate_limit_info
            """,
            (
                endpoint_id,
                str(rec.get("status") or "healthy"),
                int(rec.get("failure_count") or 0),
                str(rec.get("last_error") or "") or None,
                int(rec.get("last_failure_at") or 0) or None,
                int(rec.get("circuit_until") or 0) or None,
                rec.get("rate_limit_info"),
            ),
        )
        conn.commit()


def update_health(endpoint_id: int, status: str, error: Optional[str] = None) -> None:
    """Set endpoint health state and persist."""
    now = int(time.time())
    rec = health_cache.get(endpoint_id, {})
    rec["status"] = status
    if status == "healthy":
        rec["failure_count"] = 0
        rec["last_error"] = None
        rec["circuit_until"] = None
    else:
        rec["failure_count"] = int(rec.get("failure_count") or 0)
        rec["last_error"] = _sanitize_activity_error_summary(error or "")
        rec["last_failure_at"] = now
    health_cache[endpoint_id] = rec
    try:
        _persist_health_record(endpoint_id, rec)
    except Exception as e:
        debug_log("warn", f"[HEALTH] persist failed endpoint_id={endpoint_id}: {e}")


def increment_health_failure(
    endpoint_id: int,
    error: str,
    threshold_override: Optional[int] = None,
    cooldown_override: Optional[int] = None,
) -> None:
    """Increment failure count and open circuit when threshold is reached."""
    now = int(time.time())
    threshold = int(threshold_override or get_setting("circuit_failure_threshold", "3") or 3)
    cooldown = int(cooldown_override or get_setting("circuit_cooldown_seconds", "300") or 300)

    rec = health_cache.get(
        endpoint_id,
        {
            "status": "healthy",
            "failure_count": 0,
            "last_error": None,
            "last_failure_at": None,
            "circuit_until": None,
            "rate_limit_info": None,
        },
    )
    rec["failure_count"] = int(rec.get("failure_count") or 0) + 1
    rec["last_error"] = _sanitize_activity_error_summary(error)
    rec["last_failure_at"] = now

    if rec["failure_count"] >= max(1, threshold):
        rec["status"] = "circuit_open"
        rec["circuit_until"] = now + max(30, cooldown)
        debug_log(
            "warn",
            f"[HEALTH] circuit opened endpoint_id={endpoint_id} failures={rec['failure_count']} cooldown={cooldown}s",
        )
    else:
        rec["status"] = "unhealthy"

    health_cache[endpoint_id] = rec
    try:
        _persist_health_record(endpoint_id, rec)
    except Exception as e:
        debug_log("warn", f"[HEALTH] persist failed endpoint_id={endpoint_id}: {e}")


def is_endpoint_circuit_open(endpoint_id: Any) -> bool:
    try:
        eid = int(endpoint_id or 0)
    except Exception:
        return False
    if eid <= 0:
        return False
    rec = health_cache.get(eid) or {}
    circuit_until = int(rec.get("circuit_until") or 0)
    if circuit_until > int(time.time()):
        return True
    if rec.get("status") == "circuit_open" and circuit_until <= int(time.time()):
        update_health(eid, "healthy")
    return False


async def health_check_runner() -> None:
    """Poll configured health endpoints while failover is enabled."""
    interval = int(get_setting("health_check_interval", "60") or 60)
    if interval <= 0:
        return

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM virtual_model_failover LIMIT 1")
            if not cursor.fetchone():
                debug_log(
                    "info",
                    "[HEALTH] skipping runner start - no virtual models with failover",
                )
                return
    except Exception:
        return

    debug_log("info", f"[HEALTH] runner started interval={interval}s")
    while True:
        await asyncio.sleep(interval)
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, health_check_url FROM endpoints WHERE enabled=1 AND COALESCE(TRIM(health_check_url), '') <> ''"
                )
                endpoints = cursor.fetchall()

            for row in endpoints:
                endpoint_id = int(row[0])
                url = str(row[1] or "").strip()
                if not url:
                    continue
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.get(url)

                    healthy = 200 <= resp.status_code < 300
                    if healthy:
                        try:
                            data = resp.json()
                            if isinstance(data, dict):
                                if data.get("healthy") is False:
                                    healthy = False
                                if str(data.get("status") or "").lower() in (
                                    "error",
                                    "down",
                                    "unhealthy",
                                ):
                                    healthy = False
                        except Exception:
                            pass

                    if healthy:
                        update_health(endpoint_id, "healthy")
                    else:
                        increment_health_failure(
                            endpoint_id,
                            f"Health check returned HTTP {resp.status_code}",
                        )
                except Exception as e:
                    increment_health_failure(endpoint_id, str(e))
        except Exception as e:
            debug_log("warn", f"[HEALTH] runner iteration failed: {e}")


def _ollama_options_from_openai(
    temperature: float,
    max_tokens: int,
    top_p: float,
    kwargs: dict,
) -> dict:
    """Map OpenAI-style sampling params into Ollama options."""
    options = {}
    if temperature is not None:
        options["temperature"] = temperature
    if max_tokens is not None:
        options["num_predict"] = max_tokens
    if top_p is not None:
        options["top_p"] = top_p

    if kwargs.get("stop") is not None:
        options["stop"] = kwargs.get("stop")
    if kwargs.get("presence_penalty") is not None:
        options["presence_penalty"] = kwargs.get("presence_penalty")
    if kwargs.get("frequency_penalty") is not None:
        options["frequency_penalty"] = kwargs.get("frequency_penalty")

    return options


def _openai_to_ollama_chat_payload(
    model: str,
    messages: list,
    stream: bool,
    temperature: float,
    max_tokens: int,
    top_p: float,
    tools: Optional[list],
    kwargs: dict,
) -> dict:
    """Build Ollama-native /api/chat payload from OpenAI-style request."""
    payload = {
        "model": model,
        "messages": _normalize_ollama_messages(messages),
        "stream": stream,
        "options": _ollama_options_from_openai(temperature, max_tokens, top_p, kwargs),
    }

    if tools:
        payload["tools"] = tools

    # Do not forward OpenAI tool_choice object to Ollama; some versions reject it.

    if kwargs.get("format") is not None:
        payload["format"] = kwargs.get("format")

    response_format = kwargs.get("response_format")
    if response_format is not None and payload.get("format") is None:
        if isinstance(response_format, str):
            if response_format == "json":
                payload["format"] = "json"
        elif isinstance(response_format, dict):
            rf_type = response_format.get("type")
            if rf_type == "json_object":
                payload["format"] = "json"
            # Skip json_schema passthrough for Ollama compatibility.

    if kwargs.get("keep_alive") is not None:
        payload["keep_alive"] = kwargs.get("keep_alive")

    return payload


def _normalize_ollama_messages(messages: list) -> list:
    """Ensure Ollama messages[].content is always a string."""
    out = []
    if not isinstance(messages, list):
        return out

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        item = dict(msg)
        content = item.get("content", "")
        if isinstance(content, list):
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    parts.append(str(block.get("text") or ""))
                elif btype == "image_url":
                    parts.append("[image]")
            item["content"] = "\n".join([p for p in parts if p]).strip()
        elif content is None:
            item["content"] = ""
        elif not isinstance(content, str):
            item["content"] = str(content)
        out.append(item)

    return out


def _ollama_tool_calls_to_openai(ollama_tool_calls: list) -> list:
    """Convert Ollama tool_calls into OpenAI tool_calls format."""
    out = []
    if not isinstance(ollama_tool_calls, list):
        return out

    for idx, tc in enumerate(ollama_tool_calls):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        name = fn.get("name") or tc.get("name") or "unknown"
        arguments = fn.get("arguments") or tc.get("arguments") or {}
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments)
        out.append(
            {
                "id": tc.get("id") or f"call_{int(time.time() * 1000)}_{idx}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments,
                },
            }
        )
    return out


def _ollama_nonstream_to_openai(result: dict, model: str) -> dict:
    """Convert Ollama /api/chat non-stream response into OpenAI format."""
    message = result.get("message", {}) if isinstance(result, dict) else {}
    content = message.get("content") or ""
    reasoning_content = message.get("thinking") or ""
    tool_calls = _ollama_tool_calls_to_openai(message.get("tool_calls") or [])
    done_reason = result.get("done_reason") if isinstance(result, dict) else None
    finish_reason = (
        "tool_calls" if tool_calls and not content else (done_reason or "stop")
    )
    if finish_reason == "tool_calls":
        finish_reason = "tool_calls"
    elif finish_reason in ("stop", "length"):
        pass
    else:
        finish_reason = "stop"

    prompt_tokens = (
        result.get("prompt_eval_count", 0) if isinstance(result, dict) else 0
    )
    completion_tokens = result.get("eval_count", 0) if isinstance(result, dict) else 0

    return {
        "id": result.get("id", f"chat-{int(time.time())}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": reasoning_content,
                    "tool_calls": tool_calls if tool_calls else None,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _ollama_stream_to_openai_sse(stream_data: str, model: str) -> str:
    """Convert Ollama JSONL stream into OpenAI-style SSE stream."""
    out_lines = []
    created = int(time.time())
    chunk_id = f"chat-{int(time.time() * 1000)}"

    for line in (stream_data or "").splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            chunk = json.loads(raw)
        except Exception:
            continue

        message = chunk.get("message", {}) if isinstance(chunk, dict) else {}
        content = message.get("content") or ""
        thinking = message.get("thinking") or ""
        ollama_tc = message.get("tool_calls") or []
        tool_calls = _ollama_tool_calls_to_openai(ollama_tc)

        delta = {}
        if content:
            delta["content"] = content
        if thinking:
            delta["reasoning_content"] = thinking
        if tool_calls:
            delta["tool_calls"] = []
            for idx, tc in enumerate(tool_calls):
                delta["tool_calls"].append(
                    {
                        "index": idx,
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": tc["function"]["arguments"],
                        },
                    }
                )

        finish_reason = None
        if chunk.get("done"):
            finish_reason = "tool_calls" if tool_calls and not content else "stop"

        openai_chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }

        if chunk.get("done"):
            prompt_tokens = chunk.get("prompt_eval_count", 0)
            completion_tokens = chunk.get("eval_count", 0)
            openai_chunk["usage"] = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }

        out_lines.append(f"data: {json.dumps(openai_chunk)}")
        out_lines.append("")

    out_lines.append("data: [DONE]")
    out_lines.append("")
    return "\n".join(out_lines)


def _openai_to_ollama_chat_response(result: dict, model: str) -> dict:
    """Convert OpenAI-style chat result into Ollama /api/chat response."""
    choice = (result.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = result.get("usage") or {}
    response = {
        "model": model,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "message": {
            "role": "assistant",
            "content": message.get("content") or "",
        },
        "done": True,
        "done_reason": choice.get("finish_reason") or "stop",
        "prompt_eval_count": usage.get("prompt_tokens", 0),
        "eval_count": usage.get("completion_tokens", 0),
    }
    if message.get("tool_calls"):
        response["message"]["tool_calls"] = message.get("tool_calls")
    return response


def _hash_text(value: str) -> str:
    """Return short stable hash for payload audit logs."""
    if not isinstance(value, str):
        value = str(value)
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _detect_base64_image_mime(base64_data: str) -> str:
    """Best-effort MIME type detection from base64 image signature."""
    if not isinstance(base64_data, str):
        return "image/png"
    sig = base64_data.strip()[:12]
    if sig.startswith("iVBOR"):
        return "image/png"
    if sig.startswith("/9j/"):
        return "image/jpeg"
    if sig.startswith("R0lGOD"):
        return "image/gif"
    if sig.startswith("UklGR"):
        return "image/webp"
    if sig.startswith("Qk"):
        return "image/bmp"
    return "image/png"


def _extract_base64_from_data_url(value: str) -> str:
    """Normalize data payload for stable digesting."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value.startswith("data:") and "base64," in value:
        return value.split("base64,", 1)[1].strip()
    return value


PAYLOAD_AUDIT_DIR = os.getenv("PAYLOAD_AUDIT_DIR", "/data/payload_audit")


def _summarize_payload_for_storage(messages: Any) -> dict:
    """Build compact per-block summary for persisted inbound/outbound comparison."""
    summary = {
        "messages": len(messages) if isinstance(messages, list) else 0,
        "blocks": [],
    }
    if not isinstance(messages, list):
        return summary

    marker_re = re.compile(r"Image\s*\(base64\):", re.IGNORECASE)

    for msg_i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content", "")

        if isinstance(content, list):
            for block_i, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "image_url":
                    image_url = block.get("image_url", {})
                    if isinstance(image_url, dict):
                        url = image_url.get("url", "")
                    else:
                        url = image_url if isinstance(image_url, str) else ""
                    b64 = _extract_base64_from_data_url(url)
                    summary["blocks"].append(
                        {
                            "message_index": msg_i,
                            "block_index": block_i,
                            "role": role,
                            "type": "image_url",
                            "base64_len": len(b64),
                            "digest": _hash_text(b64),
                            "prefix": b64[:32],
                            "suffix": b64[-32:] if len(b64) > 32 else b64,
                        }
                    )
                elif btype == "text":
                    text = block.get("text", "")
                    if marker_re.search(text):
                        parts = marker_re.split(text, maxsplit=1)
                        b64 = _extract_base64_from_data_url(
                            parts[1].strip() if len(parts) > 1 else text
                        )
                        b64_clean = re.sub(r"[^A-Za-z0-9+/=]", "", b64)
                        summary["blocks"].append(
                            {
                                "message_index": msg_i,
                                "block_index": block_i,
                                "role": role,
                                "type": "text_base64",
                                "base64_len": len(b64_clean),
                                "digest": _hash_text(b64_clean),
                                "prefix": b64_clean[:32],
                                "suffix": (
                                    b64_clean[-32:]
                                    if len(b64_clean) > 32
                                    else b64_clean
                                ),
                            }
                        )
        elif isinstance(content, str):
            if marker_re.search(content):
                parts = marker_re.split(content, maxsplit=1)
                b64 = _extract_base64_from_data_url(
                    parts[1].strip() if len(parts) > 1 else content
                )
                b64_clean = re.sub(r"[^A-Za-z0-9+/=]", "", b64)
                summary["blocks"].append(
                    {
                        "message_index": msg_i,
                        "block_index": 0,
                        "role": role,
                        "type": "text_base64",
                        "base64_len": len(b64_clean),
                        "digest": _hash_text(b64_clean),
                        "prefix": b64_clean[:32],
                        "suffix": b64_clean[-32:] if len(b64_clean) > 32 else b64_clean,
                    }
                )

    return summary


def _persist_payload_snapshot(
    request_id: str,
    stage: str,
    model: str,
    messages: Any,
    audit_summary: Optional[dict] = None,
):
    """Persist payload comparison artifacts to disk for forensic debugging."""
    if not is_payload_audit_enabled():
        return

    compact = _summarize_payload_for_storage(messages)
    has_relevant = (
        compact.get("blocks")
        or (audit_summary and audit_summary.get("image_blocks", 0) > 0)
        or (audit_summary and audit_summary.get("base64_text_blocks", 0) > 0)
    )
    if not has_relevant:
        return

    try:
        out_dir = Path(PAYLOAD_AUDIT_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{request_id}_{stage}.json"
        payload = {
            "request_id": request_id,
            "stage": stage,
            "model": model,
            "ts": int(time.time()),
            "audit": audit_summary or {},
            "compact": compact,
        }
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(
            f"[PAYLOAD_STORE] request_id={request_id} stage={stage} path={out_path}",
            flush=True,
        )
    except Exception as e:
        debug_log(
            "warn",
            f"[PAYLOAD_STORE_ERR] request_id={request_id} stage={stage} error={e}",
        )


def _normalize_inline_base64_messages(messages: Any) -> tuple[Any, int]:
    """Convert 'Image (base64): ...' payloads into image_url blocks."""
    if not isinstance(messages, list):
        return messages, 0

    marker_re = re.compile(r"Image\s*\(base64\):", re.IGNORECASE)

    def _normalize_text_content(content: str) -> tuple[Any, int]:
        if not isinstance(content, str) or not marker_re.search(content):
            return content, 0

        blocks = []
        last_end = 0
        local_converted = 0
        for match in marker_re.finditer(content):
            prefix = content[last_end : match.start()]
            if prefix.strip():
                blocks.append({"type": "text", "text": prefix.strip()})

            next_match = marker_re.search(content, match.end())
            candidate = (
                content[match.end() : next_match.start()]
                if next_match
                else content[match.end() :]
            )
            compact = re.sub(r"\s+", "", candidate)
            if compact.startswith("data:image/"):
                image_url = compact
            else:
                image_b64 = _extract_base64_from_data_url(compact)
                image_b64 = re.sub(r"[^A-Za-z0-9+/=]", "", image_b64)
                if len(image_b64) < 64:
                    blocks.append(
                        {
                            "type": "text",
                            "text": ("Image (base64):" + candidate).strip(),
                        }
                    )
                    last_end = next_match.start() if next_match else len(content)
                    continue
                mime = _detect_base64_image_mime(image_b64)
                image_url = f"data:{mime};base64,{image_b64}"

            blocks.append({"type": "image_url", "image_url": {"url": image_url}})
            local_converted += 1
            last_end = next_match.start() if next_match else len(content)

        suffix = content[last_end:]
        if suffix.strip():
            blocks.append({"type": "text", "text": suffix.strip()})

        if local_converted > 0:
            return blocks, local_converted
        return content, 0

    converted = 0
    normalized_messages = []

    for msg in messages:
        if not isinstance(msg, dict):
            normalized_messages.append(msg)
            continue
        if msg.get("role") != "user":
            normalized_messages.append(msg)
            continue

        content = msg.get("content")

        if isinstance(content, str):
            normalized_content, local_converted = _normalize_text_content(content)
            if local_converted > 0:
                msg_copy = dict(msg)
                msg_copy["content"] = normalized_content
                normalized_messages.append(msg_copy)
                converted += local_converted
            else:
                normalized_messages.append(msg)
            continue

        if isinstance(content, list):
            new_blocks = []
            local_converted = 0
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_val = block.get("text", "")
                    normalized_content, block_converted = _normalize_text_content(
                        text_val
                    )
                    if block_converted > 0 and isinstance(normalized_content, list):
                        new_blocks.extend(normalized_content)
                        local_converted += block_converted
                    else:
                        new_blocks.append(block)
                else:
                    new_blocks.append(block)

            if local_converted > 0:
                msg_copy = dict(msg)
                msg_copy["content"] = new_blocks
                normalized_messages.append(msg_copy)
                converted += local_converted
            else:
                normalized_messages.append(msg)
            continue

        normalized_messages.append(msg)

    return normalized_messages, converted


def _audit_message_payload(messages):
    """Collect payload audit info for image/base64 content."""
    summary = {
        "messages": len(messages) if isinstance(messages, list) else 0,
        "image_blocks": 0,
        "data_urls": 0,
        "base64_text_blocks": 0,
        "digests": [],
    }
    if not isinstance(messages, list):
        return summary

    marker_re = re.compile(r"Image\s*\(base64\):", re.IGNORECASE)

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")

        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "image_url":
                    summary["image_blocks"] += 1
                    image_url = block.get("image_url", {})
                    if isinstance(image_url, dict):
                        url = image_url.get("url", "")
                    else:
                        url = image_url if isinstance(image_url, str) else ""
                    if isinstance(url, str) and url.startswith("data:"):
                        summary["data_urls"] += 1
                    summary["digests"].append(
                        _hash_text(_extract_base64_from_data_url(url))
                    )
                elif btype == "text":
                    text = block.get("text", "")
                    if marker_re.search(text):
                        summary["base64_text_blocks"] += 1
                        parts = marker_re.split(text, maxsplit=1)
                        b64 = parts[1].strip() if len(parts) > 1 else text
                        summary["digests"].append(
                            _hash_text(_extract_base64_from_data_url(b64))
                        )
        elif isinstance(content, str):
            if marker_re.search(content):
                summary["base64_text_blocks"] += 1
                parts = marker_re.split(content, maxsplit=1)
                b64 = parts[1].strip() if len(parts) > 1 else content
                summary["digests"].append(
                    _hash_text(_extract_base64_from_data_url(b64))
                )

    return summary


def _log_payload_audit(stage: str, request_id: str, summary: dict):
    """Log concise in/out payload audit when image/base64 content is present."""
    if not summary:
        return
    has_relevant = (
        summary.get("image_blocks", 0) > 0
        or summary.get("data_urls", 0) > 0
        or summary.get("base64_text_blocks", 0) > 0
    )
    if not has_relevant:
        return
    digests_preview = summary.get("digests", [])[:4]
    print(
        f"[PAYLOAD_AUDIT] request_id={request_id} stage={stage} "
        f"messages={summary.get('messages', 0)} image_blocks={summary.get('image_blocks', 0)} "
        f"data_urls={summary.get('data_urls', 0)} base64_text={summary.get('base64_text_blocks', 0)} "
        f"digests={digests_preview}",
        flush=True,
    )


def _looks_like_data_block(text: str) -> bool:
    """Detect large data/base64 blocks that should never be regex-normalized."""
    if not isinstance(text, str):
        return False
    if "Image (base64):" in text:
        return True
    if "data:image/" in text and "base64," in text:
        return True
    if len(text) > 800 and re.search(r"[A-Za-z0-9+/]{600,}={0,2}", text):
        return True
    return False


def init_database():
    """Initialize database tables."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Endpoints table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS endpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                api_key TEXT,
                endpoint_type TEXT DEFAULT 'ollama',
                custom_headers TEXT,
                priority INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                updated_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)

        # Virtual models table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS virtual_models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                endpoint_id INTEGER NOT NULL,
                actual_model TEXT NOT NULL,
                description TEXT,
                cost_per_1m_tokens_in REAL DEFAULT 0,
                cost_per_1m_tokens_out REAL DEFAULT 0,
                cost_per_1m_tokens_in_cached REAL DEFAULT 0,
                cost_per_1m_tokens_out_cached REAL DEFAULT 0,
                disable_streaming INTEGER DEFAULT 0,
                force_non_streaming INTEGER DEFAULT 0,
                custom_headers TEXT,
                enabled INTEGER DEFAULT 1,
                max_tokens INTEGER DEFAULT 0,
                temperature REAL DEFAULT 0,
                top_p REAL DEFAULT 1.0,
                system_prompt TEXT,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                updated_at INTEGER DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (endpoint_id) REFERENCES endpoints(id)
            )
        """)

        # Request usage tracking table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS request_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                virtual_model TEXT NOT NULL,
                endpoint_name TEXT,
                endpoint_id INTEGER,
                inbound_api_key_id INTEGER,
                inbound_api_key_label TEXT,
                request_type TEXT DEFAULT 'chat',
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                cached_input_tokens INTEGER DEFAULT 0,
                cache_creation_tokens INTEGER DEFAULT 0,
                cost_estimate REAL DEFAULT 0,
                cost_in REAL DEFAULT 0,
                cost_out REAL DEFAULT 0,
                cached_cost_estimate REAL DEFAULT 0,
                response_time_ms INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (endpoint_id) REFERENCES endpoints(id)
            )
        """)

        # Embedding usage tracking table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS embedding_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                virtual_model TEXT NOT NULL,
                endpoint_name TEXT,
                endpoint_id INTEGER,
                inbound_api_key_id INTEGER,
                inbound_api_key_label TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                cost_estimate REAL DEFAULT 0,
                response_time_ms INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (endpoint_id) REFERENCES endpoints(id)
            )
        """)

        # Recent activity table (operational metadata only)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recent_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                request_id TEXT,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                request_type TEXT NOT NULL,
                virtual_model TEXT,
                actual_model TEXT,
                endpoint_name TEXT,
                endpoint_id INTEGER,
                endpoint_type TEXT,
                inbound_api_key_id INTEGER,
                inbound_api_key_label TEXT,
                client_ip TEXT,
                forwarded_for TEXT,
                x_source TEXT,
                user_agent TEXT,
                stream INTEGER DEFAULT 0,
                status_code INTEGER,
                outcome TEXT,
                response_time_ms INTEGER DEFAULT 0,
                error_summary TEXT
            )
        """)

        # Endpoint health table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS endpoint_health (
                endpoint_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'healthy',
                failure_count INTEGER DEFAULT 0,
                last_error TEXT,
                last_failure_at INTEGER,
                circuit_until INTEGER,
                rate_limit_info TEXT,
                FOREIGN KEY (endpoint_id) REFERENCES endpoints(id)
            )
        """)

        # Virtual model failover config table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS virtual_model_failover (
                virtual_model_id INTEGER PRIMARY KEY,
                strategy TEXT NOT NULL,
                targets TEXT NOT NULL,
                max_attempts INTEGER,
                cooldown_seconds INTEGER,
                failure_threshold INTEGER,
                FOREIGN KEY (virtual_model_id) REFERENCES virtual_models(id)
            )
        """)

        # Response cache table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS response_cache (
                cache_key TEXT PRIMARY KEY,
                response_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                hit_count INTEGER DEFAULT 0,
                model TEXT NOT NULL,
                cost_in REAL DEFAULT 0,
                cost_out REAL DEFAULT 0
            )
        """)

        # Settings table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS inbound_api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL UNIQUE,
                key_prefix TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                notes TEXT DEFAULT '',
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                updated_at INTEGER DEFAULT (strftime('%s', 'now')),
                last_used_at INTEGER,
                last_used_ip TEXT,
                last_used_user_agent TEXT,
                revoked_at INTEGER
            )
        """)

        # Tool patterns table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tool_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_name TEXT NOT NULL UNIQUE,
                pattern_type TEXT NOT NULL,
                regex_pattern TEXT NOT NULL,
                tool_name TEXT,
                tool_name_group INTEGER,
                tool_name_json_path TEXT,
                tool_name_mapping TEXT,
                parameter_mapping TEXT,
                enabled INTEGER DEFAULT 1,
                priority INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                updated_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)

        # Auto-seed patterns if table is empty
        cursor.execute("SELECT COUNT(*) FROM tool_patterns")
        if cursor.fetchone()[0] == 0:
            seed_patterns = [
                (
                    "fence_json",
                    "fence",
                    r"```\s*(\w*)\s*\n?(.*?)```",
                    None,
                    None,
                    "name",
                    "{}",
                    "{}",
                    100,
                ),
                (
                    "inline_json",
                    "inline",
                    r"\{.*?\"name\"\s*:\s*\"(\w+)\".*?\"arguments\"\s*:\s*(\{[^}]*\})",
                    None,
                    1,
                    None,
                    "{}",
                    "{}",
                    90,
                ),
                (
                    "tool_use",
                    "xml",
                    r'<tool_use\s+code\s+name="(\w+)"\s*>(.*?)</tool_use>',
                    None,
                    1,
                    None,
                    "{}",
                    "{}",
                    80,
                ),
                (
                    "tool_code",
                    "xml",
                    r"<tool_code>(.*?)</tool_code>",
                    None,
                    None,
                    "name",
                    "{}",
                    "{}",
                    80,
                ),
                (
                    "tool_call",
                    "xml",
                    r"<tool_call>(.*?)</tool_call>",
                    None,
                    None,
                    "name",
                    "{}",
                    "{}",
                    80,
                ),
                (
                    "tool_call_alt",
                    "bracket",
                    r"\[TOOL_CALL\]\s*(\{.*?\})\s*\[/TOOL_CALL\]",
                    None,
                    None,
                    "name",
                    "{}",
                    "{}",
                    85,
                ),
                (
                    "tool_call_nested_xml",
                    "xml",
                    r"<tool_call>\s*<function=(\w+)>\s*<parameter=(\w+)>\s*(.*?)\s*</parameter>\s*</function>\s*</tool_call>",
                    None,
                    1,
                    None,
                    "{}",
                    '{"file_path":"filePath"}',
                    86,
                ),
                (
                    "tool_call_bare_param",
                    "xml",
                    r"<tool_call>\s*(\w+)\s*<parameter=(\w+)>\s*(.*?)\s*</parameter>\s*(?:</function>\s*)?(?:</tool_call>)?",
                    None,
                    1,
                    None,
                    "{}",
                    '{"file_path":"filePath","command":"command"}',
                    87,
                ),
                (
                    "qwen_xml_named_params",
                    "xml",
                    r"<tool_call>\s*<function=(\w+)>\s*([\s\S]*?)\s*</function>\s*(?:</tool_call>)?",
                    None,
                    1,
                    None,
                    '{"bash":"bash","read":"read","write":"write","edit":"edit","glob":"glob","grep":"grep","task":"task"}',
                    '{"file_path":"filePath"}',
                    97,
                ),
                (
                    "qwen_xml_call",
                    "xml",
                    r"<tool_call>\s*<function=(\w+)>\s*<parameter=command>\s*([\s\S]*?)\s*</parameter>(?:\s*<parameter=description>\s*[\s\S]*?\s*</parameter>)?\s*(?:</function>\s*)?(?:</tool_call>)?",
                    None,
                    1,
                    None,
                    '{"bash":"bash","read":"read","write":"write","edit":"edit","glob":"glob","grep":"grep","task":"task"}',
                    "{}",
                    95,
                ),
                (
                    "qwen_xml_read_filepath",
                    "xml",
                    r"<tool_call>\s*<function=read>\s*<parameter=filePath>\s*([\s\S]*?)\s*</parameter>\s*(?:</function>\s*)?(?:</tool_call>)?",
                    "read",
                    None,
                    None,
                    "{}",
                    '{"file_path":"filePath"}',
                    96,
                ),
                (
                    "bracket_tool",
                    "bracket",
                    r"\[tool\](\w+)\[/tool\]\s*(\{.*?\})",
                    None,
                    1,
                    None,
                    "{}",
                    "{}",
                    70,
                ),
                (
                    "action",
                    "action",
                    r"\[([a-zA-Z][^\]]+)\]",
                    None,
                    None,
                    None,
                    '{"searching":"grep","search":"grep","using task":"task","task":"task","reading":"read","read":"read","listing":"glob","ls":"glob","glob":"glob","writing":"write","write":"write","editing":"edit","edit":"edit","running":"bash","bash":"bash","executing":"bash"}',
                    '{"action":"action"}',
                    60,
                ),
            ]
            cursor.executemany(
                """
                INSERT INTO tool_patterns 
                (pattern_name, pattern_type, regex_pattern, tool_name, tool_name_group, tool_name_json_path, tool_name_mapping, parameter_mapping, priority)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                seed_patterns,
            )

        # Insert default settings if not exist
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('api_port', '8002')"
        )
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('flask_port', '5001')"
        )
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('aimenu_url', 'http://localhost:5000')"
        )
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('use_ai_queue', 'false')"
        )
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_queue_url', 'http://host.docker.internal:8102')"
        )
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('debug_mode', 'basic')"
        )
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('payload_audit_enabled', 'false')"
        )
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('auth_enabled', 'false')"
        )
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('deployment_mode', 'internal_only')"
        )
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('trusted_internal_cidrs', '127.0.0.1/32')"
        )
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('cache_ttl_chat', '300')"
        )
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('cache_ttl_embeddings', '3600')"
        )
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('health_check_interval', '60')"
        )
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('circuit_failure_threshold', '3')"
        )
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('circuit_failure_window', '60')"
        )
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('circuit_cooldown_seconds', '300')"
        )

        conn.commit()

        # Migration: Add new columns if they don't exist
        try:
            cursor.execute("SELECT custom_headers FROM endpoints LIMIT 1")
        except:
            try:
                cursor.execute("ALTER TABLE endpoints ADD COLUMN custom_headers TEXT")
            except:
                pass

        try:
            cursor.execute("SELECT health_check_url FROM endpoints LIMIT 1")
        except:
            try:
                cursor.execute(
                    "ALTER TABLE endpoints ADD COLUMN health_check_url TEXT DEFAULT ''"
                )
            except:
                pass

        try:
            cursor.execute("SELECT cost_per_1m_tokens_in FROM virtual_models LIMIT 1")
        except:
            cursor.execute(
                "ALTER TABLE virtual_models ADD COLUMN cost_per_1m_tokens_in REAL DEFAULT 0"
            )
            cursor.execute(
                "ALTER TABLE virtual_models ADD COLUMN cost_per_1m_tokens_out REAL DEFAULT 0"
            )

        try:
            cursor.execute(
                "SELECT cost_per_1k_tokens_in FROM virtual_models WHERE cost_per_1k_tokens_in > 0 LIMIT 1"
            )
            if cursor.fetchone():
                cursor.execute("""
                    UPDATE virtual_models 
                    SET cost_per_1m_tokens_in = COALESCE(cost_per_1k_tokens_in, 0),
                        cost_per_1m_tokens_out = COALESCE(cost_per_1k_tokens_out, 0)
                    WHERE cost_per_1k_tokens_in > 0 OR cost_per_1k_tokens_out > 0
                """)
                conn.commit()
        except:
            pass

        try:
            cursor.execute(
                "SELECT cost_per_1m_tokens_in_cached FROM virtual_models LIMIT 1"
            )
        except:
            try:
                cursor.execute(
                    "ALTER TABLE virtual_models ADD COLUMN cost_per_1m_tokens_in_cached REAL DEFAULT 0"
                )
            except:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE virtual_models ADD COLUMN cost_per_1m_tokens_out_cached REAL DEFAULT 0"
                )
            except:
                pass

        try:
            cursor.execute(
                "ALTER TABLE virtual_models ADD COLUMN force_non_streaming INTEGER DEFAULT 0"
            )
        except:
            pass
        try:
            cursor.execute("ALTER TABLE virtual_models ADD COLUMN custom_headers TEXT")
        except:
            pass

        try:
            cursor.execute("SELECT max_tokens FROM virtual_models LIMIT 1")
        except:
            try:
                cursor.execute(
                    "ALTER TABLE virtual_models ADD COLUMN max_tokens INTEGER DEFAULT 0"
                )
            except:
                pass

        try:
            cursor.execute("SELECT temperature FROM virtual_models LIMIT 1")
        except:
            try:
                cursor.execute(
                    "ALTER TABLE virtual_models ADD COLUMN temperature REAL DEFAULT 0"
                )
            except:
                pass

        try:
            cursor.execute("SELECT top_p FROM virtual_models LIMIT 1")
        except:
            try:
                cursor.execute(
                    "ALTER TABLE virtual_models ADD COLUMN top_p REAL DEFAULT 1.0"
                )
            except:
                pass

        try:
            cursor.execute("SELECT system_prompt FROM virtual_models LIMIT 1")
        except:
            try:
                cursor.execute(
                    "ALTER TABLE virtual_models ADD COLUMN system_prompt TEXT"
                )
            except:
                pass

        try:
            cursor.execute("SELECT show_reasoning FROM virtual_models LIMIT 1")
        except:
            try:
                cursor.execute(
                    "ALTER TABLE virtual_models ADD COLUMN show_reasoning INTEGER DEFAULT 1"
                )
            except:
                pass

        try:
            cursor.execute("SELECT cache_enabled FROM virtual_models LIMIT 1")
        except:
            try:
                cursor.execute(
                    "ALTER TABLE virtual_models ADD COLUMN cache_enabled INTEGER DEFAULT 1"
                )
            except:
                pass

        try:
            cursor.execute("SELECT cost_in FROM request_usage LIMIT 1")
        except:
            try:
                cursor.execute(
                    "ALTER TABLE request_usage ADD COLUMN cost_in REAL DEFAULT 0"
                )
            except:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE request_usage ADD COLUMN cost_out REAL DEFAULT 0"
                )
            except:
                pass

        try:
            cursor.execute("SELECT cached_input_tokens FROM request_usage LIMIT 1")
        except:
            try:
                cursor.execute(
                    "ALTER TABLE request_usage ADD COLUMN cached_input_tokens INTEGER DEFAULT 0"
                )
            except:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE request_usage ADD COLUMN cache_creation_tokens INTEGER DEFAULT 0"
                )
            except:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE request_usage ADD COLUMN cached_cost_estimate REAL DEFAULT 0"
                )
            except:
                pass

        try:
            cursor.execute("SELECT cost_in FROM embedding_usage LIMIT 1")
        except:
            try:
                cursor.execute(
                    "ALTER TABLE embedding_usage ADD COLUMN cost_in REAL DEFAULT 0"
                )
            except:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE embedding_usage ADD COLUMN cost_out REAL DEFAULT 0"
                )
            except:
                pass

        try:
            cursor.execute("SELECT cache_attempted FROM request_usage LIMIT 1")
        except:
            try:
                cursor.execute(
                    "ALTER TABLE request_usage ADD COLUMN cache_attempted INTEGER DEFAULT 0"
                )
            except:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE request_usage ADD COLUMN cache_hit INTEGER DEFAULT 0"
                )
            except:
                pass

        try:
            cursor.execute("SELECT inbound_api_key_id FROM request_usage LIMIT 1")
        except:
            try:
                cursor.execute(
                    "ALTER TABLE request_usage ADD COLUMN inbound_api_key_id INTEGER"
                )
            except:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE request_usage ADD COLUMN inbound_api_key_label TEXT"
                )
            except:
                pass

        try:
            cursor.execute("SELECT inbound_api_key_id FROM embedding_usage LIMIT 1")
        except:
            try:
                cursor.execute(
                    "ALTER TABLE embedding_usage ADD COLUMN inbound_api_key_id INTEGER"
                )
            except:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE embedding_usage ADD COLUMN inbound_api_key_label TEXT"
                )
            except:
                pass

        try:
            cursor.execute("SELECT inbound_api_key_id FROM recent_activity LIMIT 1")
        except:
            try:
                cursor.execute(
                    "ALTER TABLE recent_activity ADD COLUMN inbound_api_key_id INTEGER"
                )
            except:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE recent_activity ADD COLUMN inbound_api_key_label TEXT"
                )
            except:
                pass

        # Migration: Add OAuth columns to endpoints table (encryption-ready schema)
        oauth_columns = [
            "oauth_enabled INTEGER DEFAULT 0",
            "oauth_grant_type TEXT DEFAULT 'refresh_token'",
            "oauth_token_url TEXT",
            "oauth_client_id TEXT",
            "oauth_client_secret TEXT",
            "oauth_scope TEXT",
            "oauth_refresh_token TEXT",
            "oauth_token_expires_at INTEGER DEFAULT 0",
            "oauth_token_request_format TEXT DEFAULT 'json'",
            "oauth_client_auth_method TEXT DEFAULT 'client_secret_post'",
            "oauth_secret_storage_mode TEXT DEFAULT 'plain'",
            "oauth_secret_key_version TEXT DEFAULT ''",
            "oauth_client_secret_ciphertext TEXT DEFAULT ''",
            "oauth_client_secret_nonce TEXT DEFAULT ''",
            "oauth_client_secret_tag TEXT DEFAULT ''",
            "oauth_refresh_token_ciphertext TEXT DEFAULT ''",
            "oauth_refresh_token_nonce TEXT DEFAULT ''",
            "oauth_refresh_token_tag TEXT DEFAULT ''",
            "oauth_secret_last_migrated_at INTEGER DEFAULT 0",
        ]
        for col_def in oauth_columns:
            try:
                cursor.execute(f"ALTER TABLE endpoints ADD COLUMN {col_def}")
            except:
                pass

        # Create indexes for performance
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_usage_virtual_model ON request_usage(virtual_model)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_usage_created_at ON request_usage(created_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_usage_inbound_api_key_id ON request_usage(inbound_api_key_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_embedding_usage_virtual_model ON embedding_usage(virtual_model)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_embedding_usage_created_at ON embedding_usage(created_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_embedding_usage_inbound_api_key_id ON embedding_usage(inbound_api_key_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_recent_activity_created_at ON recent_activity(created_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_recent_activity_status ON recent_activity(status_code)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_recent_activity_virtual_model ON recent_activity(virtual_model)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_recent_activity_client_ip ON recent_activity(client_ip)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_recent_activity_inbound_api_key_id ON recent_activity(inbound_api_key_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_endpoint_health_endpoint_id ON endpoint_health(endpoint_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_response_cache_expires_at ON response_cache(expires_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_response_cache_model ON response_cache(model)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_inbound_api_keys_enabled ON inbound_api_keys(enabled)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_inbound_api_keys_last_used_at ON inbound_api_keys(last_used_at)"
        )

        # Migration: ensure nested tool_call XML pattern exists
        cursor.execute(
            """
            INSERT OR IGNORE INTO tool_patterns
            (pattern_name, pattern_type, regex_pattern, tool_name, tool_name_group, tool_name_json_path, tool_name_mapping, parameter_mapping, priority)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tool_call_nested_xml",
                "xml",
                r"<tool_call>\s*<function=(\w+)>\s*<parameter=(\w+)>\s*(.*?)\s*</parameter>\s*</function>\s*</tool_call>",
                None,
                1,
                None,
                "{}",
                '{"file_path":"filePath"}',
                86,
            ),
        )

        cursor.execute(
            """
            INSERT OR IGNORE INTO tool_patterns
            (pattern_name, pattern_type, regex_pattern, tool_name, tool_name_group, tool_name_json_path, tool_name_mapping, parameter_mapping, priority)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tool_call_bare_param",
                "xml",
                r"<tool_call>\s*(\w+)\s*<parameter=(\w+)>\s*(.*?)\s*</parameter>\s*(?:</function>\s*)?(?:</tool_call>)?",
                None,
                1,
                None,
                "{}",
                '{"file_path":"filePath","command":"command"}',
                87,
            ),
        )

        # Migration: update bare tool_call pattern to allow missing </tool_call>
        cursor.execute(
            """
            UPDATE tool_patterns
            SET regex_pattern = ?
            WHERE pattern_name = 'tool_call_bare_param'
            """,
            (
                r"<tool_call>\s*(\w+)\s*<parameter=(\w+)>\s*(.*?)\s*</parameter>\s*(?:</function>\s*)?(?:</tool_call>)?",
            ),
        )

        # Migration: ensure Qwen XML tool-call patterns exist (for existing installs)
        cursor.execute(
            """
            INSERT OR IGNORE INTO tool_patterns
            (pattern_name, pattern_type, regex_pattern, tool_name, tool_name_group, tool_name_json_path, tool_name_mapping, parameter_mapping, priority)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "qwen_xml_named_params",
                "xml",
                r"<tool_call>\s*<function=(\w+)>\s*([\s\S]*?)\s*</function>\s*(?:</tool_call>)?",
                None,
                1,
                None,
                '{"bash":"bash","read":"read","write":"write","edit":"edit","glob":"glob","grep":"grep","task":"task"}',
                '{"file_path":"filePath"}',
                97,
            ),
        )
        cursor.execute(
            """
            INSERT OR IGNORE INTO tool_patterns
            (pattern_name, pattern_type, regex_pattern, tool_name, tool_name_group, tool_name_json_path, tool_name_mapping, parameter_mapping, priority)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "qwen_xml_call",
                "xml",
                r"<tool_call>\s*<function=(\w+)>\s*<parameter=command>\s*([\s\S]*?)\s*</parameter>(?:\s*<parameter=description>\s*[\s\S]*?\s*</parameter>)?\s*(?:</function>\s*)?(?:</tool_call>)?",
                None,
                1,
                None,
                '{"bash":"bash","read":"read","write":"write","edit":"edit","glob":"glob","grep":"grep","task":"task"}',
                "{}",
                95,
            ),
        )
        cursor.execute(
            """
            INSERT OR IGNORE INTO tool_patterns
            (pattern_name, pattern_type, regex_pattern, tool_name, tool_name_group, tool_name_json_path, tool_name_mapping, parameter_mapping, priority)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "qwen_xml_read_filepath",
                "xml",
                r"<tool_call>\s*<function=read>\s*<parameter=filePath>\s*([\s\S]*?)\s*</parameter>\s*(?:</function>\s*)?(?:</tool_call>)?",
                "read",
                None,
                None,
                "{}",
                '{"file_path":"filePath"}',
                96,
            ),
        )

        # Migration: keep Qwen XML regex patterns up to date
        cursor.execute(
            """
            UPDATE tool_patterns
            SET regex_pattern = ?
            WHERE pattern_name = 'qwen_xml_named_params'
            """,
            (
                r"<tool_call>\s*<function=(\w+)>\s*([\s\S]*?)\s*</function>\s*(?:</tool_call>)?",
            ),
        )
        cursor.execute(
            """
            UPDATE tool_patterns
            SET regex_pattern = ?
            WHERE pattern_name = 'qwen_xml_call'
            """,
            (
                r"<tool_call>\s*<function=(\w+)>\s*<parameter=command>\s*([\s\S]*?)\s*</parameter>(?:\s*<parameter=description>\s*[\s\S]*?\s*</parameter>)?\s*(?:</function>\s*)?(?:</tool_call>)?",
            ),
        )
        cursor.execute(
            """
            UPDATE tool_patterns
            SET regex_pattern = ?
            WHERE pattern_name = 'qwen_xml_read_filepath'
            """,
            (
                r"<tool_call>\s*<function=read>\s*<parameter=filePath>\s*([\s\S]*?)\s*</parameter>\s*(?:</function>\s*)?(?:</tool_call>)?",
            ),
        )

        conn.commit()


# Initialize database on startup
init_database()


# ============================================================================
# Token Usage Logging Functions
# ============================================================================


def get_virtual_model_cost(virtual_model_name):
    """Get cost_per_1m_tokens for a virtual model (both in and out)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT cost_per_1m_tokens_in, cost_per_1m_tokens_out FROM virtual_models WHERE name = ?",
            (virtual_model_name,),
        )
        result = cursor.fetchone()
        if result:
            in_cost = float(result[0]) if result[0] is not None else 0.0
            out_cost = (
                float(result[1]) if result[1] is not None else in_cost
            )  # default to in_cost if not set
            return in_cost, out_cost
        return 0.0, 0.0


def get_virtual_model_cost_cached(virtual_model_name):
    """Get cost_per_1m_tokens_cached for a virtual model (both in and out)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT cost_per_1m_tokens_in_cached, cost_per_1m_tokens_out_cached FROM virtual_models WHERE name = ?",
            (virtual_model_name,),
        )
        result = cursor.fetchone()
        if result:
            in_cost = float(result[0]) if result[0] is not None else 0.0
            out_cost = (
                float(result[1]) if result[1] is not None else in_cost
            )  # default to in_cost if not set
            return in_cost, out_cost
        return 0.0, 0.0


def log_chat_usage(
    virtual_model,
    endpoint_name,
    endpoint_id,
    usage,
    response_time_ms,
    cache_attempted: int = 0,
    cache_hit: int = 0,
    inbound_api_key_id: Any = None,
    inbound_api_key_label: str = "",
):
    """Log chat completion usage to request_usage table."""
    try:
        prompt_tokens = usage.get("prompt_tokens", 0) or 0
        completion_tokens = usage.get("completion_tokens", 0) or 0
        total_tokens = usage.get("total_tokens", 0) or (
            prompt_tokens + completion_tokens
        )

        # Extract cached token information
        # OpenAI/DeepInfra format: usage.prompt_tokens_details.cached_tokens
        # Anthropic format: usage.cache_read_input_tokens, usage.cache_creation_input_tokens
        cached_input_tokens = 0
        cache_creation_tokens = 0

        prompt_tokens_details = usage.get("prompt_tokens_details", {})
        if prompt_tokens_details:
            cached_input_tokens = prompt_tokens_details.get("cached_tokens", 0) or 0

        # Anthropic format
        if usage.get("cache_read_input_tokens"):
            cached_input_tokens = usage.get("cache_read_input_tokens", 0) or 0
        if usage.get("cache_creation_input_tokens"):
            cache_creation_tokens = usage.get("cache_creation_input_tokens", 0) or 0

        # Get pricing for regular and cached tokens
        cost_in, cost_out = get_virtual_model_cost(virtual_model)
        cost_in_cached, cost_out_cached = get_virtual_model_cost_cached(virtual_model)

        # Calculate regular cost (non-cached input tokens)
        non_cached_input = max(
            0, prompt_tokens - cached_input_tokens - cache_creation_tokens
        )
        cost_estimate = (non_cached_input / 1000000 * cost_in) + (
            completion_tokens / 1000000 * cost_out
        )

        # Calculate cached cost
        cached_cost = (cached_input_tokens / 1000000 * cost_in_cached) + (
            cache_creation_tokens / 1000000 * cost_in_cached
        )
        cached_cost_estimate = cached_cost

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO request_usage 
                (virtual_model, endpoint_name, endpoint_id, inbound_api_key_id, inbound_api_key_label, request_type, 
                 prompt_tokens, completion_tokens, total_tokens, 
                 cached_input_tokens, cache_creation_tokens,
                 cost_estimate, cost_in, cost_out, cached_cost_estimate, response_time_ms,
                  cache_attempted, cache_hit)
                VALUES (?, ?, ?, ?, ?, 'chat', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    virtual_model,
                    endpoint_name,
                    endpoint_id,
                    inbound_api_key_id,
                    (inbound_api_key_label or "")[:128],
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    cached_input_tokens,
                    cache_creation_tokens,
                    cost_estimate,
                    prompt_tokens / 1000000 * cost_in,
                    completion_tokens / 1000000 * cost_out,
                    cached_cost_estimate,
                    response_time_ms,
                    int(cache_attempted or 0),
                    int(cache_hit or 0),
                ),
            )
            conn.commit()
    except Exception as e:
        # Log error but don't fail request
        print(f"Error logging chat usage: {e}")


def _approx_token_count(text: str) -> int:
    """Rough token estimate when upstream usage is unavailable."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _append_usage_text(parts: list[str], value: Any) -> None:
    """Collect text-like payloads while skipping binary-heavy blocks."""
    if value is None:
        return
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return
        if stripped.startswith("data:image/"):
            parts.append("[image]")
            return
        if len(stripped) > 2000 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", stripped):
            parts.append("[binary]")
            return
        parts.append(stripped)
        return
    if isinstance(value, list):
        for item in value:
            _append_usage_text(parts, item)
        return
    if isinstance(value, dict):
        ctype = str(value.get("type") or "").lower()
        if ctype in ("text", "input_text", "output_text"):
            _append_usage_text(parts, value.get("text"))
            return
        if ctype in ("image_url", "input_image"):
            parts.append("[image]")
            return
        if "name" in value:
            _append_usage_text(parts, value.get("name"))
        if "arguments" in value:
            _append_usage_text(parts, value.get("arguments"))
        if "content" in value:
            _append_usage_text(parts, value.get("content"))
        return
    parts.append(str(value))


def _estimate_openai_oauth_usage(
    messages: list[dict[str, Any]],
    tools: Any,
    full_content: str,
    full_reasoning: str,
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    """Estimate usage for openai_oauth streams when upstream omits token counts."""
    prompt_parts: list[str] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip()
        if role:
            prompt_parts.append(role)
        _append_usage_text(prompt_parts, msg.get("content"))
        _append_usage_text(prompt_parts, msg.get("name"))
        _append_usage_text(prompt_parts, msg.get("tool_calls"))

    if tools:
        _append_usage_text(prompt_parts, tools)

    completion_parts: list[str] = []
    _append_usage_text(completion_parts, full_content)
    _append_usage_text(completion_parts, full_reasoning)
    _append_usage_text(completion_parts, tool_calls)

    prompt_tokens = _approx_token_count("\n".join(prompt_parts))
    completion_tokens = _approx_token_count("\n".join(completion_parts))
    total_tokens = prompt_tokens + completion_tokens

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "is_estimated": True,
        "estimate_source": "openai_oauth",
    }


def is_virtual_model_cache_enabled(model_name: str) -> bool:
    """Return True when cache is enabled for a virtual model."""
    vm = get_virtual_model(model_name)
    if not vm:
        return False
    return int(vm.get("cache_enabled") or 1) == 1


def _normalize_content_for_cache(content: Any) -> Any:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        normalized = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype in ("text", "input_text", "output_text"):
                normalized.append(
                    {"type": btype, "text": str(block.get("text") or "").strip()}
                )
            elif btype in ("image_url", "image", "input_image"):
                normalized.append({"type": "image"})
        return normalized
    return content


def normalize_request_for_cache(payload: dict[str, Any]) -> str:
    """Create deterministic cache key for non-streaming requests."""
    messages = payload.get("messages") or []
    tools = payload.get("tools") or []

    normalized_messages = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        normalized_messages.append(
            {
                "role": str(msg.get("role") or "user"),
                "content": _normalize_content_for_cache(msg.get("content")),
                "name": msg.get("name"),
                "tool_calls": msg.get("tool_calls"),
            }
        )

    normalized_tools = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        normalized_tools.append(
            {
                "type": str(tool.get("type") or "function"),
                "name": str(fn.get("name") or ""),
                "description": str(fn.get("description") or ""),
                "parameters": fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {},
            }
        )
    normalized_tools = sorted(normalized_tools, key=lambda t: t.get("name") or "")

    key_obj = {
        "model": payload.get("model"),
        "messages": normalized_messages,
        "tools": normalized_tools,
        "temperature": payload.get("temperature"),
        "top_p": payload.get("top_p"),
        "max_tokens": payload.get("max_tokens"),
        "stop": payload.get("stop"),
        "presence_penalty": payload.get("presence_penalty"),
        "frequency_penalty": payload.get("frequency_penalty"),
    }
    key_json = json.dumps(key_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(key_json.encode("utf-8")).hexdigest()


def get_cached_response(cache_key: str) -> Optional[dict[str, Any]]:
    """Fetch cached response by key if unexpired; increments hit count on hit."""
    now = int(time.time())
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT response_json FROM response_cache WHERE cache_key = ? AND expires_at > ?",
                (cache_key, now),
            )
            row = cursor.fetchone()
            if not row:
                return None
            cursor.execute(
                "UPDATE response_cache SET hit_count = hit_count + 1 WHERE cache_key = ?",
                (cache_key,),
            )
            conn.commit()
            return json.loads(row[0])
    except Exception:
        return None


def store_cached_response(cache_key: str, response: dict[str, Any], ttl: int, virtual_model: str) -> None:
    """Persist cache entry for non-streaming response."""
    now = int(time.time())
    expires_at = now + max(1, int(ttl or 1))
    cost_in, cost_out = get_virtual_model_cost(virtual_model)
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO response_cache
                (cache_key, response_json, created_at, expires_at, hit_count, model, cost_in, cost_out)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    json.dumps(response),
                    now,
                    expires_at,
                    0,
                    virtual_model,
                    float(cost_in or 0),
                    float(cost_out or 0),
                ),
            )
            conn.commit()
    except Exception as e:
        debug_log("warn", f"[CACHE] store failed key={cache_key[:12]} err={e}")


def log_completion_usage(
    virtual_model,
    endpoint_name,
    endpoint_id,
    usage,
    response_time_ms,
    inbound_api_key_id: Any = None,
    inbound_api_key_label: str = "",
):
    """Log text completion usage to request_usage table."""
    try:
        prompt_tokens = usage.get("prompt_tokens", 0) or 0
        completion_tokens = usage.get("completion_tokens", 0) or 0
        total_tokens = usage.get("total_tokens", 0) or (
            prompt_tokens + completion_tokens
        )

        # Extract cached token information
        cached_input_tokens = 0
        cache_creation_tokens = 0

        prompt_tokens_details = usage.get("prompt_tokens_details", {})
        if prompt_tokens_details:
            cached_input_tokens = prompt_tokens_details.get("cached_tokens", 0) or 0

        if usage.get("cache_read_input_tokens"):
            cached_input_tokens = usage.get("cache_read_input_tokens", 0) or 0
        if usage.get("cache_creation_input_tokens"):
            cache_creation_tokens = usage.get("cache_creation_input_tokens", 0) or 0

        # Get pricing for regular and cached tokens
        cost_in, cost_out = get_virtual_model_cost(virtual_model)
        cost_in_cached, cost_out_cached = get_virtual_model_cost_cached(virtual_model)

        # Calculate regular cost (non-cached input tokens)
        non_cached_input = max(
            0, prompt_tokens - cached_input_tokens - cache_creation_tokens
        )
        cost_estimate = (non_cached_input / 1000000 * cost_in) + (
            completion_tokens / 1000000 * cost_out
        )

        # Calculate cached cost
        cached_cost = (cached_input_tokens / 1000000 * cost_in_cached) + (
            cache_creation_tokens / 1000000 * cost_in_cached
        )
        cached_cost_estimate = cached_cost

        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO request_usage 
                    (virtual_model, endpoint_name, endpoint_id, inbound_api_key_id, inbound_api_key_label, request_type, 
                     prompt_tokens, completion_tokens, total_tokens, 
                     cached_input_tokens, cache_creation_tokens,
                     cost_estimate, cost_in, cost_out, cached_cost_estimate, response_time_ms)
                    VALUES (?, ?, ?, ?, ?, 'completion', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        virtual_model,
                        endpoint_name,
                        endpoint_id,
                        inbound_api_key_id,
                        (inbound_api_key_label or "")[:128],
                        prompt_tokens,
                        completion_tokens,
                        total_tokens,
                        cached_input_tokens,
                        cache_creation_tokens,
                        cost_estimate,
                        prompt_tokens / 1000000 * cost_in,
                        completion_tokens / 1000000 * cost_out,
                        cached_cost_estimate,
                        response_time_ms,
                    ),
                )
                conn.commit()
        except Exception as e:
            print(f"Error logging completion usage: {e}")
    except Exception as e:
        print(f"Error logging completion usage: {e}")


def log_embedding_usage(
    virtual_model,
    endpoint_name,
    endpoint_id,
    input_tokens,
    output_tokens,
    response_time_ms,
    inbound_api_key_id: Any = None,
    inbound_api_key_label: str = "",
):
    """Log embedding usage to embedding_usage table."""
    try:
        total_tokens = input_tokens + output_tokens

        cost_in, cost_out = get_virtual_model_cost(virtual_model)
        cost_estimate = (input_tokens / 1000000 * cost_in) + (
            output_tokens / 1000000 * cost_out
        )

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO embedding_usage 
                (virtual_model, endpoint_name, endpoint_id, inbound_api_key_id, inbound_api_key_label,
                 input_tokens, output_tokens, total_tokens, 
                 cost_estimate, cost_in, cost_out, response_time_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    virtual_model,
                    endpoint_name,
                    endpoint_id,
                    inbound_api_key_id,
                    (inbound_api_key_label or "")[:128],
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    cost_estimate,
                    input_tokens / 1000000 * cost_in,
                    output_tokens / 1000000 * cost_out,
                    response_time_ms,
                ),
            )
            conn.commit()
    except Exception as e:
        print(f"Error logging embedding usage: {e}")


ACTIVITY_MAX_ROWS = int(os.getenv("ACTIVITY_MAX_ROWS", "10000"))
ACTIVITY_MAX_AGE_DAYS = int(os.getenv("ACTIVITY_MAX_AGE_DAYS", "30"))


def _resolve_activity_client_ip(request: Request) -> tuple[str, str]:
    client_ip, forwarded_for, _ = resolve_effective_client_ip(request)
    return client_ip, forwarded_for


def _normalize_activity_request_type(path: str) -> str:
    p = (path or "").lower()
    if "chat/completions" in p or p == "/api/chat":
        return "chat"
    if "embeddings" in p or p == "/api/embed":
        return "embeddings"
    if "completions" in p:
        return "completions"
    if p.endswith("/models") or "/models" in p:
        return "models"
    if p.startswith("/api/admin"):
        return "admin"
    return "other"


def _normalize_activity_outcome(status_code: int) -> str:
    if 200 <= int(status_code or 0) < 400:
        return "success"
    if status_code in (401, 403):
        return "auth_error"
    if 400 <= int(status_code or 0) < 500:
        return "client_error"
    if status_code in (502, 503, 504):
        return "upstream_error"
    if int(status_code or 0) >= 500:
        return "server_error"
    return "other"


def _sanitize_activity_error_summary(msg: Any, max_len: int = 220) -> str:
    if msg is None:
        return ""
    txt = str(msg).replace("\n", " ").replace("\r", " ").strip()
    if len(txt) <= max_len:
        return txt
    return txt[: max_len - 1].rstrip() + "…"


def _resolve_endpoint_name(endpoint_id: Any) -> str:
    try:
        eid = int(endpoint_id or 0)
    except Exception:
        eid = 0
    if eid <= 0:
        return ""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM endpoints WHERE id = ?", (eid,))
            row = cursor.fetchone()
            return str(row[0] or "") if row else ""
    except Exception:
        return ""


def log_recent_activity(
    *,
    request_id: str,
    method: str,
    path: str,
    request_type: str,
    virtual_model: str,
    actual_model: str,
    endpoint_name: str,
    endpoint_id: Any,
    endpoint_type: str,
    inbound_api_key_id: Any = None,
    inbound_api_key_label: str = "",
    client_ip: str,
    forwarded_for: str,
    x_source: str,
    user_agent: str,
    stream: bool,
    status_code: int,
    response_time_ms: int,
    error_summary: str = "",
) -> None:
    try:
        try:
            eid = int(endpoint_id or 0) if endpoint_id is not None else None
            if eid == 0:
                eid = None
        except Exception:
            eid = None

        endpoint_name_value = (endpoint_name or "").strip()
        vm_name = str(virtual_model or "").strip()

        if (not endpoint_name_value or not eid) and vm_name:
            try:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        SELECT e.id, e.name
                        FROM virtual_models vm
                        JOIN endpoints e ON vm.endpoint_id = e.id
                        WHERE vm.name = ?
                        LIMIT 1
                        """,
                        (vm_name,),
                    )
                    route_row = cursor.fetchone()
                if route_row:
                    if not eid:
                        try:
                            eid = int(route_row[0]) if route_row[0] is not None else None
                        except Exception:
                            eid = eid
                    if not endpoint_name_value:
                        endpoint_name_value = str(route_row[1] or "")
            except Exception:
                pass

        if not endpoint_name_value and eid:
            endpoint_name_value = _resolve_endpoint_name(eid)

        summary = _sanitize_activity_error_summary(error_summary)
        status = int(status_code or 0)
        outcome = _normalize_activity_outcome(status)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO recent_activity (
                    created_at, request_id, method, path, request_type,
                    virtual_model, actual_model, endpoint_name, endpoint_id, endpoint_type,
                    inbound_api_key_id, inbound_api_key_label,
                    client_ip, forwarded_for, x_source, user_agent, stream,
                    status_code, outcome, response_time_ms, error_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(time.time()),
                    (request_id or "")[:64],
                    (method or "GET")[:10],
                    (path or "")[:255],
                    (request_type or "other")[:32],
                    (virtual_model or "")[:128],
                    (actual_model or "")[:128],
                    endpoint_name_value[:128],
                    eid,
                    (endpoint_type or "")[:64],
                    inbound_api_key_id,
                    (inbound_api_key_label or "")[:128],
                    (client_ip or "-")[:128],
                    (forwarded_for or "")[:255],
                    (x_source or "")[:128],
                    (user_agent or "")[:180],
                    1 if stream else 0,
                    status,
                    outcome,
                    int(response_time_ms or 0),
                    summary,
                ),
            )

            max_age_cutoff = int(time.time()) - max(1, ACTIVITY_MAX_AGE_DAYS) * 86400
            cursor.execute(
                "DELETE FROM recent_activity WHERE created_at < ?", (max_age_cutoff,)
            )
            cursor.execute(
                """
                DELETE FROM recent_activity
                WHERE id NOT IN (
                    SELECT id FROM recent_activity ORDER BY id DESC LIMIT ?
                )
                """,
                (max(1000, ACTIVITY_MAX_ROWS),),
            )
            conn.commit()
    except Exception as exc:
        debug_log("warn", f"[ACTIVITY] failed to log activity: {exc}")


def _build_activity_query(
    *,
    limit: int,
    status_filter: str,
    model_filter: str,
    path_filter: str,
    ip_filter: str,
    inbound_api_key_id: Optional[int],
    since: int,
    include_health: bool,
) -> tuple[str, list[Any]]:
    sql = """
        SELECT id, created_at, request_id, method, path, request_type,
               virtual_model, actual_model, endpoint_name, endpoint_id, endpoint_type,
               inbound_api_key_id, inbound_api_key_label,
               client_ip, forwarded_for, x_source, user_agent, stream,
               status_code, outcome, response_time_ms, error_summary
        FROM recent_activity
        WHERE 1=1
    """
    params: list[Any] = []
    if since > 0:
        sql += " AND created_at >= ?"
        params.append(since)
    if not include_health:
        sql += " AND path != '/health'"
    if status_filter:
        sql += " AND outcome = ?"
        params.append(status_filter)
    if model_filter:
        sql += " AND virtual_model LIKE ?"
        params.append(f"%{model_filter}%")
    if path_filter:
        sql += " AND path LIKE ?"
        params.append(f"%{path_filter}%")
    if ip_filter:
        sql += " AND client_ip LIKE ?"
        params.append(f"%{ip_filter}%")
    if inbound_api_key_id is not None:
        sql += " AND inbound_api_key_id = ?"
        params.append(inbound_api_key_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(500, limit)))
    return sql, params


KNOWN_TOOL_NAMES = frozenset(
    [
        "glob",
        "read",
        "write",
        "edit",
        "bash",
        "grep",
        "web_search",
        "webfetch",
        "web_search",
        "visit",
        "task",
        "submit",
        "TodoWrite",
        "TodoRead",
        "grep",
        "read",
        "write",
        "edit",
        "delete",
        "rename",
        "mkdir",
        "glob",
        "bash",
        "run",
        "question",
        "codesearch",
        "websearch",
        "webfetch",
        "read_file",
        "write_file",
        "execute_command",
        "task_agent",
        "explore",
        "search",
        "fetch",
    ]
)

app = FastAPI()

# Add CORS middleware for browser-based tools (OA-7)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_health_runner():
    """Initialize health cache and start optional health polling runner."""
    global health_runner_task
    load_health_cache()
    if health_runner_task is None or health_runner_task.done():
        health_runner_task = asyncio.create_task(health_check_runner())


@app.on_event("shutdown")
async def shutdown_health_runner():
    """Stop health polling runner on shutdown."""
    global health_runner_task
    if health_runner_task is not None and not health_runner_task.done():
        health_runner_task.cancel()
        try:
            await health_runner_task
        except Exception:
            pass
    health_runner_task = None


@app.middleware("http")
async def diagnostics_request_logger(request: Request, call_next):
    """Optional ingress/egress HTTP logging controlled by debug_mode."""
    mode = is_debug_mode()
    log_enabled = mode in ("basic", "full")

    start = time.time()
    if log_enabled:
        request_id = request.headers.get("x-request-id", "-")
        ua = request.headers.get("user-agent", "")[:80]
        print(
            f"[HTTP_IN] method={request.method} path={request.url.path} query={request.url.query or '-'} "
            f"rid={request_id} ua={ua}",
            flush=True,
        )

    try:
        response = await call_next(request)
    except Exception as e:
        if log_enabled:
            elapsed_ms = int((time.time() - start) * 1000)
            print(
                f"[HTTP_OUT] method={request.method} path={request.url.path} status=500 elapsed_ms={elapsed_ms} err={str(e)[:160]}",
                flush=True,
            )
        raise

    response.headers["X-Proxy"] = "serverless-proxy"

    if log_enabled:
        elapsed_ms = int((time.time() - start) * 1000)
        print(
            f"[HTTP_OUT] method={request.method} path={request.url.path} status={response.status_code} elapsed_ms={elapsed_ms}",
            flush=True,
        )

    return response


@app.exception_handler(StarletteHTTPException)
async def diagnostics_http_exception_handler(
    request: Request, exc: StarletteHTTPException
):
    """Log explicit 404s/HTTP errors when diagnostics are enabled."""
    mode = is_debug_mode()
    if mode in ("basic", "full"):
        print(
            f"[HTTP_ERR] method={request.method} path={request.url.path} status={exc.status_code} detail={str(exc.detail)[:160]}",
            flush=True,
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# ============================================================================
# Backend Abstraction Layer
# ============================================================================


class LLMBackend(ABC):
    """Abstract base class for LLM backends."""

    @abstractmethod
    async def chat_completion(
        self,
        messages: list,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 256,
        top_p: float = 1.0,
        stream: bool = False,
        tools: Optional[list] = None,
        **kwargs,
    ) -> tuple[Optional[dict], Optional[dict], int]:
        """
        Execute a chat completion request.
        Returns: (result, error, status_code)
        - result: Response dict on success
        - error: Error dict on failure
        - status_code: HTTP status code
        """
        pass

    @abstractmethod
    async def embeddings(
        self,
        input_text: str,
        model: str,
    ) -> tuple[Optional[dict], Optional[dict], int]:
        """
        Execute an embeddings request.
        Returns: (result, error, status_code)
        - result: Response dict on success
        - error: Error dict on failure
        - status_code: HTTP status code
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if backend is healthy."""
        pass


class AIQueueBackend(LLMBackend):
    """AI Queue Master backend."""

    def __init__(self):
        self.url = os.getenv("AI_QUEUE_URL", "http://host.docker.internal:8102")
        self.api_key = os.getenv("AI_QUEUE_API_KEY", "")
        self.priority = os.getenv("AI_QUEUE_PRIORITY", "NORMAL")
        self.source = os.getenv("AI_QUEUE_SOURCE", "runpod-proxy")

    async def chat_completion(
        self,
        messages: list,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 256,
        top_p: float = 1.0,
        stream: bool = False,
        tools: Optional[list] = None,
        **kwargs,
    ) -> tuple[Optional[dict], Optional[dict], int]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Source": self.source,
            "X-Priority": self.priority,
            "X-Model": model,
        }

        # Add user ID if provided
        user = kwargs.get("user")
        if user:
            headers["X-User-ID"] = user

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
        }

        # Add optional parameters
        optional_params = [
            "stop",
            "presence_penalty",
            "frequency_penalty",
            "logit_bias",
            "tool_choice",
            "response_format",
            "seed",
            "parallel_tool_calls",
        ]
        for param in optional_params:
            if kwargs.get(param) is not None:
                payload[param] = kwargs[param]

        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=1200.0) as client:
            print(
                f"[AIQ_CALL] Model: {model}, Messages: {len(messages)}, Tools: {len(tools) if tools else 0}"
            )
            print(
                f"[AIQ_CALL] Payload: model={payload.get('model')}, stream={payload.get('stream')}"
            )
            response = await client.post(
                f"{self.url}/v1/chat/completions",
                headers=headers,
                json=payload,
            )

            if response.status_code != 200:
                try:
                    error_data = response.json()
                    if "error" in error_data:
                        return (
                            None,
                            {"error": error_data["error"]},
                            response.status_code,
                        )
                except Exception:
                    pass
                return (
                    None,
                    {
                        "error": {
                            "message": f"AI Queue error: {response.text}",
                            "type": "internal_server_error",
                            "code": "queue_error",
                        }
                    },
                    response.status_code,
                )

            result = response.json()

            # Debug: log response details
            choices = result.get("choices", [])
            if choices:
                first_choice = choices[0]
                msg = first_choice.get("message", {})
                content = msg.get("content", "")
                tc = msg.get("tool_calls", [])
                finish = first_choice.get("finish_reason")
                print(
                    f"[AIQ_RESP] Content length: {len(content) if content else 0}, Tool calls: {len(tc)}, Finish: {finish}"
                )
            else:
                print(f"[AIQ_RESP] No choices in response: {result.keys()}")

            if result.get("error"):
                error_info = result.get("error")
                if isinstance(error_info, str):
                    error_info = {
                        "message": error_info,
                        "type": "internal_server_error",
                    }
                return None, {"error": error_info}, 500

            return result, None, 200

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.url}/health")
                return response.status_code == 200
        except Exception:
            return False

    async def embeddings(
        self,
        input_text: str,
        model: str,
    ) -> tuple[Optional[dict], Optional[dict], int]:
        """Route embeddings request to AI Queue."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Source": self.source,
        }

        payload = {
            "model": model,
            "input": input_text,
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                response = await client.post(
                    f"{self.url}/v1/embeddings",
                    headers=headers,
                    json=payload,
                )

                if response.status_code < 200 or response.status_code >= 300:
                    if self.endpoint_type == "ollama":
                        try:
                            payload_preview = json.dumps(payload)[:2000]
                        except Exception:
                            payload_preview = str(payload)[:2000]
                        debug_log(
                            "warn",
                            f"[OLLAMA_400] request_id={request_id} status={response.status_code} body={response.text[:400]} payload={payload_preview}",
                        )
                    return (
                        None,
                        {
                            "error": {
                                "message": f"AI Queue error: {response.text}",
                                "type": "internal_server_error",
                            }
                        },
                        response.status_code,
                    )

                result = response.json()
                return result, None, 200
            except Exception as e:
                return (
                    None,
                    {"error": {"message": str(e), "type": "internal_server_error"}},
                    500,
                )


class RunPodBackend(LLMBackend):
    """RunPod Serverless backend."""

    def __init__(self):
        self.api_key = os.getenv("RUNPOD_API_KEY", "")
        self.endpoint_id = os.getenv("RUNPOD_ENDPOINT_ID", "")
        self.endpoint_type = os.getenv("ENDPOINT_TYPE", "ollama").lower()

    def _build_payload(
        self,
        messages: list,
        temperature: float,
        max_tokens: int,
        top_p: float,
        tools: Optional[list] = None,
        **kwargs,
    ) -> dict:
        """Build backend-specific payload."""
        if self.endpoint_type == "ollama":
            return self._build_ollama_payload(
                messages, temperature, max_tokens, top_p, tools, **kwargs
            )
        else:
            return self._build_vllm_payload(
                messages, temperature, max_tokens, top_p, tools, **kwargs
            )

    def _build_ollama_payload(
        self, messages, temperature, max_tokens, top_p, tools=None, **kwargs
    ):
        """Build Ollama format payload."""
        system_parts = []

        if tools:
            tool_desc = "You have access to the following tools:\n\n"
            for tool in tools:
                func = tool.get("function", {})
                name = func.get("name", "unknown")
                desc = func.get("description", "")
                params = func.get("parameters", {})
                props = params.get("properties", {})
                param_str = ", ".join(props.keys()) if props else "none"
                tool_desc += f"- {name}({param_str}): {desc}\n"
            tool_desc += "\nWhen you need to use a tool, respond with ONLY the tool call in this format:\n"
            tool_desc += '```tool_call\n{"name": "tool_name", "arguments": {"arg1": "value1"}}\n```\n'
            system_parts.append(tool_desc)

        prompt_parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                prompt_parts.append(f"System: {content}")
            elif role == "user":
                prompt_parts.append(f"User: {content}")
            elif role == "assistant":
                prompt_parts.append(f"Assistant: {content}")

        if system_parts:
            prompt_parts = system_parts + prompt_parts

        prompt = "\n\n".join(prompt_parts) + "\n\nAssistant:"

        return {
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "top_p": top_p,
            },
        }

    def _build_vllm_payload(
        self, messages, temperature, max_tokens, top_p, tools=None, **kwargs
    ):
        """Build vLLM format payload."""
        payload = {
            "messages": messages,
            "sampling_params": {
                "temperature": temperature,
                "max_tokens": max_tokens,
                "top_p": top_p,
            },
            "use_openai_format": 1,
        }

        stop = kwargs.get("stop")
        presence_penalty = kwargs.get("presence_penalty")
        frequency_penalty = kwargs.get("frequency_penalty")

        if stop is not None:
            payload["sampling_params"]["stop"] = stop
        if presence_penalty is not None:
            payload["sampling_params"]["presence_penalty"] = presence_penalty
        if frequency_penalty is not None:
            payload["sampling_params"]["frequency_penalty"] = frequency_penalty

        if tools:
            payload["tools"] = tools

        return payload

    async def chat_completion(
        self,
        messages: list,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 256,
        top_p: float = 1.0,
        stream: bool = False,
        tools: Optional[list] = None,
        **kwargs,
    ) -> tuple[Optional[dict], Optional[dict], int]:
        input_data = self._build_payload(
            messages, temperature, max_tokens, top_p, tools, **kwargs
        )

        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                f"https://api.runpod.ai/v2/{self.endpoint_id}/runsync",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"input": input_data},
            )

            if response.status_code != 200:
                return (
                    None,
                    {
                        "error": {
                            "message": f"RunPod error: {response.text}",
                            "type": "internal_server_error",
                            "code": "runpod_error",
                        }
                    },
                    500,
                )

            result = response.json()
            job_id = result.get("id", f"chat-{int(time.time())}")

            if result.get("status") != "COMPLETED":
                result = await self._wait_for_completion(client, job_id)
                if result.get("status") == "TIMEOUT":
                    return (
                        None,
                        {
                            "error": {
                                "message": "Request timed out",
                                "type": "timeout_error",
                                "code": "request_timeout",
                            }
                        },
                        408,
                    )
                if result.get("status") in ["FAILED", "CANCELLED"]:
                    return (
                        None,
                        {
                            "error": {
                                "message": f"Job {result.get('status', 'unknown').lower()}",
                                "type": "internal_server_error",
                                "code": "job_failed",
                            }
                        },
                        500,
                    )

            return {"job_id": job_id, "result": result}, None, 200

    async def _wait_for_completion(self, client, job_id, max_wait=300):
        start = time.time()
        while time.time() - start < max_wait:
            await asyncio.sleep(2)
            status_resp = await client.get(
                f"https://api.runpod.ai/v2/{self.endpoint_id}/status/{job_id}",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            if status_resp.status_code == 200:
                data = status_resp.json()
                if data.get("status") == "COMPLETED":
                    return data
                elif data.get("status") in ["FAILED", "CANCELLED"]:
                    return data
        return {"status": "TIMEOUT"}

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"https://api.runpod.ai/v2/{self.endpoint_id}/health",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                return response.status_code == 200
        except Exception:
            return False

    async def embeddings(
        self,
        input_text: str,
        model: str,
    ) -> tuple[Optional[dict], Optional[dict], int]:
        """Route embeddings request to RunPod."""
        # RunPod doesn't have a standard embeddings API via /runsync
        # This would need a separate endpoint or worker
        return (
            None,
            {
                "error": {
                    "message": "Embeddings not supported in direct RunPod mode. Use AI Queue mode.",
                    "type": "invalid_request_error",
                    "code": "not_implemented",
                }
            },
            501,
        )


# ============================================================================
# Backend Factory
# ============================================================================


def get_virtual_model(model_name: str) -> Optional[dict]:
    """Look up virtual model in database."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT vm.*, e.url as endpoint_url, e.api_key as endpoint_api_key, 
                       e.endpoint_type as endpoint_type,
                       e.oauth_enabled as oauth_enabled,
                       e.oauth_grant_type as oauth_grant_type,
                       e.oauth_token_url as oauth_token_url,
                       e.oauth_client_id as oauth_client_id,
                       e.oauth_client_secret as oauth_client_secret,
                       e.oauth_scope as oauth_scope,
                       e.oauth_refresh_token as oauth_refresh_token,
                       e.oauth_token_expires_at as oauth_token_expires_at,
                       e.oauth_token_request_format as oauth_token_request_format,
                       e.oauth_client_auth_method as oauth_client_auth_method
                FROM virtual_models vm
                JOIN endpoints e ON vm.endpoint_id = e.id
                WHERE vm.name = ? AND vm.enabled = 1 AND e.enabled = 1
            """,
                (model_name,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception:
        return None


def get_enabled_virtual_models() -> list[dict]:
    """Return enabled virtual model records joined with endpoint metadata."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT vm.name, vm.actual_model, e.name as endpoint_name, e.endpoint_type as endpoint_type
                FROM virtual_models vm
                JOIN endpoints e ON vm.endpoint_id = e.id
                WHERE vm.enabled = 1 AND e.enabled = 1
            """
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception:
        return []


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _oauth_defaults_for_endpoint_type(endpoint_type: str) -> dict[str, Any]:
    et = (endpoint_type or "").lower()
    if et == "openai_oauth":
        return {
            "url": "https://chatgpt.com",
            "oauth_enabled": 1,
            "oauth_grant_type": "refresh_token",
            "oauth_token_url": "https://auth.openai.com/oauth/token",
            "oauth_token_request_format": "json",
            "oauth_client_auth_method": "client_secret_post",
        }
    return {}


def _apply_endpoint_defaults(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data or {})
    endpoint_type = (out.get("endpoint_type") or "openai").lower()
    defaults = _oauth_defaults_for_endpoint_type(endpoint_type)
    for key, value in defaults.items():
        current = out.get(key)
        if current is None or current == "":
            out[key] = value
    if out.get("health_check_url") is None:
        out["health_check_url"] = ""
    return out


def _preserve_endpoint_oauth_secrets_on_update(
    endpoint_id: int, data: dict[str, Any]
) -> dict[str, Any]:
    """Keep stored OAuth secrets when update payload leaves them blank.

    This prevents accidental secret/token erasure when editing endpoints from UI forms
    that intentionally do not prefill sensitive fields.
    """
    out = dict(data or {})
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT oauth_client_secret, oauth_refresh_token FROM endpoints WHERE id = ?",
                (endpoint_id,),
            )
            row = cursor.fetchone()
    except Exception:
        row = None

    if not row:
        return out

    existing_client_secret = str(row[0] or "")
    existing_refresh_token = str(row[1] or "")

    incoming_client_secret = out.get("oauth_client_secret")
    if incoming_client_secret is None or str(incoming_client_secret).strip() == "":
        out["oauth_client_secret"] = existing_client_secret

    incoming_refresh_token = out.get("oauth_refresh_token")
    if incoming_refresh_token is None or str(incoming_refresh_token).strip() == "":
        out["oauth_refresh_token"] = existing_refresh_token

    return out


_oauth_token_cache: dict[int, tuple[str, int]] = {}
_oauth_refresh_locks: dict[int, asyncio.Lock] = {}


def _oauth_endpoint_is_configured(endpoint: dict[str, Any]) -> bool:
    if not _coerce_bool(endpoint.get("oauth_enabled", 0)):
        return False
    grant_type = (endpoint.get("oauth_grant_type") or "").strip()
    token_url = (endpoint.get("oauth_token_url") or "").strip()
    if not grant_type or not token_url:
        return False
    if grant_type == "refresh_token":
        return bool((endpoint.get("oauth_refresh_token") or "").strip())
    if grant_type == "client_credentials":
        return bool((endpoint.get("oauth_client_secret") or "").strip())
    return False


def _oauth_build_token_request(endpoint: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any], Optional[tuple[str, str]], bool]:
    grant_type = (endpoint.get("oauth_grant_type") or "refresh_token").strip()
    client_id = (endpoint.get("oauth_client_id") or "").strip()
    client_secret = (endpoint.get("oauth_client_secret") or "").strip()
    refresh_token = (endpoint.get("oauth_refresh_token") or "").strip()
    scope = (endpoint.get("oauth_scope") or "").strip()
    request_format = (endpoint.get("oauth_token_request_format") or "json").strip().lower()
    client_auth_method = (
        (endpoint.get("oauth_client_auth_method") or "client_secret_post").strip().lower()
    )

    payload: dict[str, Any] = {"grant_type": grant_type}
    auth: Optional[tuple[str, str]] = None

    if grant_type == "refresh_token":
        payload["refresh_token"] = refresh_token
        if client_id:
            payload["client_id"] = client_id
    elif grant_type == "client_credentials":
        if client_id:
            payload["client_id"] = client_id
    if scope:
        payload["scope"] = scope

    if client_auth_method == "client_secret_basic" and client_id and client_secret:
        auth = (client_id, client_secret)
        payload.pop("client_id", None)
    elif client_secret:
        payload["client_secret"] = client_secret

    use_form = request_format == "form"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
        if use_form
        else "application/json"
    }
    return headers, payload, auth, use_form


def _persist_oauth_rotation(endpoint_id: int, refresh_token: str, expires_at: int) -> None:
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            if refresh_token:
                cursor.execute(
                    """
                    UPDATE endpoints
                    SET oauth_refresh_token = ?, oauth_token_expires_at = ?, updated_at = strftime('%s', 'now')
                    WHERE id = ?
                    """,
                    (refresh_token, expires_at, endpoint_id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE endpoints
                    SET oauth_token_expires_at = ?, updated_at = strftime('%s', 'now')
                    WHERE id = ?
                    """,
                    (expires_at, endpoint_id),
                )
            conn.commit()
    except Exception as exc:
        debug_log("warn", f"[OAUTH] failed to persist token metadata endpoint_id={endpoint_id}: {exc}")


async def resolve_oauth_access_token_async(endpoint: dict[str, Any]) -> Optional[str]:
    if not _oauth_endpoint_is_configured(endpoint):
        return None
    endpoint_id = int(endpoint.get("id") or 0)
    if endpoint_id <= 0:
        return None

    now = int(time.time())
    cached = _oauth_token_cache.get(endpoint_id)
    if cached and cached[1] > now + 60:
        return cached[0]

    lock = _oauth_refresh_locks.setdefault(endpoint_id, asyncio.Lock())
    async with lock:
        cached = _oauth_token_cache.get(endpoint_id)
        now = int(time.time())
        if cached and cached[1] > now + 60:
            return cached[0]

        token_url = (endpoint.get("oauth_token_url") or "").strip()
        headers, payload, auth, use_form = _oauth_build_token_request(endpoint)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                req_kwargs: dict[str, Any] = {"headers": headers}
                if auth:
                    req_kwargs["auth"] = auth
                if use_form:
                    req_kwargs["data"] = payload
                else:
                    req_kwargs["json"] = payload
                resp = await client.post(token_url, **req_kwargs)
        except Exception as exc:
            debug_log("warn", f"[OAUTH] token fetch failed endpoint_id={endpoint_id}: {exc}")
            return None

        if resp.status_code >= 400:
            err_code = ""
            err_msg = ""
            try:
                err_obj = resp.json()
                if isinstance(err_obj, dict):
                    err = err_obj.get("error")
                    if isinstance(err, dict):
                        err_code = str(err.get("code") or "").strip()
                        err_msg = str(err.get("message") or "").strip()
                    elif isinstance(err, str):
                        err_msg = err.strip()
                    if not err_code:
                        err_code = str(err_obj.get("code") or "").strip()
                    if not err_msg:
                        err_msg = str(err_obj.get("message") or "").strip()
            except Exception:
                pass
            suffix = (
                f" code={err_code} msg={err_msg[:180]}" if (err_code or err_msg) else ""
            )
            debug_log(
                "warn",
                f"[OAUTH] token fetch rejected endpoint_id={endpoint_id} status={resp.status_code}{suffix}",
            )
            return None

        try:
            token_data = resp.json()
        except Exception:
            debug_log("warn", f"[OAUTH] token response invalid JSON endpoint_id={endpoint_id}")
            return None

        access_token = str(token_data.get("access_token") or "").strip()
        if not access_token:
            return None

        try:
            expires_in = int(token_data.get("expires_in") or 3600)
        except Exception:
            expires_in = 3600
        expires_at = int(time.time()) + max(expires_in, 60)

        rotated_refresh_token = str(token_data.get("refresh_token") or "").strip()
        if rotated_refresh_token:
            endpoint["oauth_refresh_token"] = rotated_refresh_token

        _persist_oauth_rotation(endpoint_id, rotated_refresh_token, expires_at)
        _oauth_token_cache[endpoint_id] = (access_token, expires_at)
        return access_token


def resolve_oauth_access_token_sync(endpoint: dict[str, Any]) -> Optional[str]:
    if not _oauth_endpoint_is_configured(endpoint):
        return None
    endpoint_id = int(endpoint.get("id") or 0)
    if endpoint_id <= 0:
        return None

    now = int(time.time())
    cached = _oauth_token_cache.get(endpoint_id)
    if cached and cached[1] > now + 60:
        return cached[0]

    token_url = (endpoint.get("oauth_token_url") or "").strip()
    headers, payload, auth, use_form = _oauth_build_token_request(endpoint)
    try:
        req_kwargs: dict[str, Any] = {"headers": headers, "timeout": 15}
        if auth:
            req_kwargs["auth"] = auth
        if use_form:
            req_kwargs["data"] = payload
        else:
            req_kwargs["json"] = payload
        resp = httpx.post(token_url, **req_kwargs)
    except Exception as exc:
        debug_log("warn", f"[OAUTH] token fetch failed endpoint_id={endpoint_id}: {exc}")
        return None

    if resp.status_code >= 400:
        err_code = ""
        err_msg = ""
        try:
            err_obj = resp.json()
            if isinstance(err_obj, dict):
                err = err_obj.get("error")
                if isinstance(err, dict):
                    err_code = str(err.get("code") or "").strip()
                    err_msg = str(err.get("message") or "").strip()
                elif isinstance(err, str):
                    err_msg = err.strip()
                if not err_code:
                    err_code = str(err_obj.get("code") or "").strip()
                if not err_msg:
                    err_msg = str(err_obj.get("message") or "").strip()
        except Exception:
            pass
        suffix = f" code={err_code} msg={err_msg[:180]}" if (err_code or err_msg) else ""
        debug_log(
            "warn",
            f"[OAUTH] token fetch rejected endpoint_id={endpoint_id} status={resp.status_code}{suffix}",
        )
        return None

    try:
        token_data = resp.json()
    except Exception:
        debug_log("warn", f"[OAUTH] token response invalid JSON endpoint_id={endpoint_id}")
        return None

    access_token = str(token_data.get("access_token") or "").strip()
    if not access_token:
        return None

    try:
        expires_in = int(token_data.get("expires_in") or 3600)
    except Exception:
        expires_in = 3600
    expires_at = int(time.time()) + max(expires_in, 60)

    rotated_refresh_token = str(token_data.get("refresh_token") or "").strip()
    if rotated_refresh_token:
        endpoint["oauth_refresh_token"] = rotated_refresh_token

    _persist_oauth_rotation(endpoint_id, rotated_refresh_token, expires_at)
    _oauth_token_cache[endpoint_id] = (access_token, expires_at)
    return access_token


async def resolve_endpoint_auth_header_async(endpoint: dict[str, Any]) -> Optional[str]:
    oauth_token = await resolve_oauth_access_token_async(endpoint)
    if oauth_token:
        return f"Bearer {oauth_token}"
    api_key = (endpoint.get("api_key") or endpoint.get("endpoint_api_key") or "").strip()
    if api_key:
        return f"Bearer {api_key}"
    return None


def resolve_endpoint_auth_header_sync(endpoint: dict[str, Any]) -> Optional[str]:
    oauth_token = resolve_oauth_access_token_sync(endpoint)
    if oauth_token:
        return f"Bearer {oauth_token}"
    api_key = (endpoint.get("api_key") or endpoint.get("endpoint_api_key") or "").strip()
    if api_key:
        return f"Bearer {api_key}"
    return None


def _deep_find_first_string(obj: Any, key_names: set[str]) -> Optional[str]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in key_names and isinstance(value, str) and value.strip():
                return value.strip()
        for value in obj.values():
            found = _deep_find_first_string(value, key_names)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _deep_find_first_string(item, key_names)
            if found:
                return found
    return None


def _deep_find_scope(obj: Any) -> Optional[str]:
    scope = _deep_find_first_string(obj, {"scope", "oauth_scope"})
    if scope:
        return scope
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in {"scopes", "oauth_scopes"}:
                if isinstance(value, list):
                    vals = [str(v).strip() for v in value if str(v).strip()]
                    if vals:
                        return " ".join(vals)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            nested = _deep_find_scope(value)
            if nested:
                return nested
    elif isinstance(obj, list):
        for item in obj:
            nested = _deep_find_scope(item)
            if nested:
                return nested
    return None


def _extract_oauth_fields_from_auth_json(data: Any) -> dict[str, str]:
    refresh_token = _deep_find_first_string(
        data,
        {
            "refresh_token",
            "oauth_refresh_token",
            "refreshToken",
            "oauthRefreshToken",
        },
    )
    client_id = _deep_find_first_string(
        data,
        {"client_id", "oauth_client_id", "clientId", "oauthClientId"},
    )
    client_secret = _deep_find_first_string(
        data,
        {
            "client_secret",
            "oauth_client_secret",
            "clientSecret",
            "oauthClientSecret",
        },
    )
    token_url = _deep_find_first_string(
        data,
        {
            "token_url",
            "oauth_token_url",
            "tokenUrl",
            "oauthTokenUrl",
        },
    )
    scope = _deep_find_scope(data)
    return {
        "oauth_refresh_token": refresh_token or "",
        "oauth_client_id": client_id or "",
        "oauth_client_secret": client_secret or "",
        "oauth_token_url": token_url or "",
        "oauth_scope": scope or "",
    }


def _candidate_codex_auth_paths(explicit_path: Optional[str] = None) -> list[str]:
    paths: list[str] = []
    if explicit_path and explicit_path.strip():
        paths.append(os.path.expanduser(explicit_path.strip()))
    for env_name in ("CHATGPT_LOCAL_HOME", "CODEX_HOME"):
        env_val = os.getenv(env_name, "").strip()
        if env_val:
            paths.append(os.path.join(os.path.expanduser(env_val), "auth.json"))
    paths.extend(
        [
            os.path.expanduser("~/.chatgpt-local/auth.json"),
            os.path.expanduser("~/.codex/auth.json"),
            "/root/.chatgpt-local/auth.json",
            "/root/.codex/auth.json",
        ]
    )
    deduped: list[str] = []
    seen = set()
    for p in paths:
        if p not in seen:
            deduped.append(p)
            seen.add(p)
    return deduped


OPENAI_WEB_OAUTH_DEFAULT_CLIENT_ID = os.getenv(
    "OPENAI_WEB_OAUTH_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann"
)
OPENAI_WEB_OAUTH_AUTHORIZE_URL = os.getenv(
    "OPENAI_WEB_OAUTH_AUTHORIZE_URL", "https://auth.openai.com/oauth/authorize"
)
OPENAI_WEB_OAUTH_TOKEN_URL = os.getenv(
    "OPENAI_WEB_OAUTH_TOKEN_URL", "https://auth.openai.com/oauth/token"
)
OPENAI_WEB_OAUTH_DEFAULT_REDIRECT_URI = os.getenv(
    "OPENAI_WEB_OAUTH_DEFAULT_REDIRECT_URI", "http://localhost:1455/auth/callback"
)
OPENAI_WEB_OAUTH_ORIGINATOR = os.getenv("OPENAI_WEB_OAUTH_ORIGINATOR", "pi")

# state -> pending session
_oauth_web_sessions: dict[str, dict[str, Any]] = {}
# state -> completed result
_oauth_web_results: dict[str, dict[str, Any]] = {}


def _base64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return _base64url_no_pad(digest)


def _cleanup_oauth_web_cache(now_ts: Optional[int] = None) -> None:
    now = now_ts or int(time.time())
    session_ttl = 900
    result_ttl = 1200
    expired_sessions = [
        state
        for state, session in _oauth_web_sessions.items()
        if int(session.get("created_at") or 0) < now - session_ttl
    ]
    for state in expired_sessions:
        _oauth_web_sessions.pop(state, None)

    expired_results = [
        state
        for state, result in _oauth_web_results.items()
        if int(result.get("created_at") or 0) < now - result_ttl
    ]
    for state in expired_results:
        _oauth_web_results.pop(state, None)


def _resolve_oauth_callback_base() -> str:
    explicit = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    host = flask_request.host
    scheme = (
        flask_request.headers.get("X-Forwarded-Proto", "")
        .split(",")[0]
        .strip()
        .lower()
    )
    if scheme not in ("http", "https"):
        scheme = flask_request.scheme or "http"
    return f"{scheme}://{host}"


def _extract_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                if item:
                    parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").lower()
            if item_type in ("text", "input_text", "output_text"):
                text_val = item.get("text")
                if isinstance(text_val, str) and text_val:
                    parts.append(text_val)
            elif item_type == "image_url":
                parts.append("[image]")
            elif item_type in ("input_image", "image"):
                parts.append("[image]")
        return "\n".join([p for p in parts if p])
    if content is None:
        return ""
    return str(content)


def _resolve_openai_oauth_response_endpoint(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return "https://chatgpt.com/backend-api/codex/responses"
    try:
        parsed = urllib.parse.urlparse(base)
        host = (parsed.netloc or "").lower()
        if host in ("api.openai.com", "www.api.openai.com"):
            return "https://chatgpt.com/backend-api/codex/responses"
    except Exception:
        pass
    if "/backend-api/codex/responses" in base:
        return base
    return f"{base}/backend-api/codex/responses"


def _openai_chat_to_openai_oauth_payload(
    messages: list,
    model: str,
    stream: bool,
    temperature: Optional[float],
    max_tokens: Optional[int],
    top_p: Optional[float],
    tools: Optional[list] = None,
    tool_choice: Any = None,
    parallel_tool_calls: Optional[bool] = None,
) -> dict[str, Any]:
    system_parts: list[str] = []
    input_items: list[dict[str, Any]] = []

    for msg in messages or []:
        role = str(msg.get("role") or "user").lower()
        text = _extract_text_content(msg.get("content"))
        if role == "system":
            if text:
                system_parts.append(text)
            continue

        # Convert prior assistant tool calls (OpenAI chat format) into
        # responses-style function_call items.
        if role == "assistant" and isinstance(msg.get("tool_calls"), list):
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                name = str(fn.get("name") or "").strip()
                if not name:
                    continue
                arguments = fn.get("arguments")
                if isinstance(arguments, (dict, list)):
                    arguments = json.dumps(arguments)
                elif arguments is None:
                    arguments = "{}"
                else:
                    arguments = str(arguments)
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": str(tc.get("id") or f"call_{int(time.time() * 1000)}"),
                        "name": name,
                        "arguments": arguments,
                    }
                )
            # Keep optional assistant text too, if present.
            if text:
                input_items.append({"role": "assistant", "content": text})
            continue

        # Convert tool result messages into responses-style function_call_output.
        if role == "tool":
            tool_call_id = str(msg.get("tool_call_id") or "").strip()
            if tool_call_id:
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": tool_call_id,
                        "output": text,
                    }
                )
                continue

        if role not in ("user", "assistant"):
            role = "user"
        input_items.append({"role": role, "content": text})

    if not input_items:
        input_items = [{"role": "user", "content": ""}]

    payload: dict[str, Any] = {
        "model": model,
        "store": False,
        "stream": bool(stream),
        "input": input_items,
    }
    if system_parts:
        payload["instructions"] = "\n\n".join(system_parts)
    else:
        # Some Responses-compatible OAuth backends reject requests unless
        # an instructions field is present, even when the caller provides
        # only user messages.
        payload["instructions"] = "You are a helpful assistant."
    # NOTE: Codex/ChatGPT OAuth backend may reject max_output_tokens.
    # Keep payload minimal/compatible by omitting token-limit fields here.
    # NOTE: Codex/ChatGPT OAuth backend may reject OpenAI-style sampling params.
    # Omit temperature/top_p to keep compatibility broad.

    # Tool mapping: OpenAI chat tools -> OAuth/Codex responses tools
    # Incoming shape (chat):
    #   {"type":"function","function":{"name":"...","description":"...","parameters":{...}}}
    # Expected responses shape:
    #   {"type":"function","name":"...","description":"...","parameters":{...}}
    mapped_tools: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        ttype = str(tool.get("type") or "function").strip().lower()
        if ttype != "function":
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        mapped: dict[str, Any] = {"type": "function", "name": name}
        desc = fn.get("description")
        if isinstance(desc, str) and desc.strip():
            mapped["description"] = desc
        params = fn.get("parameters")
        if isinstance(params, dict):
            mapped["parameters"] = params
        mapped_tools.append(mapped)

    if mapped_tools:
        payload["tools"] = mapped_tools

    # Tool choice mapping (OpenAI chat -> responses-style)
    # - "auto" / "none" map directly
    # - "required" is passed through to preserve strict semantics
    # - {"type":"function","function":{"name":"..."}} -> {"type":"function","name":"..."}
    if tool_choice is not None:
        mapped_choice = None
        if isinstance(tool_choice, str):
            choice = tool_choice.strip().lower()
            if choice in ("auto", "none", "required"):
                mapped_choice = choice
        elif isinstance(tool_choice, dict):
            ctype = str(tool_choice.get("type") or "").strip().lower()
            if ctype == "function":
                fn = tool_choice.get("function") or {}
                if isinstance(fn, dict):
                    fn_name = str(fn.get("name") or "").strip()
                    if fn_name:
                        mapped_choice = {"type": "function", "name": fn_name}
        if mapped_choice is not None:
            payload["tool_choice"] = mapped_choice

    if isinstance(parallel_tool_calls, bool):
        payload["parallel_tool_calls"] = parallel_tool_calls

    return payload


def _openai_oauth_response_to_chat_completion(
    response_obj: dict[str, Any], model_name: str
) -> dict[str, Any]:
    if "choices" in response_obj:
        return response_obj

    output_text = str(response_obj.get("output_text") or "")
    if not output_text:
        output_items = response_obj.get("output") or []
        if isinstance(output_items, list):
            collected: list[str] = []
            for item in output_items:
                if not isinstance(item, dict):
                    continue
                content_blocks = item.get("content") or []
                if not isinstance(content_blocks, list):
                    continue
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    block_type = str(block.get("type") or "").lower()
                    if block_type in ("output_text", "text", "input_text"):
                        txt = block.get("text")
                        if isinstance(txt, str) and txt:
                            collected.append(txt)
            output_text = "".join(collected)

    tool_calls: list[dict[str, Any]] = []
    output_items = response_obj.get("output") or []
    if isinstance(output_items, list):
        for item in output_items:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").lower()
            if item_type not in ("function_call", "tool_call"):
                continue
            name = str(item.get("name") or item.get("tool_name") or "").strip()
            if not name:
                continue
            arguments = item.get("arguments")
            if isinstance(arguments, (dict, list)):
                arguments = json.dumps(arguments)
            elif arguments is None:
                arguments = "{}"
            else:
                arguments = str(arguments)
            call_id = str(item.get("call_id") or item.get("id") or "").strip()
            if not call_id:
                call_id = f"call_{int(time.time() * 1000)}_{len(tool_calls)}"
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": arguments,
                    },
                }
            )

    usage = response_obj.get("usage") or {}
    prompt_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion_tokens = int(
        usage.get("output_tokens") or usage.get("completion_tokens") or 0
    )
    total_tokens = int(
        usage.get("total_tokens") or (prompt_tokens + completion_tokens)
    )

    finish_reason = "tool_calls" if tool_calls else "stop"
    message_obj: dict[str, Any] = {
        "role": "assistant",
        "content": output_text,
    }
    if tool_calls:
        message_obj["tool_calls"] = tool_calls

    return {
        "id": response_obj.get("id") or f"chatcmpl_{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": message_obj,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }


def _openai_oauth_sse_to_openai_chat_sse(sse_text: str, model_name: str) -> str:
    out_lines: list[str] = []
    saw_tool_call = False
    oauth_usage: Optional[dict[str, Any]] = None
    oauth_tool_stats: dict[str, int] = {
        "events": 0,
        "tool_added": 0,
        "tool_arg_delta": 0,
        "tool_done": 0,
        "tool_unknown_delta": 0,
    }
    tool_calls_by_key: dict[str, dict[str, Any]] = {}
    tool_order: list[str] = []
    tool_aliases: dict[str, str] = {}
    last_tool_key: Optional[str] = None

    def _args_to_str(value: Any) -> str:
        if isinstance(value, (dict, list)):
            try:
                return json.dumps(value)
            except Exception:
                return "{}"
        if value is None:
            return ""
        return str(value)

    def _ensure_tool(tool_key: str) -> tuple[dict[str, Any], int]:
        tool_key = tool_aliases.get(tool_key, tool_key)
        if tool_key in tool_calls_by_key:
            return tool_calls_by_key[tool_key], tool_order.index(tool_key)
        rec = {
            "id": tool_key,
            "name": "",
            "args": "",
            "emitted_args_len": 0,
            "emitted_name": False,
        }
        tool_calls_by_key[tool_key] = rec
        tool_order.append(tool_key)
        return rec, len(tool_order) - 1

    def _emit_tool_delta(
        evt: dict[str, Any],
        tc_index: int,
        call_id: str,
        fn_name: Optional[str] = None,
        args_fragment: Optional[str] = None,
    ) -> None:
        nonlocal saw_tool_call
        tool_delta: dict[str, Any] = {
            "index": tc_index,
            "id": call_id,
        }
        if fn_name is not None or args_fragment is not None:
            fn_obj: dict[str, Any] = {}
            if fn_name is not None:
                fn_obj["name"] = fn_name
            if args_fragment is not None:
                fn_obj["arguments"] = args_fragment
            tool_delta["type"] = "function"
            tool_delta["function"] = fn_obj

        chunk = {
            "id": evt.get("response_id") or f"chatcmpl_{int(time.time() * 1000)}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [tool_delta]},
                    "finish_reason": None,
                }
            ],
        }
        out_lines.append(f"data: {json.dumps(chunk)}\n")
        saw_tool_call = True

    def _normalize_usage(u: Any) -> Optional[dict[str, int]]:
        if not isinstance(u, dict):
            return None
        prompt_tokens = int(u.get("input_tokens") or u.get("prompt_tokens") or 0)
        completion_tokens = int(
            u.get("output_tokens") or u.get("completion_tokens") or 0
        )
        total_tokens = int(u.get("total_tokens") or (prompt_tokens + completion_tokens))
        if prompt_tokens <= 0 and completion_tokens <= 0 and total_tokens <= 0:
            return None
        normalized = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

        prompt_details = u.get("prompt_tokens_details")
        if isinstance(prompt_details, dict) and prompt_details:
            normalized["prompt_tokens_details"] = prompt_details
        return normalized

    for raw in (sse_text or "").splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        if payload == "[DONE]":
            out_lines.append("data: [DONE]\n")
            continue
        try:
            evt = json.loads(payload)
        except Exception:
            continue

        oauth_tool_stats["events"] += 1
        evt_type = str(evt.get("type") or "").lower()
        if oauth_usage is None:
            oauth_usage = _normalize_usage(evt.get("usage"))
        if oauth_usage is None:
            oauth_usage = _normalize_usage((evt.get("response") or {}).get("usage"))
        delta_text = ""
        if evt_type in (
            "response.output_text.delta",
            "response.output.delta",
            "output_text.delta",
        ):
            delta_text = str(evt.get("delta") or evt.get("text") or "")
        elif evt_type in (
            "response.output_item.added",
            "response.output_text.added",
        ):
            item = evt.get("item") or {}
            if isinstance(item, dict):
                item_type = str(item.get("type") or "").lower()
                if item_type in ("function_call", "tool_call"):
                    oauth_tool_stats["tool_added"] += 1
                    fn_name = str(item.get("name") or item.get("tool_name") or "").strip()
                    fn_args = _args_to_str(item.get("arguments"))
                    call_id = str(item.get("call_id") or item.get("id") or "").strip()
                    if not call_id:
                        call_id = f"call_{int(time.time() * 1000)}"
                    item_id = str(item.get("id") or "").strip()
                    tool_key = call_id
                    rec, tc_index = _ensure_tool(tool_key)
                    if item_id and item_id != tool_key:
                        tool_aliases[item_id] = tool_key
                    rec["id"] = call_id
                    if fn_name:
                        rec["name"] = fn_name
                    if fn_args:
                        rec["args"] = str(rec.get("args") or "") + fn_args
                    last_tool_key = tool_key
                    if fn_name:
                        if not bool(rec.get("emitted_name")):
                            _emit_tool_delta(
                                evt=evt,
                                tc_index=tc_index,
                                call_id=call_id,
                                fn_name=fn_name,
                                args_fragment="",
                            )
                            rec["emitted_name"] = True
                        if fn_args:
                            _emit_tool_delta(
                                evt=evt,
                                tc_index=tc_index,
                                call_id=call_id,
                                args_fragment=fn_args,
                            )
                            rec["emitted_args_len"] = int(rec.get("emitted_args_len") or 0) + len(
                                fn_args
                            )
                # Intentionally ignore text from output_item.added/output_text.added
                # to avoid duplicate text chunks; rely on output_text.delta events.
        elif evt_type == "response.function_call_arguments.delta":
            oauth_tool_stats["tool_arg_delta"] += 1
            arg_delta = _args_to_str(evt.get("delta"))
            if arg_delta:
                tool_key = str(evt.get("call_id") or evt.get("item_id") or "").strip()
                if not tool_key:
                    tool_key = last_tool_key or ""
                tool_key = tool_aliases.get(tool_key, tool_key)
                if tool_key:
                    rec, tc_index = _ensure_tool(tool_key)
                    call_id = str(rec.get("id") or tool_key)
                    rec["args"] = str(rec.get("args") or "") + arg_delta
                    _emit_tool_delta(
                        evt=evt,
                        tc_index=tc_index,
                        call_id=call_id,
                        args_fragment=arg_delta,
                    )
                    rec["emitted_args_len"] = int(rec.get("emitted_args_len") or 0) + len(arg_delta)
                    last_tool_key = tool_key
                else:
                    oauth_tool_stats["tool_unknown_delta"] += 1
        elif evt_type == "response.output_item.done":
            item = evt.get("item") or {}
            if isinstance(item, dict):
                item_type = str(item.get("type") or "").lower()
                if item_type in ("function_call", "tool_call"):
                    oauth_tool_stats["tool_done"] += 1
                    call_id = str(item.get("call_id") or item.get("id") or "").strip()
                    if not call_id:
                        call_id = last_tool_key or f"call_{int(time.time() * 1000)}"
                    item_id = str(item.get("id") or "").strip()
                    tool_key = call_id
                    rec, tc_index = _ensure_tool(tool_key)
                    if item_id and item_id != tool_key:
                        tool_aliases[item_id] = tool_key
                    rec["id"] = call_id
                    fn_name = str(item.get("name") or item.get("tool_name") or "").strip()
                    if fn_name:
                        rec["name"] = fn_name
                        if not bool(rec.get("emitted_name")):
                            _emit_tool_delta(
                                evt=evt,
                                tc_index=tc_index,
                                call_id=call_id,
                                fn_name=fn_name,
                                args_fragment="",
                            )
                            rec["emitted_name"] = True

                    full_args = _args_to_str(item.get("arguments"))
                    if full_args:
                        emitted_len = int(rec.get("emitted_args_len") or 0)
                        prev = str(rec.get("args") or "")
                        rec["args"] = full_args
                        if emitted_len > 0 and full_args.startswith(prev[:emitted_len]):
                            missing = full_args[emitted_len:]
                        else:
                            missing = full_args
                        if missing:
                            _emit_tool_delta(
                                evt=evt,
                                tc_index=tc_index,
                                call_id=call_id,
                                args_fragment=missing,
                            )
                            rec["emitted_args_len"] = emitted_len + len(missing)
                    elif int(rec.get("emitted_args_len") or 0) == 0:
                        _emit_tool_delta(
                            evt=evt,
                            tc_index=tc_index,
                            call_id=call_id,
                            args_fragment="{}",
                        )
                        rec["emitted_args_len"] = 2
                    last_tool_key = tool_key

        if delta_text:
            chunk = {
                "id": evt.get("response_id") or f"chatcmpl_{int(time.time() * 1000)}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": delta_text},
                        "finish_reason": None,
                    }
                ],
            }
            out_lines.append(f"data: {json.dumps(chunk)}\n")

        if evt_type in ("response.completed", "response.done", "completed"):
            finish_reason = "tool_calls" if saw_tool_call else "stop"
            final_chunk = {
                "id": evt.get("response_id") or f"chatcmpl_{int(time.time() * 1000)}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model_name,
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": finish_reason}
                ],
            }
            if oauth_usage:
                final_chunk["usage"] = oauth_usage
            out_lines.append(f"data: {json.dumps(final_chunk)}\n")
            out_lines.append("data: [DONE]\n")

    if oauth_tool_stats["events"] > 0:
        debug_log(
            "info",
            f"[OAUTH_SSE] model={model_name} events={oauth_tool_stats['events']} tool_added={oauth_tool_stats['tool_added']} tool_arg_delta={oauth_tool_stats['tool_arg_delta']} tool_done={oauth_tool_stats['tool_done']} unknown_delta={oauth_tool_stats['tool_unknown_delta']} usage={'yes' if oauth_usage else 'no'}",
        )
        if is_debug_mode() == "full" and tool_order:
            tool_debug_parts: list[str] = []
            for key in tool_order[:12]:
                rec = tool_calls_by_key.get(key) or {}
                args_len, args_preview = _tool_args_preview(rec.get("args"), max_len=220)
                tool_debug_parts.append(
                    " ".join(
                        [
                            f"id={_truncate_debug_text(rec.get('id') or key, max_len=64)}",
                            f"name={_truncate_debug_text(rec.get('name') or '', max_len=64)}",
                            f"args_len={args_len}",
                            f"args={args_preview}",
                        ]
                    )
                )
            debug_log(
                "info",
                f"[OAUTH_SSE_TOOLS] model={model_name} calls={len(tool_order)} details={' || '.join(tool_debug_parts)}",
            )

    if not out_lines:
        return sse_text
    return "\n".join(out_lines)


def _extract_code_and_state_from_redirect_input(raw: str) -> tuple[str, str, str]:
    text = (raw or "").strip()
    if not text:
        return "", "", "Missing input"
    if "?" in text or "code=" in text:
        try:
            parsed = urllib.parse.urlparse(text)
            query = urllib.parse.parse_qs(parsed.query)
            code = (query.get("code", [""])[0] or "").strip()
            state = (query.get("state", [""])[0] or "").strip()
            if not code and text.startswith("code="):
                query2 = urllib.parse.parse_qs(text)
                code = (query2.get("code", [""])[0] or "").strip()
                state = (query2.get("state", [""])[0] or "").strip()
            if not code:
                return "", "", "Could not extract code from redirect input"
            return code, state, ""
        except Exception:
            return "", "", "Invalid redirect URL"
    return text, "", ""


def _exchange_openai_oauth_code(
    token_url: str,
    code: str,
    client_id: str,
    redirect_uri: str,
    code_verifier: str,
    client_secret: str = "",
) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[str]]:
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    if client_secret:
        payload["client_secret"] = client_secret

    try:
        resp = httpx.post(
            token_url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=payload,
            timeout=20,
        )
    except Exception as exc:
        return None, f"Token exchange request failed: {exc}", None

    if resp.status_code >= 400:
        return None, f"Token exchange failed ({resp.status_code})", resp.text[:300]

    try:
        token_data = resp.json()
    except Exception:
        return None, "Token exchange succeeded but returned invalid JSON", resp.text[:300]
    return token_data, None, None


def get_default_ollama_endpoint() -> Optional[dict]:
    """Return highest-priority enabled endpoint configured as ollama."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT *
                FROM endpoints
                WHERE enabled = 1 AND lower(endpoint_type) = 'ollama'
                ORDER BY priority DESC, id ASC
                LIMIT 1
            """
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception:
        return None


def _resolve_ollama_target_for_request(
    model_name: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve Ollama base URL/api_key using model backend or default ollama endpoint."""
    if model_name:
        backend = get_backend(model_name)
        if backend is not None and getattr(backend, "endpoint_type", "") == "ollama":
            return getattr(backend, "url", None), getattr(backend, "api_key", None)

    endpoint = get_default_ollama_endpoint()
    if endpoint:
        return endpoint.get("url"), endpoint.get("api_key")
    return None, None


def _ollama_streaming_not_supported_response() -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={
            "error": "Streaming Ollama translation is not supported for the current backend"
        },
    )


def _backend_effective_url(backend: Any) -> str:
    url = getattr(backend, "url", None)
    if not url and hasattr(backend, "primary"):
        url = getattr(backend.primary, "url", None)
    if not url:
        raise AttributeError("backend has no url")
    return str(url)


def _openai_backend_endpoint_for_chat(backend: Any) -> tuple[str, str]:
    endpoint = _backend_effective_url(backend)
    endpoint_type = getattr(backend, "endpoint_type", "")
    if endpoint_type == "deepinfra":
        endpoint = f"{endpoint}/v1/openai/chat/completions"
    elif endpoint_type == "openai_oauth":
        endpoint = _resolve_openai_oauth_response_endpoint(endpoint)
    elif endpoint_type == "openwebui":
        endpoint = f"{endpoint}/api/chat/completions"
    elif endpoint_type == "queue":
        endpoint = f"{endpoint}/v1/chat/completions"
    else:
        endpoint = f"{endpoint}/v1/chat/completions"
    return endpoint, endpoint_type


async def _call_backend_chat_via_stream_collect(
    *,
    backend: Any,
    model: str,
    messages: list[dict[str, Any]],
    temperature: Any,
    max_tokens: Any,
    top_p: Any,
    tools: list[Any],
    incoming_source: str,
) -> tuple[dict[str, Any], Optional[dict[str, Any]], int]:
    actual_model = getattr(backend, "model", model)
    endpoint, endpoint_type = _openai_backend_endpoint_for_chat(backend)
    payload = {
        "model": actual_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": top_p,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
    if endpoint_type == "openai_oauth":
        payload = _openai_chat_to_openai_oauth_payload(
            messages=messages,
            model=actual_model,
            stream=True,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            tools=tools,
            tool_choice=None,
            parallel_tool_calls=None,
        )

    headers = {"Content-Type": "application/json"}
    auth_header = await resolve_endpoint_auth_header_async(
        {
            "id": getattr(backend, "endpoint_id", None),
            "api_key": getattr(backend, "api_key", ""),
            "oauth_enabled": getattr(backend, "oauth_enabled", 0),
            "oauth_grant_type": getattr(backend, "oauth_grant_type", ""),
            "oauth_token_url": getattr(backend, "oauth_token_url", ""),
            "oauth_client_id": getattr(backend, "oauth_client_id", ""),
            "oauth_client_secret": getattr(backend, "oauth_client_secret", ""),
            "oauth_scope": getattr(backend, "oauth_scope", ""),
            "oauth_refresh_token": getattr(backend, "oauth_refresh_token", ""),
            "oauth_token_expires_at": getattr(backend, "oauth_token_expires_at", 0),
            "oauth_token_request_format": getattr(
                backend, "oauth_token_request_format", "json"
            ),
            "oauth_client_auth_method": getattr(
                backend, "oauth_client_auth_method", "client_secret_post"
            ),
        }
    )
    if auth_header:
        headers["Authorization"] = auth_header
    headers["X-Source"] = incoming_source
    headers["X-Model"] = actual_model
    headers["X-Priority"] = "NORMAL"

    stream_data = ""
    async with httpx.AsyncClient(timeout=1200.0) as client:
        async with client.stream("POST", endpoint, headers=headers, json=payload) as resp:
            if resp.status_code >= 400:
                error_text = await resp.aread()
                return (
                    None,
                    {
                        "error": {
                            "message": error_text.decode("utf-8", errors="replace"),
                            "type": "upstream_error",
                        }
                    },
                    int(resp.status_code or 500),
                )
            async for chunk in resp.aiter_text():
                stream_data += chunk

    converted = (
        _openai_oauth_sse_to_openai_chat_sse(stream_data, actual_model)
        if endpoint_type == "openai_oauth"
        else stream_data
    )
    result = {"_stream_data": converted, "id": f"chat-{int(time.time())}"}
    return result, None, 200


def _parse_chat_stream_payload(
    *,
    stream_data: str,
    backend: Any,
    messages: list[dict[str, Any]],
    tools: list[Any],
    model: str,
    request_id: str,
) -> dict[str, Any]:
    full_content = ""
    full_reasoning = ""
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    stream_tool_calls = []
    has_tool_calls = False
    finish_reason = None
    stats = {
        "total_lines": 0,
        "data_lines": 0,
        "json_ok": 0,
        "json_fail": 0,
        "first_error": None,
    }

    for line in stream_data.split("\n"):
        stats["total_lines"] += 1
        stripped = line.strip()
        if not stripped:
            continue
        if not (stripped.startswith("data:") or stripped.startswith("data ")):
            continue

        payload = stripped
        if stripped.startswith("data:"):
            payload = stripped[5:]
            if payload.startswith(" "):
                payload = payload[1:]
        if not payload or payload.strip() == "[DONE]":
            continue

        stats["data_lines"] += 1
        try:
            chunk = json.loads(payload)
            stats["json_ok"] += 1
            if "choices" in chunk and chunk["choices"]:
                delta = chunk["choices"][0].get("delta", {})
                tc = delta.get("tool_calls")
                if tc:
                    for tc_chunk in tc:
                        tc_index = tc_chunk.get("index", 0)
                        while len(stream_tool_calls) <= tc_index:
                            stream_tool_calls.append({"index": tc_index})
                        if tc_chunk.get("id") and not stream_tool_calls[tc_index].get("id"):
                            stream_tool_calls[tc_index]["id"] = tc_chunk["id"]
                        if tc_chunk.get("type") and not stream_tool_calls[tc_index].get("type"):
                            stream_tool_calls[tc_index]["type"] = tc_chunk["type"]
                        func = tc_chunk.get("function", {})
                        if "function" not in stream_tool_calls[tc_index]:
                            stream_tool_calls[tc_index]["function"] = {}
                        func_target = stream_tool_calls[tc_index]["function"]
                        if func.get("name") and not func_target.get("name"):
                            func_target["name"] = func["name"]
                        arg_fragment = func.get("arguments", "")
                        if arg_fragment:
                            current_args = func_target.get("arguments", "")
                            func_target["arguments"] = current_args + arg_fragment
                        if tc_chunk.get("arguments") and not func_target.get("arguments"):
                            func_target["arguments"] = tc_chunk["arguments"]
                    has_tool_calls = True
                if not has_tool_calls and delta.get("content"):
                    full_content += delta["content"]
                if delta.get("reasoning_content"):
                    full_reasoning += delta["reasoning_content"]
                finish_reason = chunk["choices"][0].get("finish_reason")
            if "usage" in chunk and chunk["usage"]:
                usage = chunk["usage"]
        except json.JSONDecodeError as e:
            stats["json_fail"] += 1
            if not stats["first_error"]:
                stats["first_error"] = f"line={stats['total_lines']} err={str(e)[:40]}"
        except Exception as e:
            stats["json_fail"] += 1
            if not stats["first_error"]:
                stats["first_error"] = f"line={stats['total_lines']} err={str(e)[:40]}"

    has_vision = any(
        isinstance(c, dict) and c.get("type") == "image_url"
        for msg in messages
        if isinstance(msg, dict)
        for c in (msg.get("content") or [])
    )
    is_openai_oauth_backend = (
        hasattr(backend, "endpoint_type") and backend.endpoint_type == "openai_oauth"
    )
    advertised_tool_names = _advertised_tool_names(tools)

    if has_vision:
        text_content = full_content
        extracted_tc = stream_tool_calls if stream_tool_calls else []
    elif is_openai_oauth_backend:
        text_content = full_content
        extracted_tc = []
    else:
        extracted_tc, text_content = process_content(full_content)

    if stream_tool_calls and not extracted_tc:
        extracted_tc = stream_tool_calls

    if extracted_tc:
        filtered_tc, _ = _filter_tool_calls_by_advertised(extracted_tc, advertised_tool_names)
        extracted_tc = filtered_tc

    stream_error = None
    for line in stream_data.split("\n"):
        stripped = line.strip()
        if stripped.startswith("data:") or stripped.startswith("data "):
            payload = stripped
            if stripped.startswith("data:"):
                payload = stripped[5:]
                if payload.startswith(" "):
                    payload = payload[1:]
            if payload and payload.strip() != "[DONE]":
                try:
                    data = json.loads(payload)
                    if "error" in data:
                        err = data["error"]
                        stream_error = err.get("message") if isinstance(err, dict) else str(err)
                        break
                    if "error_type" in data or "error_message" in data:
                        stream_error = data.get("error_message") or data.get("error_type") or str(data)
                        break
                except Exception:
                    pass

    if is_openai_oauth_backend:
        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or 0)
        if total_tokens <= 0:
            total_tokens = prompt_tokens + completion_tokens
        if prompt_tokens <= 0 and completion_tokens <= 0 and total_tokens <= 0:
            usage = _estimate_openai_oauth_usage(
                messages=messages,
                tools=tools,
                full_content=full_content,
                full_reasoning=full_reasoning,
                tool_calls=extracted_tc or [],
            )
        else:
            usage["prompt_tokens"] = prompt_tokens
            usage["completion_tokens"] = completion_tokens
            usage["total_tokens"] = total_tokens

    return {
        "full_content": full_content,
        "full_reasoning": full_reasoning,
        "text_content": text_content,
        "tool_calls": extracted_tc or [],
        "usage": usage,
        "finish_reason": finish_reason,
        "stream_error": stream_error,
        "stats": stats,
    }


async def _ollama_passthrough(
    request: Request, upstream_path: str, method: str = "POST"
):
    """Forward request to configured Ollama endpoint for full API compatibility."""
    body = None
    model_name = None
    if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
        try:
            body = await request.json()
            if isinstance(body, dict):
                model_name = body.get("model")
        except Exception:
            body = None

    url, api_key = _resolve_ollama_target_for_request(model_name)
    if not url:
        return JSONResponse(
            status_code=404,
            content={"error": "No enabled Ollama endpoint is configured"},
        )

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    stream_requested = isinstance(body, dict) and bool(body.get("stream", False))
    full_url = f"{url}{upstream_path}"

    if stream_requested:

        async def ndjson_generator():
            async with httpx.AsyncClient(timeout=1200.0) as client:
                async with client.stream(
                    method.upper(), full_url, headers=headers, json=body
                ) as resp:
                    async for line in resp.aiter_lines():
                        if line:
                            yield line + "\n"

        return StreamingResponse(ndjson_generator(), media_type="application/x-ndjson")

    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.request(
            method.upper(), full_url, headers=headers, json=body
        )
        try:
            content = resp.json()
            return JSONResponse(status_code=resp.status_code, content=content)
        except Exception:
            return JSONResponse(
                status_code=resp.status_code, content={"error": resp.text}
            )


def create_backend_from_virtual_model(vm: dict) -> LLMBackend:
    """Create a backend from virtual model configuration."""
    endpoint_type = vm.get("endpoint_type", "openai")

    class VirtualModelBackend(LLMBackend):
        def __init__(self):
            self.virtual_model_id = vm.get("id")
            self.endpoint_id = vm.get("endpoint_id")
            self.url = vm["endpoint_url"]
            self.api_key = vm.get("endpoint_api_key", "")
            self.model = vm["actual_model"]
            self.endpoint_type = endpoint_type
            self.disable_streaming = vm.get("disable_streaming", 0) == 1
            self.virtual_model_name = vm.get("name")
            self.custom_headers = vm.get("custom_headers", "")
            self.show_reasoning = vm.get("show_reasoning", 1) == 1
            self.oauth_enabled = vm.get("oauth_enabled", 0)
            self.oauth_grant_type = vm.get("oauth_grant_type", "refresh_token")
            self.oauth_token_url = vm.get("oauth_token_url", "")
            self.oauth_client_id = vm.get("oauth_client_id", "")
            self.oauth_client_secret = vm.get("oauth_client_secret", "")
            self.oauth_scope = vm.get("oauth_scope", "")
            self.oauth_refresh_token = vm.get("oauth_refresh_token", "")
            self.oauth_token_expires_at = vm.get("oauth_token_expires_at", 0)
            self.oauth_token_request_format = vm.get("oauth_token_request_format", "json")
            self.oauth_client_auth_method = vm.get(
                "oauth_client_auth_method", "client_secret_post"
            )

        async def chat_completion(
            self,
            messages,
            model,
            temperature=0.7,
            max_tokens=256,
            top_p=1.0,
            stream=False,
            tools=None,
            **kwargs,
        ):
            # Extract request_id for correlation
            request_id = kwargs.pop("_request_id", None)
            inbound_audit = kwargs.pop("_inbound_audit", None)

            # Force non-streaming if disabled in model config
            if self.disable_streaming:
                stream = False

            headers = {"Content-Type": "application/json"}
            auth_header = await resolve_endpoint_auth_header_async(
                {
                    "id": self.endpoint_id,
                    "api_key": self.api_key,
                    "oauth_enabled": self.oauth_enabled,
                    "oauth_grant_type": self.oauth_grant_type,
                    "oauth_token_url": self.oauth_token_url,
                    "oauth_client_id": self.oauth_client_id,
                    "oauth_client_secret": self.oauth_client_secret,
                    "oauth_scope": self.oauth_scope,
                    "oauth_refresh_token": self.oauth_refresh_token,
                    "oauth_token_expires_at": self.oauth_token_expires_at,
                    "oauth_token_request_format": self.oauth_token_request_format,
                    "oauth_client_auth_method": self.oauth_client_auth_method,
                }
            )
            if auth_header:
                headers["Authorization"] = auth_header

            # Add X-Source header for tracking - use incoming from kwargs if available
            incoming_src = kwargs.get("_incoming_source", "serverless-proxy")
            headers["X-Source"] = incoming_src

            # Upstream request logging
            debug_log(
                "info",
                f"[UPSTREAM_REQ] request_id={request_id} backend={self.endpoint_type} url={self.url} model={self.model} stream={stream}",
            )

            # Add custom headers if configured
            if self.custom_headers:
                try:
                    custom = json.loads(self.custom_headers)
                    if isinstance(custom, dict):
                        headers.update(custom)
                except:
                    pass

            # Use actual model from virtual model config
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "top_p": top_p,
                "stream": stream,
            }
            ollama_openai_payload = None

            if self.endpoint_type == "openai_oauth":
                payload = _openai_chat_to_openai_oauth_payload(
                    messages=messages,
                    model=self.model,
                    stream=stream,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    top_p=top_p,
                    tools=tools,
                    tool_choice=kwargs.get("tool_choice"),
                    parallel_tool_calls=kwargs.get("parallel_tool_calls"),
                )

            if self.endpoint_type == "ollama":
                ollama_openai_payload = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "top_p": top_p,
                    "stream": stream,
                }
                if tools:
                    ollama_openai_payload["tools"] = tools
                for k, v in kwargs.items():
                    if (
                        v is not None
                        and not k.startswith("_")
                        and k
                        not in (
                            "stop",
                            "presence_penalty",
                            "frequency_penalty",
                        )
                    ):
                        ollama_openai_payload[k] = v

            if self.endpoint_type == "ollama":
                payload = _openai_to_ollama_chat_payload(
                    model=self.model,
                    messages=messages,
                    stream=stream,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    top_p=top_p,
                    tools=tools,
                    kwargs=kwargs,
                )

            # Debug: log message structure for vision requests
            if messages:
                first_user = None
                for msg in messages:
                    if msg.get("role") == "user":
                        first_user = msg
                        break
                if first_user:
                    content = first_user.get("content", "")
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "image_url":
                                debug_log(
                                    "info",
                                    f"[VM_PAYLOAD] request_id={request_id} has_vision=True model={self.model} endpoint_type={self.endpoint_type}",
                                )

            # Anthropic-specific payload transformation
            if self.endpoint_type == "anthropic":
                # Extract system prompt from messages if present
                system_prompt = ""
                filtered_messages = []
                for msg in messages:
                    if msg.get("role") == "system":
                        system_prompt = msg.get("content", "")
                    else:
                        # Anthropic requires content to be string or array of content blocks
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            filtered_messages.append(
                                {"role": msg.get("role"), "content": content}
                            )
                        else:
                            # Pass through as-is for complex content
                            filtered_messages.append(msg)

                # Rebuild payload for Anthropic format
                payload = {
                    "model": self.model,
                    "messages": filtered_messages,
                    "max_tokens": max_tokens,
                }
                if system_prompt:
                    payload["system"] = system_prompt
                if temperature is not None:
                    payload["temperature"] = temperature
                if top_p is not None:
                    payload["top_p"] = top_p
                if stream:
                    payload["stream"] = stream
                if tools:
                    payload["tools"] = tools

            # For non-Anthropic backends (OpenAI-compatible), always add tools if provided
            # This ensures tool_choice works correctly
            if self.endpoint_type not in ("anthropic", "ollama", "openai_oauth") and tools:
                payload["tools"] = tools

            # Safety: if tool_choice/parallel_tool_calls but no tools, strip them to avoid upstream validation errors
            # This can happen when client sends tool_choice but tools get lost in transformation
            tool_choice = kwargs.get("tool_choice")
            parallel_tool_calls = kwargs.get("parallel_tool_calls")
            if (
                tool_choice is not None or parallel_tool_calls is not None
            ) and not tools:
                debug_log(
                    "warn",
                    f"[TOOL_SAFETY] Stripping tool_choice/parallel_tool_calls - no tools provided. request_id={request_id}",
                )
                # Remove these from kwargs before they're added to payload
                kwargs = {
                    k: v
                    for k, v in kwargs.items()
                    if k not in ("tool_choice", "parallel_tool_calls")
                }

            if self.endpoint_type != "ollama":
                for k, v in kwargs.items():
                    if self.endpoint_type == "openai_oauth":
                        continue
                    if v is not None and k not in [
                        "stop",
                        "presence_penalty",
                        "frequency_penalty",
                    ]:
                        payload[k] = v

            # For RunPod serverless, use /runsync endpoint which waits for completion
            if self.endpoint_type == "runpod":
                # Extract endpoint ID from URL (e.g., https://api.runpod.ai/v2/endpoint_id/run)
                endpoint_id = (
                    self.url.split("/")[-2]
                    if self.url.endswith("/run")
                    else self.url.split("/")[-1]
                )
                endpoint = f"https://api.runpod.ai/v2/{endpoint_id}/runsync"
                payload = {"input": payload}
            elif self.endpoint_type in ("openai", "openai_oauth"):
                if self.endpoint_type == "openai_oauth":
                    endpoint = _resolve_openai_oauth_response_endpoint(self.url)
                else:
                    endpoint = f"{self.url}/v1/chat/completions"
            elif self.endpoint_type == "openwebui":
                endpoint = f"{self.url}/api/chat/completions"
            elif self.endpoint_type == "together":
                endpoint = f"{self.url}/v1/chat/completions"
            elif self.endpoint_type == "deepinfra":
                endpoint = f"{self.url}/v1/openai/chat/completions"
            elif self.endpoint_type == "anthropic":
                endpoint = f"{self.url}/v1/messages"
            elif self.endpoint_type == "queue":
                endpoint = f"{self.url}/v1/chat/completions"
            elif self.endpoint_type == "ollama":
                endpoint = f"{self.url}/v1/chat/completions"
            else:
                endpoint = f"{self.url}/v1/chat/completions"

            timeout = 1200.0 if stream else 300.0
            debug_log(
                "info",
                f"[VM_BACKEND] request_id={request_id} Calling {endpoint}, model={payload.get('model')}, stream={stream}",
            )

            outbound_audit = _audit_message_payload(payload.get("messages", []))
            _log_payload_audit("outbound", request_id, outbound_audit)
            _persist_payload_snapshot(
                request_id=request_id,
                stage="outbound",
                model=str(payload.get("model", "")),
                messages=payload.get("messages", []),
                audit_summary=outbound_audit,
            )
            if inbound_audit and outbound_audit:
                in_digests = sorted(inbound_audit.get("digests", []))
                out_digests = sorted(outbound_audit.get("digests", []))
                if in_digests != out_digests:
                    print(
                        f"[PAYLOAD_AUDIT:WARN] request_id={request_id} digest_mismatch inbound={in_digests[:4]} outbound={out_digests[:4]}",
                        flush=True,
                    )

            async with httpx.AsyncClient(timeout=timeout) as client:
                try:
                    used_ollama_api_chat = False
                    if self.endpoint_type == "ollama":
                        response = await client.post(
                            endpoint,
                            headers=headers,
                            json=(ollama_openai_payload or payload),
                        )
                        if response.status_code in (404, 405):
                            endpoint = f"{self.url}/api/chat"
                            response = await client.post(
                                endpoint, headers=headers, json=payload
                            )
                            used_ollama_api_chat = True
                    else:
                        response = await client.post(
                            endpoint, headers=headers, json=payload
                        )
                except Exception as e:
                    debug_log(
                        "error",
                        f"[VM_BACKEND_ERR] request_id={request_id} error={str(e)}",
                    )
                    return (
                        None,
                        {
                            "error": {
                                "message": f"Request error: {str(e)}",
                                "type": "internal_server_error",
                            }
                        },
                        500,
                    )

                if response.status_code != 200:
                    if self.endpoint_type == "ollama":
                        try:
                            payload_preview = json.dumps(payload)[:2000]
                        except Exception:
                            payload_preview = str(payload)[:2000]
                        print(
                            f"[OLLAMA_400] request_id={request_id} status={response.status_code} body={response.text[:500]} payload={payload_preview}",
                            flush=True,
                        )
                    body_text = (response.text or "").strip()
                    parsed_message = ""
                    try:
                        err_obj = response.json()
                        if isinstance(err_obj, dict):
                            if isinstance(err_obj.get("error"), dict):
                                parsed_message = str(
                                    err_obj.get("error", {}).get("message") or ""
                                ).strip()
                            elif isinstance(err_obj.get("error"), str):
                                parsed_message = err_obj.get("error", "").strip()
                    except Exception:
                        pass

                    detail_msg = parsed_message or body_text
                    if not detail_msg:
                        detail_msg = f"Upstream returned HTTP {response.status_code}"

                    if self.endpoint_type == "openai_oauth" and response.status_code in (
                        401,
                        403,
                    ):
                        detail_msg = (
                            f"{detail_msg} (OpenAI OAuth note: token may be missing required scope for this operation)"
                        )
                    elif self.endpoint_type == "openai_oauth" and response.status_code == 404:
                        detail_msg = (
                            f"{detail_msg} (OpenAI OAuth note: Codex backend path not found at {endpoint}. "
                            "Use endpoint URL https://chatgpt.com for openai_oauth.)"
                        )

                    return (
                        None,
                        {
                            "error": {
                                "message": detail_msg,
                                "type": "internal_server_error",
                            }
                        },
                        response.status_code,
                    )

                try:
                    if stream:
                        # For streaming, return the raw text (SSE format)
                        if self.endpoint_type == "ollama" and used_ollama_api_chat:
                            sse_data = _ollama_stream_to_openai_sse(
                                response.text, self.model
                            )
                            return {"_stream_data": sse_data}, None, 200
                        if self.endpoint_type == "openai_oauth":
                            sse_data = _openai_oauth_sse_to_openai_chat_sse(
                                response.text, self.model
                            )
                            return {"_stream_data": sse_data}, None, 200
                        debug_log(
                            "info",
                            f"[VM_BACKEND_RESP] request_id={request_id} status={response.status_code} len={len(response.text)} stream=true",
                        )
                        return {"_stream_data": response.text}, None, 200
                    result = response.json()
                    if self.endpoint_type == "openai_oauth":
                        result = _openai_oauth_response_to_chat_completion(
                            result, self.model
                        )
                    if self.endpoint_type == "ollama" and used_ollama_api_chat:
                        result = _ollama_nonstream_to_openai(result, self.model)
                    # Debug: log response details
                    choices = result.get("choices", [])
                    if choices:
                        first_choice = choices[0]
                        msg = first_choice.get("message", {})
                        content = msg.get("content") or ""
                        tc = msg.get("tool_calls") or []
                        finish = first_choice.get("finish_reason")
                        debug_log(
                            "info",
                            f"[VM_BACKEND_RESP] request_id={request_id} status={response.status_code} content_len={len(content)} tc={len(tc)} finish={finish}",
                        )
                        if tc:
                            debug_log(
                                "info",
                                f"[VM_BACKEND_RESP] request_id={request_id} tc_raw={str(tc)[:500]}",
                            )
                    else:
                        debug_log(
                            "warn",
                            f"[VM_BACKEND_RESP] request_id={request_id} No choices in response: {result.keys()}",
                        )
                except Exception as e:
                    return (
                        None,
                        {
                            "error": {
                                "message": f"JSON parse error: {str(e)}, response: {response.text[:500]}",
                                "type": "internal_server_error",
                            }
                        },
                        500,
                    )
                return result, None, 200

        async def embeddings(self, input_text, model):
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            payload = {"model": self.model, "input": input_text}

            endpoint = f"{self.url}/v1/embeddings"
            if self.endpoint_type == "openwebui":
                endpoint = f"{self.url}/api/v1/embeddings"

            if self.endpoint_type == "ollama":
                endpoint = f"{self.url}/api/embed"

            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(endpoint, headers=headers, json=payload)

                if self.endpoint_type == "ollama" and response.status_code in (
                    404,
                    405,
                ):
                    endpoint = f"{self.url}/api/embeddings"
                    response = await client.post(
                        endpoint, headers=headers, json=payload
                    )

                if response.status_code != 200:
                    return (
                        None,
                        {"error": {"message": f"Error: {response.text}"}},
                        response.status_code,
                    )

                result = response.json()

                if self.endpoint_type == "ollama":
                    embeddings = result.get("embeddings")
                    if embeddings is None and result.get("embedding") is not None:
                        embeddings = [result.get("embedding")]
                    if embeddings is None:
                        embeddings = []
                    normalized = {
                        "object": "list",
                        "data": [
                            {
                                "object": "embedding",
                                "index": i,
                                "embedding": emb,
                            }
                            for i, emb in enumerate(embeddings)
                        ],
                        "model": self.model,
                        "usage": {
                            "prompt_tokens": result.get("prompt_eval_count", 0),
                            "total_tokens": result.get("prompt_eval_count", 0),
                        },
                    }
                    return normalized, None, 200

                return result, None, 200

        async def health_check(self) -> bool:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(f"{self.url}/health")
                    return response.status_code == 200
            except Exception:
                return False

    return VirtualModelBackend()


def get_virtual_model_by_id(vm_id: int) -> Optional[dict]:
    """Return enabled virtual model row joined with endpoint metadata by id."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT vm.*, e.url as endpoint_url, e.api_key as endpoint_api_key,
                       e.endpoint_type as endpoint_type,
                       e.oauth_enabled as oauth_enabled,
                       e.oauth_grant_type as oauth_grant_type,
                       e.oauth_token_url as oauth_token_url,
                       e.oauth_client_id as oauth_client_id,
                       e.oauth_client_secret as oauth_client_secret,
                       e.oauth_scope as oauth_scope,
                       e.oauth_refresh_token as oauth_refresh_token,
                       e.oauth_token_expires_at as oauth_token_expires_at,
                       e.oauth_token_request_format as oauth_token_request_format,
                       e.oauth_client_auth_method as oauth_client_auth_method
                FROM virtual_models vm
                JOIN endpoints e ON vm.endpoint_id = e.id
                WHERE vm.id = ? AND vm.enabled = 1 AND e.enabled = 1
                """,
                (vm_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception:
        return None


def get_failover_config(vm_id: int) -> Optional[dict]:
    """Return failover config row for a virtual model."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM virtual_model_failover WHERE virtual_model_id = ?",
                (vm_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception:
        return None


def _normalize_failover_config(payload: Any) -> Optional[dict[str, Any]]:
    """Validate and normalize failover payload for DB storage."""
    if not isinstance(payload, dict):
        return None

    enabled = _coerce_bool(payload.get("enabled", True))
    strategy = str(payload.get("strategy") or "").strip().lower()
    if not enabled or strategy in ("", "none"):
        return None
    if strategy not in ("backup", "rotational", "duplicate"):
        return None

    raw_targets = payload.get("targets") or []
    if isinstance(raw_targets, str):
        try:
            raw_targets = json.loads(raw_targets)
        except Exception:
            raw_targets = []

    targets: list[dict[str, Any]] = []
    if isinstance(raw_targets, list):
        for item in raw_targets:
            if not isinstance(item, dict):
                continue
            try:
                target_vm_id = int(item.get("virtual_model_id") or 0)
            except Exception:
                target_vm_id = 0
            if target_vm_id <= 0:
                continue
            targets.append({"virtual_model_id": target_vm_id})

    if strategy in ("backup", "rotational") and not targets:
        return None

    def _to_optional_int(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            ivalue = int(value)
            return ivalue if ivalue > 0 else None
        except Exception:
            return None

    return {
        "strategy": strategy,
        "targets": targets,
        "max_attempts": _to_optional_int(payload.get("max_attempts")),
        "cooldown_seconds": _to_optional_int(payload.get("cooldown_seconds")),
        "failure_threshold": _to_optional_int(payload.get("failure_threshold")),
    }


def save_virtual_model_failover(vm_id: int, payload: Any) -> None:
    """Create/update/delete failover config for a virtual model."""
    cfg = _normalize_failover_config(payload)
    with get_db() as conn:
        cursor = conn.cursor()
        if not cfg:
            cursor.execute(
                "DELETE FROM virtual_model_failover WHERE virtual_model_id = ?",
                (vm_id,),
            )
            conn.commit()
            return

        cursor.execute(
            """
            INSERT OR REPLACE INTO virtual_model_failover
            (virtual_model_id, strategy, targets, max_attempts, cooldown_seconds, failure_threshold)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                vm_id,
                cfg["strategy"],
                json.dumps(cfg["targets"]),
                cfg["max_attempts"],
                cfg["cooldown_seconds"],
                cfg["failure_threshold"],
            ),
        )
        conn.commit()


class FailoverBackend(LLMBackend):
    """Wrapper backend that applies per-virtual-model failover strategy."""

    def __init__(self, primary_backend: LLMBackend, primary_vm: dict, config: dict):
        self.primary = primary_backend
        self.primary_vm = primary_vm
        self.config = config
        self.strategy = str(config.get("strategy") or "").strip().lower()
        self.targets = []
        try:
            self.targets = (
                json.loads(config.get("targets") or "[]")
                if isinstance(config.get("targets"), str)
                else (config.get("targets") or [])
            )
            if not isinstance(self.targets, list):
                self.targets = []
        except Exception:
            self.targets = []

        self.max_attempts = int(
            config.get("max_attempts")
            or get_setting("circuit_failure_threshold", "3")
            or 3
        )
        self.failure_threshold = int(
            config.get("failure_threshold")
            or get_setting("circuit_failure_threshold", "3")
            or 3
        )
        self.cooldown_seconds = int(
            config.get("cooldown_seconds")
            or get_setting("circuit_cooldown_seconds", "300")
            or 300
        )
        self._rr_index = 0
        # Runtime route metadata exposed to activity logging.
        self.virtual_model_name = getattr(primary_backend, "virtual_model_name", primary_vm.get("name"))
        self.model = getattr(primary_backend, "model", primary_vm.get("actual_model"))
        self.endpoint_id = getattr(primary_backend, "endpoint_id", primary_vm.get("endpoint_id"))
        self.endpoint_type = getattr(primary_backend, "endpoint_type", primary_vm.get("endpoint_type", ""))
        self.failover_used = False
        self.failover_strategy = self.strategy
        self.routed_virtual_model_name = self.virtual_model_name

    def __getattr__(self, name: str):
        return getattr(self.primary, name)

    def _set_runtime_route_meta(self, backend: LLMBackend, vm: dict, used_failover: bool) -> None:
        self.model = getattr(backend, "model", vm.get("actual_model"))
        self.endpoint_id = getattr(backend, "endpoint_id", vm.get("endpoint_id"))
        self.endpoint_type = getattr(backend, "endpoint_type", vm.get("endpoint_type", ""))
        self.failover_used = bool(used_failover)
        self.routed_virtual_model_name = str(vm.get("name") or "")

    def _build_candidates(self) -> list[tuple[LLMBackend, dict]]:
        candidates: list[tuple[LLMBackend, dict]] = [(self.primary, self.primary_vm)]

        if self.strategy in ("backup", "rotational"):
            ordered_vms: list[dict] = []
            for target in self.targets:
                if not isinstance(target, dict):
                    continue
                try:
                    vm_id = int(target.get("virtual_model_id") or 0)
                except Exception:
                    vm_id = 0
                if vm_id <= 0:
                    continue
                vm = get_virtual_model_by_id(vm_id)
                if vm:
                    ordered_vms.append(vm)

            if self.strategy == "rotational" and ordered_vms:
                offset = self._rr_index % len(ordered_vms)
                ordered_vms = ordered_vms[offset:] + ordered_vms[:offset]
                self._rr_index = (self._rr_index + 1) % len(ordered_vms)

            for vm in ordered_vms:
                candidates.append((create_backend_from_virtual_model(vm), vm))

        elif self.strategy == "duplicate":
            current_vm_id = int(self.primary_vm.get("id") or 0)
            actual_model = str(self.primary_vm.get("actual_model") or "").strip()
            if actual_model:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        SELECT vm.id
                        FROM virtual_models vm
                        JOIN endpoints e ON vm.endpoint_id = e.id
                        WHERE vm.enabled = 1
                          AND e.enabled = 1
                          AND vm.actual_model = ?
                          AND vm.id != ?
                        ORDER BY vm.id ASC
                        """,
                        (actual_model, current_vm_id),
                    )
                    rows = cursor.fetchall()
                duplicate_vms = []
                for row in rows:
                    vm = get_virtual_model_by_id(int(row[0]))
                    if vm:
                        duplicate_vms.append(vm)
                if duplicate_vms:
                    offset = self._rr_index % len(duplicate_vms)
                    duplicate_vms = duplicate_vms[offset:] + duplicate_vms[:offset]
                    self._rr_index = (self._rr_index + 1) % len(duplicate_vms)
                    for vm in duplicate_vms:
                        candidates.append((create_backend_from_virtual_model(vm), vm))

        return candidates

    async def chat_completion(
        self,
        messages: list,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 256,
        top_p: float = 1.0,
        stream: bool = False,
        tools: Optional[list] = None,
        **kwargs,
    ) -> tuple[Optional[dict], Optional[dict], int]:
        candidates = self._build_candidates()
        max_tries = max(1, min(self.max_attempts, len(candidates)))
        last_err = None
        last_status = 500

        for idx in range(max_tries):
            backend, vm = candidates[idx]
            endpoint_id = int(vm.get("endpoint_id") or 0)

            if endpoint_id > 0 and is_endpoint_circuit_open(endpoint_id):
                debug_log(
                    "info",
                    f"[FAILOVER] skipping endpoint_id={endpoint_id} vm={vm.get('name')} circuit=open",
                )
                continue

            result, error, status = await backend.chat_completion(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                stream=stream,
                tools=tools,
                **kwargs,
            )

            if status == 200 and error is None:
                self._set_runtime_route_meta(backend, vm, used_failover=(idx > 0))
                if endpoint_id > 0:
                    update_health(endpoint_id, "healthy")
                if idx > 0:
                    debug_log(
                        "warn",
                        f"[FAILOVER] switched_to endpoint_id={endpoint_id} vm={vm.get('name')} strategy={self.strategy}",
                    )
                return result, error, status

            retryable = status in (429, 500, 502, 503, 504)
            if endpoint_id > 0 and retryable:
                increment_health_failure(
                    endpoint_id,
                    f"status={status} error={error}",
                    threshold_override=self.failure_threshold,
                    cooldown_override=self.cooldown_seconds,
                )

            last_err = error
            last_status = int(status or 500)
            if not retryable:
                break

        return None, last_err, last_status

    async def embeddings(
        self,
        input_text: str,
        model: str,
    ) -> tuple[Optional[dict], Optional[dict], int]:
        return await self.primary.embeddings(input_text=input_text, model=model)

    async def health_check(self) -> bool:
        endpoint_id = int(getattr(self.primary, "endpoint_id", 0) or 0)
        if endpoint_id > 0 and is_endpoint_circuit_open(endpoint_id):
            return False
        return await self.primary.health_check()


def get_backend(model_name: str = None) -> Optional[LLMBackend]:
    """Get backend for a configured virtual model only."""

    # Enforce explicit model routing via virtual_models.
    if not model_name:
        return None

    vm = get_virtual_model(model_name)
    if vm:
        debug_log(
            "info",
            f"[BACKEND] Using virtual model backend: {model_name} -> {vm.get('endpoint_type')}",
        )
        primary = create_backend_from_virtual_model(vm)
        config = get_failover_config(int(vm.get("id") or 0))
        if config:
            return FailoverBackend(primary_backend=primary, primary_vm=vm, config=config)
        return primary

    debug_log("warn", f"[BACKEND] Model not found in virtual_models: {model_name}")
    return None


def model_not_found_response(model_name: str) -> JSONResponse:
    """OpenAI-style model not found error response."""
    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "message": f"Model '{model_name}' is not configured. Define it in virtual_models and map it to an enabled endpoint.",
                "type": "invalid_request_error",
                "code": "model_not_found",
            }
        },
    )


# Initialize backend (uses env vars for default)
BACKEND = get_backend()

# Global tool patterns cache (loaded from DB)
TOOL_PATTERNS = []


def load_tool_patterns():
    """Load tool patterns from database into memory cache."""
    global TOOL_PATTERNS

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, pattern_name, pattern_type, regex_pattern,
                       tool_name, tool_name_group, tool_name_json_path,
                       tool_name_mapping, parameter_mapping, priority
                FROM tool_patterns 
                WHERE enabled = 1 
                ORDER BY priority DESC
            """)

            TOOL_PATTERNS = []
            for row in cursor.fetchall():
                try:
                    regex = re.compile(row["regex_pattern"], re.DOTALL)
                    tool_map = (
                        json.loads(row["tool_name_mapping"])
                        if row["tool_name_mapping"]
                        else {}
                    )
                    param_map = (
                        json.loads(row["parameter_mapping"])
                        if row["parameter_mapping"]
                        else {}
                    )
                    TOOL_PATTERNS.append(
                        {
                            "id": row["id"],
                            "name": row["pattern_name"],
                            "type": row["pattern_type"],
                            "regex": regex,
                            "tool_name": row["tool_name"],
                            "tool_name_group": row["tool_name_group"],
                            "tool_name_json_path": row["tool_name_json_path"],
                            "tool_map": tool_map,
                            "param_map": param_map,
                            "priority": row["priority"],
                        }
                    )
                except re.error as e:
                    print(
                        f"Warning: Invalid regex in pattern '{row['pattern_name']}': {e}"
                    )

            print(f"Loaded {len(TOOL_PATTERNS)} tool patterns from database")
    except Exception as e:
        print(f"Error loading tool patterns: {e}")
        TOOL_PATTERNS = []


# Load patterns at module startup
load_tool_patterns()


def extract_tool_calls(content):
    """Extract tool calls from content using DB-driven patterns."""
    if not content:
        return [], content

    if not TOOL_PATTERNS:
        return [], content

    all_matches = []

    # Try each pattern in priority order
    for pattern in TOOL_PATTERNS:
        try:
            for m in pattern["regex"].finditer(content):
                tool_name = None
                raw_args = None

                # Determine tool name via one of three modes:
                if pattern["tool_name"]:  # Static
                    tool_name = pattern["tool_name"]
                elif pattern["tool_name_group"]:  # Dynamic from capture group
                    tool_name = m.group(pattern["tool_name_group"])
                elif pattern["tool_name_json_path"]:  # JSON extract
                    try:
                        inner = (m.group(1) or m.group(2) or "").strip()
                        if inner.startswith("{"):
                            obj = json.loads(inner)
                            # Simple path like "name" or "arguments.name"
                            path = pattern["tool_name_json_path"]
                            if "." in path:
                                parts = path.split(".")
                                val = obj
                                for p in parts:
                                    val = val.get(p, {})
                                tool_name = val
                            else:
                                tool_name = obj.get(path)
                    except:
                        pass
                else:
                    # Default: try to parse as JSON to get "name"
                    # Check group(2) first (the content), then group(1) (lang specifier)
                    try:
                        groups = m.groups()
                        # Use the largest group (last non-None)
                        inner = None
                        for g in reversed(groups):
                            if g:
                                inner = g.strip()
                                break
                        if inner and inner.startswith("{"):
                            obj = json.loads(inner)
                            tool_name = obj.get("name")
                    except:
                        pass

                # Apply tool_name_mapping (e.g., read_file → read)
                if tool_name:
                    tool_name = pattern["tool_map"].get(tool_name, tool_name)

                # For action patterns, detect tool from raw_args BEFORE the skip check
                raw_args = ""
                if pattern["type"] in ("fence", "inline", "xml", "bracket", "action"):
                    if pattern["type"] == "fence":
                        raw_args = m.group(2) if m.lastindex >= 2 else ""
                    elif pattern["type"] == "inline":
                        raw_args = m.group(2) if m.lastindex >= 2 else ""
                    elif pattern["type"] == "xml":
                        groups = m.groups()
                        raw_args = groups[-1] if groups else ""
                    elif pattern["type"] == "bracket":
                        raw_args = m.group(2) if m.lastindex >= 2 else ""
                    elif pattern["type"] == "action":
                        raw_args = m.group(1) if m.lastindex >= 1 else ""
                        # Detect tool from action content
                        if raw_args:
                            action_text = raw_args.lower()
                            action_map = {
                                "searching": "grep",
                                "search": "grep",
                                "using task": "task",
                                "task": "task",
                                "reading": "read",
                                "read": "read",
                                "listing": "glob",
                                "ls": "glob",
                                "glob": "glob",
                                "writing": "write",
                                "write": "write",
                                "editing": "edit",
                                "edit": "edit",
                                "running": "bash",
                                "bash": "bash",
                                "executing": "bash",
                            }
                            for key, t in action_map.items():
                                if key in action_text:
                                    tool_name = t
                                    break

                # Skip invalid tool names
                if not tool_name or tool_name == "tool_call":
                    continue

                # Parse arguments based on content
                args_dict = {}

                if raw_args:
                    # Try to parse as JSON first
                    if raw_args.startswith("{"):
                        try:
                            parsed = json.loads(raw_args)
                            if "arguments" in parsed:
                                parsed = parsed["arguments"]
                            # Apply parameter mapping
                            for k, v in parsed.items():
                                mapped_key = pattern["param_map"].get(k, k)
                                args_dict[mapped_key] = v
                        except:
                            # Not valid JSON, treat as raw text
                            if "/" in raw_args or "\\" in raw_args:
                                args_dict = {"filePath": raw_args.strip()}
                            else:
                                args_dict = {"value": raw_args.strip()}
                    else:
                        # Non-JSON content - check pattern type
                        if pattern["type"] == "xml" and "<parameter=" in raw_args:
                            param_pairs = re.findall(
                                r"<parameter=(\w+)>([\s\S]*?)</parameter>",
                                raw_args,
                                flags=re.DOTALL,
                            )
                            if param_pairs:
                                for k, v in param_pairs:
                                    mapped_key = pattern["param_map"].get(k, k)
                                    if mapped_key in (
                                        "oldString",
                                        "newString",
                                        "content",
                                    ):
                                        val = v
                                        if val.startswith("\n"):
                                            val = val[1:]
                                        if val.endswith("\n"):
                                            val = val[:-1]
                                        args_dict[mapped_key] = val
                                    else:
                                        args_dict[mapped_key] = v.strip()
                        elif pattern["type"] == "bracket":
                            # Extract path from text
                            import re as re_module

                            path_match = re_module.search(
                                r"(?:in\s+)?(/[^\s]+)", raw_args
                            )
                            if path_match:
                                args_dict = {"filePath": path_match.group(1)}
                            else:
                                # Try keyword detection
                                for prefix in [
                                    "list files in ",
                                    "search for ",
                                    "read ",
                                    "write ",
                                    "edit ",
                                ]:
                                    if raw_args.lower().startswith(prefix):
                                        raw_args = raw_args[len(prefix) :]
                                        break
                                if "/" in raw_args or "\\" in raw_args:
                                    args_dict = {"filePath": raw_args.strip()}
                                else:
                                    args_dict = {"query": raw_args.strip()}
                        elif pattern["type"] == "action":
                            pass  # Already handled above
                        else:
                            # Default: treat as value
                            if pattern["param_map"]:
                                for k, v in pattern["param_map"].items():
                                    args_dict[v] = raw_args.strip()
                            else:
                                args_dict = {"value": raw_args.strip()}

                # Tool-specific argument normalization
                if tool_name == "bash":
                    # Some model formats send command text in filePath/value/query.
                    if "command" not in args_dict:
                        cmd = None
                        for key in ("filePath", "file_path", "value", "query", "path"):
                            if key in args_dict and isinstance(args_dict[key], str):
                                cmd = args_dict[key]
                                break
                        if not cmd and isinstance(raw_args, str) and raw_args.strip():
                            cmd = raw_args.strip()
                        if cmd:
                            args_dict["command"] = cmd
                    # Bash tool schema requires a description field.
                    if "description" not in args_dict:
                        args_dict["description"] = "Run bash command"
                    # Keep bash args schema-compatible (avoid unrelated keys).
                    args_dict = {
                        "command": args_dict.get("command", ""),
                        "description": args_dict.get("description", "Run bash command"),
                    }
                elif tool_name == "read":
                    if "filePath" not in args_dict:
                        for key in ("file_path", "path", "value", "query"):
                            if key in args_dict and isinstance(args_dict[key], str):
                                args_dict["filePath"] = args_dict[key]
                                break
                    normalized = {}
                    if "filePath" in args_dict:
                        normalized["filePath"] = args_dict["filePath"]
                    for key in ("offset", "limit"):
                        if key in args_dict:
                            v = args_dict[key]
                            if isinstance(v, str):
                                sv = v.strip()
                                if sv.isdigit():
                                    v = int(sv)
                                else:
                                    continue
                            if isinstance(v, (int, float)):
                                normalized[key] = int(v)
                    if normalized:
                        args_dict = normalized
                elif tool_name == "write":
                    if "filePath" not in args_dict:
                        for key in ("file_path", "path"):
                            if key in args_dict and isinstance(args_dict[key], str):
                                args_dict["filePath"] = args_dict[key]
                                break
                    if "content" not in args_dict:
                        for key in ("value", "query"):
                            if key in args_dict and isinstance(args_dict[key], str):
                                args_dict["content"] = args_dict[key]
                                break
                    if isinstance(raw_args, str) and "<parameter=" in raw_args:
                        if "filePath" not in args_dict:
                            m_fp = re.search(
                                r"<parameter=filePath>\s*(.*?)\s*</parameter>",
                                raw_args,
                                flags=re.DOTALL,
                            )
                            if m_fp:
                                args_dict["filePath"] = m_fp.group(1).strip()
                        if "content" not in args_dict:
                            m_ct = re.search(
                                r"<parameter=content>\s*(.*?)\s*</parameter>",
                                raw_args,
                                flags=re.DOTALL,
                            )
                            if m_ct:
                                args_dict["content"] = m_ct.group(1)
                    if "filePath" in args_dict and "content" in args_dict:
                        args_dict = {
                            "filePath": args_dict["filePath"],
                            "content": args_dict["content"],
                        }
                elif tool_name == "todowrite":
                    # TodoWrite schema requires: {"todos": [ ... ]}
                    # Some model/tool-call formats omit or reshape this payload.
                    todos_val = args_dict.get("todos")

                    # Accept alternate keys that occasionally appear.
                    if todos_val is None:
                        for key in ("items", "tasks", "todo", "value"):
                            if key in args_dict:
                                todos_val = args_dict.get(key)
                                break

                    # If caller provided a single todo object, wrap it.
                    if isinstance(todos_val, dict):
                        todos_val = [todos_val]

                    # If caller provided a string, create a minimal todo entry.
                    if isinstance(todos_val, str):
                        text = todos_val.strip()
                        todos_val = (
                            [
                                {
                                    "content": text,
                                    "status": "pending",
                                    "priority": "medium",
                                }
                            ]
                            if text
                            else []
                        )

                    # Normalize each todo object to required shape.
                    normalized_todos = []
                    if isinstance(todos_val, list):
                        for item in todos_val:
                            if isinstance(item, str):
                                content = item.strip()
                                if not content:
                                    continue
                                normalized_todos.append(
                                    {
                                        "content": content,
                                        "status": "pending",
                                        "priority": "medium",
                                    }
                                )
                                continue
                            if not isinstance(item, dict):
                                continue

                            content = str(item.get("content") or "").strip()
                            if not content:
                                continue

                            status = str(item.get("status") or "pending").strip().lower()
                            if status not in ("pending", "in_progress", "completed", "cancelled"):
                                status = "pending"

                            priority = str(item.get("priority") or "medium").strip().lower()
                            if priority not in ("low", "medium", "high"):
                                priority = "medium"

                            normalized_todos.append(
                                {
                                    "content": content,
                                    "status": status,
                                    "priority": priority,
                                }
                            )

                    # Always provide todos array to satisfy schema.
                    args_dict = {"todos": normalized_todos}

                args_str = json.dumps(args_dict, ensure_ascii=False)

                all_matches.append(
                    {
                        "start": m.start(),
                        "end": m.end(),
                        "tool_name": tool_name,
                        "args_str": args_str,
                    }
                )
        except Exception as e:
            print(f"Error processing pattern {pattern.get('name', 'unknown')}: {e}")
            continue

    # Sort by position
    all_matches.sort(key=lambda x: x["start"])

    # Remove overlapping matches (first one wins)
    filtered = []
    last_end = -1
    for match in all_matches:
        if match["start"] >= last_end:
            filtered.append(match)
            last_end = match["end"]

    # Build tool_calls list
    tool_calls = []
    parts = []
    last_end = 0

    for match in filtered:
        start = match["start"]
        end = match["end"]

        if start > last_end:
            parts.append(content[last_end:start])

        tool_calls.append(
            {
                "id": f"call_{int(time.time() * 1000)}_{len(tool_calls)}",
                "type": "function",
                "function": {
                    "name": match["tool_name"],
                    "arguments": match["args_str"],
                },
            }
        )

        last_end = end

    # Continue with existing cleanup code...

    if last_end < len(content):
        remaining = content[last_end:].strip()
        if remaining:
            parts.append(remaining)

    cleaned_parts = []
    for part in parts:
        part = re.sub(r"^analysis\w*\s*", "", part)
        part = re.sub(r"^We need to[^\.]+\.?\s*", "", part)
        part = re.sub(r"^Let\'s[^\.]+\.?\s*", "", part)
        part = re.sub(r"^assistant\w*\s*", "", part)
        part = re.sub(r"^\.\.+\s*", "", part)
        part = re.sub(r"\.\.+$", "", part)
        part = re.sub(r"^analysis\w*\s*", "", part)
        part = re.sub(r"^We need to[^\.]+\.?\s*", "", part)
        part = re.sub(r"^Let\'s[^\.]+\.?\s*", "", part)
        part = re.sub(r"^assistant\w*\s*", "", part)
        part = re.sub(r"^\.\.+\s*", "", part)
        part = re.sub(r"\.\.+$", "", part)
        part = re.sub(r"^_thinking[:_\s]*", "", part, flags=re.IGNORECASE)
        part = re.sub(r"<_thinking[^>]*>", "", part, flags=re.IGNORECASE)
        part = re.sub(r"</_thinking>", "", part, flags=re.IGNORECASE)
        part = re.sub(r"<\|thinking\|>", "", part, flags=re.IGNORECASE)
        part = re.sub(r"<\|/thinking\|>", "", part, flags=re.IGNORECASE)
        part = part.strip()
        if part:
            cleaned_parts.append(part)

    remaining_text = "\n".join(cleaned_parts) if cleaned_parts else None
    if remaining_text:
        remaining_text = re.sub(
            r"_Thinking:_\s*", "", remaining_text, flags=re.IGNORECASE
        )
        remaining_text = re.sub(
            r"_Thinking:\s*", "", remaining_text, flags=re.IGNORECASE
        )
        remaining_text = re.sub(
            r"_\s*Thinking:_\s*", "", remaining_text, flags=re.IGNORECASE
        )
        remaining_text = re.sub(
            r"^\s*_Thinking:.*$", "", remaining_text, flags=re.IGNORECASE | re.MULTILINE
        )
        remaining_text = re.sub(
            r"\n\s*_Thinking:.*$",
            "",
            remaining_text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        remaining_text = re.sub(r"\n{2,}", "\n", remaining_text)
        remaining_text = remaining_text.strip()
    return tool_calls, remaining_text


def parse_json_objects(text):
    """Parse multiple separate JSON objects from text like {"name":"x"}{"name":"y"}{...}."""
    objs = []
    i = 0
    n = len(text)
    while i < n:
        while i < n and text[i] in " \t\n\r,":
            i += 1
        if i >= n or text[i] != "{":
            break
        depth = 0
        start = i
        for j in range(i, n):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        objs.append(json.loads(text[start : j + 1]))
                    except (json.JSONDecodeError, ValueError):
                        pass
                    i = j + 1
                    break
        else:
            break
    return objs


def _fix_json_newlines(text):
    """Fix real newlines inside JSON string values (malformed JSON) by escaping them."""
    result = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            result.append(c)
            i += 1
            while i < n:
                c = text[i]
                if c == "\\":
                    result.append(c)
                    i += 1
                    if i < n:
                        result.append(text[i])
                        i += 1
                elif c == '"':
                    result.append(c)
                    i += 1
                    break
                elif c in "\r\n":
                    result.append("\\n")
                    if c == "\r" and i + 1 < n and text[i + 1] == "\n":
                        i += 1
                    i += 1
                else:
                    result.append(c)
                    i += 1
        else:
            result.append(c)
            i += 1
    return "".join(result)


def _parse_bare_call(text):
    """Parse bare Python-style function call: task(description: "...", prompt: "...")."""
    text = text.strip()
    match = re.match(r"(\w+)\s*\((.*)\)$", text, re.DOTALL)
    if not match:
        return None
    func_name = match.group(1)
    args_str = match.group(2)

    args = {}
    i = 0
    n = len(args_str)

    while i < n:
        while i < n and args_str[i] in " \t\n\r,":
            i += 1
        if i >= n:
            break

        key_match = re.match(r"(\w+)\s*([:=])", args_str[i:])
        if not key_match:
            i += 1
            continue
        key = key_match.group(1)
        i += len(key) + 1
        while i < n and args_str[i] in " \t\n\r=":
            i += 1

        while i < n and args_str[i] in " \t\n\r":
            i += 1

        if i >= n:
            break

        if args_str[i] in "\"'":
            quote = args_str[i]
            i += 1
            value_parts = []
            while i < n:
                c = args_str[i]
                if c == "\\":
                    i += 1
                    if i < n:
                        nc = args_str[i]
                        if nc == "n":
                            value_parts.append("\n")
                        elif nc == "r":
                            value_parts.append("\r")
                        elif nc == "t":
                            value_parts.append("\t")
                        elif nc == quote:
                            value_parts.append(quote)
                        elif nc == "\\":
                            value_parts.append("\\")
                        else:
                            value_parts.append(nc)
                        i += 1
                elif c == quote:
                    i += 1
                    break
                else:
                    value_parts.append(c)
                    i += 1
            value = "".join(value_parts)
            args[key] = value
        else:
            comma_pos = args_str.find(",", i)
            if comma_pos < 0:
                comma_pos = n
            value = args_str[i:comma_pos].strip()
            if value.lower() in ("true", "false", "null"):
                value = value.lower() == "true"
            else:
                try:
                    value = int(value)
                except ValueError:
                    try:
                        value = float(value)
                    except ValueError:
                        pass
            args[key] = value

    return (func_name, json.dumps(args, ensure_ascii=False))


def process_content(content):
    """Process model output, removing chain-of-thought and extracting tool calls."""

    def _normalize_markdown_layout(text):
        """Heuristic fix for one-line markdown responses from some models."""
        if not text:
            return text
        # Add structure breaks before markdown blocks that were flattened into one line
        text = re.sub(r"\s+(?=##\s)", "\n\n", text)
        text = re.sub(r"\s+(?=###\s)", "\n\n", text)
        text = re.sub(r"\s+(?=-\s)", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    if not content:
        return None, None

    # Never run regex/tool-call normalization over data blocks (base64/image payloads)
    if _looks_like_data_block(content):
        debug_log(
            "warn", "Skipping process_content normalization for data-like payload"
        )
        return None, content

    tool_calls, remaining = extract_tool_calls(content)

    if remaining is not None:
        for m in re.finditer(r"(\w+)\s*\(([^)]+)\)", remaining, re.DOTALL):
            name = m.group(1)
            if name in KNOWN_TOOL_NAMES:
                bare = _parse_bare_call(m.group(0))
                if bare:
                    tool_calls.append(
                        {
                            "id": f"call_{int(time.time() * 1000)}_{len(tool_calls)}",
                            "type": "function",
                            "function": {"name": bare[0], "arguments": bare[1]},
                        }
                    )
                    remaining = remaining.replace(m.group(0), "").strip()
                    if not remaining:
                        remaining = None

    if tool_calls:
        cleaned = remaining.strip() if remaining else ""
        cleaned = re.sub(r"^analysis\w*\s*", "", cleaned)
        cleaned = re.sub(r"^analysis\w*\s*", "", cleaned)
        cleaned = re.sub(r"^_thinking[:_\s]*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"<_thinking[^>]*>", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"</_thinking>", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"<\|thinking\|>", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"<\|/thinking\|>", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"_Thinking:_\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"_Thinking:\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(
            r"^\s*_Thinking:.*$", "", cleaned, flags=re.IGNORECASE | re.MULTILINE
        )
        cleaned = re.sub(
            r"\n\s*_Thinking:.*$", "", cleaned, flags=re.IGNORECASE | re.MULTILINE
        )
        cleaned = _normalize_markdown_layout(cleaned)
        return tool_calls, cleaned if cleaned else None

    if "final:" in content:
        content = content.split("final:")[-1].strip()
    elif "assistantfinal" in content:
        content = content.split("assistantfinal")[-1].strip()
    elif "final " in content:
        content = content.split("final ")[-1].strip()

    if content.startswith("analysis"):
        content = content[8:].strip()

    content = re.sub(r"^_thinking[:_\s]*", "", content, flags=re.IGNORECASE)
    content = re.sub(r"<_thinking[^>]*>", "", content, flags=re.IGNORECASE)
    content = re.sub(r"</_thinking>", "", content, flags=re.IGNORECASE)
    content = re.sub(r"<\|thinking\|>", "", content, flags=re.IGNORECASE)
    content = re.sub(r"<\|/thinking\|>", "", content, flags=re.IGNORECASE)
    content = re.sub(r"_Thinking:_\s*", "", content, flags=re.IGNORECASE)
    content = re.sub(r"_Thinking:\s*", "", content, flags=re.IGNORECASE)
    content = re.sub(
        r"^\s*_Thinking:.*$", "", content, flags=re.IGNORECASE | re.MULTILINE
    )
    content = re.sub(
        r"\n\s*_Thinking:.*$", "", content, flags=re.IGNORECASE | re.MULTILINE
    )

    content = _normalize_markdown_layout(content)
    return None, content


def _advertised_tool_names(tools: Any) -> set[str]:
    names: set[str] = set()
    if not isinstance(tools, list):
        return names
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else {}
        name = str(fn.get("name") or "").strip()
        if name:
            names.add(name)
    return names


def _filter_tool_calls_by_advertised(
    tool_calls: list[dict[str, Any]], advertised_names: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not tool_calls:
        return [], []
    if not advertised_names:
        return tool_calls, []

    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        name = str(fn.get("name") or "").strip()
        if name in advertised_names:
            kept.append(tc)
        else:
            dropped.append(tc)
    return kept, dropped


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions endpoint."""
    auth_response = enforce_runtime_access(request)
    if auth_response is not None:
        return auth_response
    import time as time_module
    import uuid

    start_time = time_module.time()

    # Generate unique request ID for correlation
    request_id = str(uuid.uuid4())[:8]
    inbound_api_key_id, inbound_api_key_label = _get_request_inbound_key_meta(request)

    data = await request.json()

    inbound_raw_messages = data.get("messages", [])
    messages = inbound_raw_messages
    messages, base64_blocks_converted = _normalize_inline_base64_messages(messages)
    if base64_blocks_converted > 0:
        print(
            f"[BASE64_NORMALIZE] request_id={request_id} converted_blocks={base64_blocks_converted}",
            flush=True,
        )

    inbound_audit = _audit_message_payload(messages)
    _log_payload_audit("inbound", request_id, inbound_audit)
    _persist_payload_snapshot(
        request_id=request_id,
        stage="inbound_raw",
        model=str(data.get("model", "")),
        messages=inbound_raw_messages,
        audit_summary=_audit_message_payload(inbound_raw_messages),
    )
    _persist_payload_snapshot(
        request_id=request_id,
        stage="inbound_normalized",
        model=str(data.get("model", "")),
        messages=messages,
        audit_summary=inbound_audit,
    )

    model = data.get("model")
    model_l = (model or "").lower()
    is_vision_model = "-vl" in model_l or "vision" in model_l
    if (
        is_vision_model
        and inbound_audit.get("base64_text_blocks", 0) > 0
        and inbound_audit.get("image_blocks", 0) == 0
    ):
        print(
            f"[BASE64_NORMALIZE:WARN] request_id={request_id} unparsed_inline_base64 blocks={inbound_audit.get('base64_text_blocks', 0)}",
            flush=True,
        )
        response = JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "Vision request contains inline base64 text that could not be converted to image blocks. Ensure payload includes 'Image (base64): <full base64>' or send OpenAI image_url content blocks.",
                    "type": "invalid_request_error",
                    "code": "invalid_image_payload",
                }
            },
        )
        v_client_ip, v_forwarded_for = _resolve_activity_client_ip(request)
        log_recent_activity(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            request_type="chat",
            virtual_model=model or "",
            actual_model="",
            endpoint_name="",
            endpoint_id=None,
            endpoint_type="",
            inbound_api_key_id=inbound_api_key_id,
            inbound_api_key_label=inbound_api_key_label,
            client_ip=v_client_ip,
            forwarded_for=v_forwarded_for,
            x_source=(request.headers.get("x-source") or "serverless-proxy"),
            user_agent=(request.headers.get("user-agent") or ""),
            stream=bool(data.get("stream", False)),
            status_code=400,
            response_time_ms=int((time_module.time() - start_time) * 1000),
            error_summary="invalid_image_payload",
        )
        return response

    temperature = data.get("temperature", 0.7)
    max_tokens = data.get("max_tokens", 256)
    top_p = data.get("top_p", 1.0)
    stream = data.get("stream", False)
    tools = data.get("tools", [])

    # Inbound request logging (basic diagnostics)
    accept_header = request.headers.get("accept", "")
    user_agent = request.headers.get("user-agent", "")
    incoming_source = request.headers.get("x-source", "serverless-proxy")
    client_ip, forwarded_for = _resolve_activity_client_ip(request)
    debug_log(
        "info",
        f"[REQUEST] request_id={request_id} model={model} stream={stream} tools={len(tools)} ua={user_agent[:40]} x-source={incoming_source}",
    )

    # Get virtual model settings first
    vm = get_virtual_model(model)
    if vm:
        # Use default max_tokens from virtual model if not specified by client
        if vm.get("max_tokens") and "max_tokens" not in data:
            max_tokens = vm.get("max_tokens")
        # Use default temperature from virtual model if not specified
        if vm.get("temperature") and "temperature" not in data:
            temperature = vm.get("temperature")
        # Use default top_p from virtual model if not specified
        if vm.get("top_p") and "top_p" not in data:
            top_p = vm.get("top_p")
        # Prepend system prompt if set
        if vm.get("system_prompt"):
            system_msg = {"role": "system", "content": vm.get("system_prompt")}
            messages = [system_msg] + messages
        # Check if we should force non-streaming for this model
        if vm.get("force_non_streaming", 0) == 1:
            stream = False

    # Get the actual model name from virtual models if applicable
    virtual_model = model
    backend = get_backend(model)
    if backend is None:
        response = model_not_found_response(model)
        log_recent_activity(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            request_type="chat",
            virtual_model=model or "",
            actual_model="",
            endpoint_name="",
            endpoint_id=None,
            endpoint_type="",
            inbound_api_key_id=inbound_api_key_id,
            inbound_api_key_label=inbound_api_key_label,
            client_ip=client_ip,
            forwarded_for=forwarded_for,
            x_source=incoming_source,
            user_agent=user_agent,
            stream=bool(stream),
            status_code=404,
            response_time_ms=int((time_module.time() - start_time) * 1000),
            error_summary=f"model not found: {model}",
        )
        return response
    if hasattr(backend, "virtual_model_name"):
        virtual_model = backend.virtual_model_name

    endpoint_id = getattr(backend, "endpoint_id", None)
    endpoint_type = getattr(backend, "endpoint_type", "")
    actual_model = getattr(backend, "model", model)

    def _current_route_meta() -> tuple[Any, str, str, bool, str, str]:
        current_endpoint_id = getattr(backend, "endpoint_id", endpoint_id)
        current_endpoint_type = getattr(backend, "endpoint_type", endpoint_type)
        current_actual_model = getattr(backend, "model", actual_model)
        failover_used = bool(getattr(backend, "failover_used", False))
        failover_strategy = str(getattr(backend, "failover_strategy", "") or "")
        routed_vm_name = str(getattr(backend, "routed_virtual_model_name", "") or "")
        return (
            current_endpoint_id,
            current_endpoint_type,
            current_actual_model,
            failover_used,
            failover_strategy,
            routed_vm_name,
        )

    def _log_chat_activity(status_code: int, error_summary: str = "", stream_flag: Optional[bool] = None):
        (
            current_endpoint_id,
            current_endpoint_type,
            current_actual_model,
            failover_used,
            failover_strategy,
            routed_vm_name,
        ) = _current_route_meta()
        summary = str(error_summary or "")
        if failover_used:
            route_note = f"failover:{failover_strategy or 'configured'}"
            if routed_vm_name:
                route_note += f" -> {routed_vm_name}"
            summary = f"{summary} | {route_note}" if summary else route_note
        log_recent_activity(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            request_type="chat",
            virtual_model=virtual_model or "",
            actual_model=current_actual_model or "",
            endpoint_name="",
            endpoint_id=current_endpoint_id,
            endpoint_type=current_endpoint_type or "",
            inbound_api_key_id=inbound_api_key_id,
            inbound_api_key_label=inbound_api_key_label,
            client_ip=client_ip,
            forwarded_for=forwarded_for,
            x_source=incoming_source,
            user_agent=user_agent,
            stream=bool(stream if stream_flag is None else stream_flag),
            status_code=int(status_code or 0),
            response_time_ms=int((time_module.time() - start_time) * 1000),
            error_summary=summary,
        )

    # Additional OpenAI parameters (include request_id for correlation)
    extra_params = {
        "stop": data.get("stop"),
        "presence_penalty": data.get("presence_penalty"),
        "frequency_penalty": data.get("frequency_penalty"),
        "logit_bias": data.get("logit_bias"),
        "user": data.get("user"),
        "tool_choice": data.get("tool_choice"),
        "response_format": data.get("response_format"),
        "seed": data.get("seed"),
        "_request_id": request_id,
        "_inbound_audit": inbound_audit,
    }
    # Only add parallel_tool_calls if tools are present
    if tools and data.get("parallel_tool_calls") is not None:
        extra_params["parallel_tool_calls"] = data.get("parallel_tool_calls")

    # Preserve original stream request from client (before any modifications)
    original_stream = stream

    cache_attempted = 0
    cache_hit = 0
    cache_key = None

    # Compat mode: if client asks for stream but doesn't accept SSE, force non-streaming
    # BUT still track original request to return proper response format to client
    wants_sse = "text/event-stream" in accept_header.lower()
    # Also accept */* as valid - it means client accepts anything
    accepts_all = accept_header.strip() == "*/*"
    ua_lower = user_agent.lower()
    is_openai_js = "openai/js" in ua_lower or "openclaw" in ua_lower
    is_opencode = "opencode" in ua_lower or "ai-sdk" in ua_lower
    # OpenAI JS/OpenClaw can request stream=true while using application/json and
    # still correctly consume SSE. Do not force non-streaming for that client.
    if (
        stream
        and is_opencode
        and not is_openai_js
        and not wants_sse
        and not accepts_all
    ):
        print(
            f"[COMPAT] Forcing non-streaming: accept={accept_header} user-agent={user_agent}",
            flush=True,
        )
        stream = False

    # Non-stream cache lookup (virtual-model gated)
    cache_control_req = (request.headers.get("cache-control") or "").lower()
    if not stream and "no-store" not in cache_control_req and is_virtual_model_cache_enabled(model):
        cache_attempted = 1
        try:
            cache_key = normalize_request_for_cache(
                {
                    "model": model,
                    "messages": messages,
                    "tools": tools,
                    "temperature": temperature,
                    "top_p": top_p,
                    "max_tokens": max_tokens,
                    "stop": data.get("stop"),
                    "presence_penalty": data.get("presence_penalty"),
                    "frequency_penalty": data.get("frequency_penalty"),
                }
            )
            cached_resp = get_cached_response(cache_key)
            if cached_resp:
                cache_hit = 1
                debug_log("info", f"[CACHE] hit model={model} key={cache_key[:12]}")
                cached_usage = cached_resp.get("usage") if isinstance(cached_resp, dict) else {}
                if not isinstance(cached_usage, dict):
                    cached_usage = {}
                response_time_ms = int((time_module.time() - start_time) * 1000)
                log_chat_usage(
                    virtual_model,
                    None,
                    endpoint_id,
                    cached_usage,
                    response_time_ms,
                    cache_attempted=1,
                    cache_hit=1,
                    inbound_api_key_id=inbound_api_key_id,
                    inbound_api_key_label=inbound_api_key_label,
                )
                response = JSONResponse(content=cached_resp)
                response.headers["X-Cache"] = "HIT"
                _log_chat_activity(200, "", stream_flag=False)
                return response
            else:
                debug_log("info", f"[CACHE] miss model={model} key={cache_key[:12]}")
        except Exception as e:
            debug_log("warn", f"[CACHE] lookup failed model={model}: {e}")

    # Call backend (handles both AI Queue and RunPod)
    if not stream and getattr(backend, "endpoint_type", "") == "openai_oauth":
        backend_result, error, status_code = await _call_backend_chat_via_stream_collect(
            backend=backend,
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            tools=tools,
            incoming_source=incoming_source,
        )
    else:
        backend_result, error, status_code = await backend.chat_completion(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stream=stream,
            tools=tools,
            **extra_params,
        )

    if error:
        response = JSONResponse(status_code=status_code, content=error)
        _log_chat_activity(
            int(status_code or 500),
            (error.get("error") if isinstance(error, dict) else str(error)),
            stream_flag=bool(stream),
        )
        return response

    # For RunPod backend, extract result from wrapper
    result = backend_result
    if "result" in backend_result:
        result = backend_result["result"]

    # Check if this is a streaming response from a virtual model backend
    if "_stream_data" in result and stream:
        stream_data = result["_stream_data"]
        parsed = _parse_chat_stream_payload(
            stream_data=stream_data,
            backend=backend,
            messages=messages,
            tools=tools,
            model=model,
            request_id=request_id,
        )
        if parsed["stream_error"]:
            response = JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": f"Upstream error: {parsed['stream_error']}",
                        "type": "upstream_error",
                    }
                },
            )
            _log_chat_activity(400, f"Upstream error: {parsed['stream_error']}", stream_flag=True)
            return response

        full_content = parsed["full_content"]
        full_reasoning = parsed["full_reasoning"]
        text_content = parsed["text_content"]
        extracted_tc = parsed["tool_calls"]
        usage = parsed["usage"]
        finish_reason = parsed["finish_reason"]
        stats = parsed["stats"]
        is_openai_oauth_backend = endpoint_type == "openai_oauth"

        # Check for empty response anomaly
        content_chars = len(full_content)
        reasoning_chars = len(full_reasoning)
        tc_count = len(extracted_tc) if extracted_tc else 0

        if (
            content_chars == 0
            and reasoning_chars == 0
            and tc_count == 0
            and finish_reason is None
        ):
            debug_log(
                "warn",
                f"EMPTY_STREAM_DETECTED: request_id={request_id}, raw_preview={stream_data[:200].replace(chr(10), '\\n')}",
            )
            # Save raw stream data for triage (only in full debug mode)
            if is_debug_mode() == "full":
                debug_log("info", f"RAW_STREAM_FULL: {stream_data[:2000]}")

        debug_log(
            "info",
            f"[STREAM_PARSE] request_id={request_id} stats={stats} content_ch={content_chars} reasoning_ch={reasoning_chars} tc={tc_count} finish={finish_reason}",
        )

        if endpoint_type == "openai_oauth" and is_debug_mode() == "full":
            if extracted_tc:
                debug_log(
                    "info",
                    f"[OAUTH_TOOL_PARSE] request_id={request_id} tc={len(extracted_tc)} details={_summarize_tool_calls_for_debug(extracted_tc)}",
                )
            if extracted_tc and finish_reason != "tool_calls":
                debug_log(
                    "warn",
                    f"[OAUTH_TOOL_MISMATCH] request_id={request_id} tc={len(extracted_tc)} finish={finish_reason} (tool calls parsed but stream did not end with tool_calls)",
                )

        print(
            f"[DEBUG] Parsed stream: content='{full_content[:50]}...' if full_content else '(empty)', reasoning='{full_reasoning[:50]}...' if full_reasoning else '(empty)', tc={tc_count}, finish={finish_reason}",
            flush=True,
        )

        # Handle reasoning content based on show_reasoning setting
        show_reasoning = True
        vm_config = get_virtual_model(model)
        if vm_config:
            show_reasoning = vm_config.get("show_reasoning", 1) == 1

        # When tool calls are present, don't add any content (reasoning or otherwise)
        # to avoid breaking the tool_calls flow. The client expects finish_reason=tool_calls
        if extracted_tc:
            text_content = None
        elif show_reasoning and full_reasoning:
            # Only add reasoning when NO tool calls
            if text_content:
                text_content = full_reasoning + "\n\n" + text_content
            else:
                text_content = full_reasoning

        # OpenAI OAuth streams frequently omit usage tokens. Keep upstream values when present,
        # and estimate only for OAuth when usage is missing.
        if is_openai_oauth_backend:
            prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            completion_tokens = int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            )
            total_tokens = int(usage.get("total_tokens") or 0)
            if total_tokens <= 0:
                total_tokens = prompt_tokens + completion_tokens

            if prompt_tokens <= 0 and completion_tokens <= 0 and total_tokens <= 0:
                usage = _estimate_openai_oauth_usage(
                    messages=messages,
                    tools=tools,
                    full_content=full_content,
                    full_reasoning=full_reasoning,
                    tool_calls=extracted_tc or [],
                )
                debug_log(
                    "info",
                    f"[OAUTH_USAGE_ESTIMATE] request_id={request_id} model={model} prompt={usage.get('prompt_tokens', 0)} completion={usage.get('completion_tokens', 0)} total={usage.get('total_tokens', 0)}",
                )
            else:
                usage["prompt_tokens"] = prompt_tokens
                usage["completion_tokens"] = completion_tokens
                usage["total_tokens"] = total_tokens

        # Generate proper SSE with extracted tool calls
        job_id = f"chat-{int(time_module.time())}"

        # Log usage for streaming requests BEFORE returning
        response_time_ms = int((time_module.time() - start_time) * 1000)
        log_chat_usage(
            model,
            None,
            None,
            usage,
            response_time_ms,
            cache_attempted=0,
            cache_hit=0,
            inbound_api_key_id=inbound_api_key_id,
            inbound_api_key_label=inbound_api_key_label,
        )

        async def stream_generator():
            async for chunk_data in _generate_sse(
                job_id=job_id,
                model=model,
                tool_calls_data=extracted_tc,
                text_content=text_content,
            ):
                yield chunk_data

        response = StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        _log_chat_activity(200, "", stream_flag=True)
        return response

    # Extract content from response
    content = ""
    reasoning_content = ""
    tool_calls_data = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    finish_reason = "stop"

    if "_stream_data" in result:
        parsed = _parse_chat_stream_payload(
            stream_data=result["_stream_data"],
            backend=backend,
            messages=messages,
            tools=tools,
            model=model,
            request_id=request_id,
        )
        if parsed["stream_error"]:
            response = JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": f"Upstream error: {parsed['stream_error']}",
                        "type": "upstream_error",
                    }
                },
            )
            _log_chat_activity(400, f"Upstream error: {parsed['stream_error']}", stream_flag=False)
            return response
        content = parsed["full_content"]
        reasoning_content = parsed["full_reasoning"]
        tool_calls_data = parsed["tool_calls"]
        usage = parsed["usage"]
        finish_reason = parsed["finish_reason"] or finish_reason
    elif "choices" in result and result["choices"]:
        choice = result["choices"][0]
        message = choice.get("message", {})
        content = message.get("content", "") or ""
        reasoning_content = message.get("reasoning_content", "") or ""
        tool_calls_data = message.get("tool_calls", []) or []
        usage = result.get("usage", usage)
        finish_reason = choice.get("finish_reason") or finish_reason

    # Handle reasoning content based on show_reasoning setting
    show_reasoning = True
    vm_config = get_virtual_model(model)
    if vm_config:
        show_reasoning = vm_config.get("show_reasoning", 1) == 1

    # Append reasoning to content if show_reasoning is enabled
    # Only add reasoning when there are NO tool calls (to avoid interfering with tool execution)
    if show_reasoning and reasoning_content:
        if content and not tool_calls_data:
            content = reasoning_content + "\n\n" + content
        elif not content and not tool_calls_data:
            # Only use reasoning as fallback when no tool calls
            content = reasoning_content
    elif not show_reasoning and reasoning_content and not content:
        # If reasoning is hidden but content is empty, use reasoning as fallback
        content = reasoning_content

    # Extract system_fingerprint if present (OA-4)
    system_fingerprint = result.get("system_fingerprint")

    # Skip all content processing for vision requests - prevents corrupting image data
    has_vision = any(
        isinstance(c, dict) and c.get("type") == "image_url"
        for msg in messages
        if isinstance(msg, dict)
        for c in (msg.get("content") or [])
    )
    is_openai_oauth_backend = (
        hasattr(backend, "endpoint_type") and backend.endpoint_type == "openai_oauth"
    )
    advertised_tool_names = _advertised_tool_names(tools)

    if has_vision:
        debug_log(
            "info",
            f"[BYPASS_PROCESS] request_id={request_id} skipping process_content for vision request (non-stream)",
        )
        text_content = content
        extracted_tc = []
    elif is_openai_oauth_backend:
        # For OAuth-backed responses, do not infer tool calls from text.
        text_content = content
        extracted_tc = []
    else:
        # Process content to extract tool calls
        extracted_tc, text_content = process_content(content)
        if extracted_tc:
            tool_calls_data = extracted_tc
        elif not tool_calls_data:
            text_content = text_content or content

    if tool_calls_data:
        filtered_tc, dropped_tc = _filter_tool_calls_by_advertised(
            tool_calls_data, advertised_tool_names
        )
        if dropped_tc:
            dropped_names = []
            for tc in dropped_tc:
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                dropped_names.append(str(fn.get("name") or "").strip() or "<empty>")
            debug_log(
                "warn",
                f"[TOOL_FILTER] request_id={request_id} dropped={len(dropped_tc)} names={','.join(dropped_names[:8])}",
            )
        tool_calls_data = filtered_tc

    job_id = result.get("id", f"chat-{int(time_module.time())}")

    # Handle streaming response - use stream (actual value used) not original_stream
    if stream:
        response = StreamingResponse(
            _generate_sse(
                job_id=job_id,
                model=model,
                tool_calls_data=tool_calls_data,
                text_content=text_content,
                system_fingerprint=system_fingerprint,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        _log_chat_activity(200, "", stream_flag=True)
        return response

    # Log usage for both streaming and non-streaming
    response_time_ms = int((time_module.time() - start_time) * 1000)
    endpoint_name = None
    endpoint_id = None
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT name, id FROM virtual_models WHERE name = ?
            """,
                (virtual_model,),
            )
            row = cursor.fetchone()
            if row:
                endpoint_name = row[0] if row[0] else virtual_model
                cursor.execute(
                    """
                    SELECT endpoint_id FROM virtual_models WHERE name = ?
                """,
                    (virtual_model,),
                )
                endpoint_id = cursor.fetchone()[0]
    except:
        pass
    log_chat_usage(
        virtual_model,
        endpoint_name,
        endpoint_id,
        usage,
        response_time_ms,
        cache_attempted=cache_attempted,
        cache_hit=cache_hit,
        inbound_api_key_id=inbound_api_key_id,
        inbound_api_key_label=inbound_api_key_label,
    )

    # Build non-streaming response
    response_content = {
        "id": job_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": usage,
    }

    if system_fingerprint:
        response_content["system_fingerprint"] = system_fingerprint

    normalized_tool_calls = []
    for idx, tc in enumerate(tool_calls_data or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        tc_name = str(fn.get("name") or "").strip()
        if not tc_name:
            continue
        tc_id = str(tc.get("id") or f"call_{int(time.time() * 1000)}_{idx}").strip()
        tc_args = fn.get("arguments")
        if isinstance(tc_args, (dict, list)):
            tc_args = json.dumps(tc_args)
        elif tc_args is None:
            tc_args = "{}"
        else:
            tc_args = str(tc_args)
            if not tc_args.strip():
                tc_args = "{}"
        normalized_tool_calls.append(
            {
                "id": tc_id,
                "type": "function",
                "function": {"name": tc_name, "arguments": tc_args},
            }
        )

    finish_reason = "tool_calls" if (normalized_tool_calls and not text_content) else (finish_reason or "stop")

    choice = {
        "index": 0,
        "message": {
            "role": "assistant",
            "content": text_content,
        },
        "finish_reason": finish_reason,
    }
    if normalized_tool_calls:
        choice["message"]["tool_calls"] = normalized_tool_calls
    response_content["choices"].append(choice)

    # Store non-stream cache after successful response when safe
    if (
        not stream
        and cache_attempted == 1
        and cache_hit == 0
        and cache_key
        and is_virtual_model_cache_enabled(model)
    ):
        has_tools_resp = bool(tool_calls_data)
        if not has_tools_resp:
            ttl = int(get_setting("cache_ttl_chat", "300") or 300)
            try:
                store_cached_response(cache_key, response_content, ttl, virtual_model)
                debug_log(
                    "info",
                    f"[CACHE] store model={model} key={cache_key[:12]} ttl={ttl}",
                )
            except Exception as e:
                debug_log("warn", f"[CACHE] store failed model={model}: {e}")

    response = JSONResponse(content=response_content)
    _log_chat_activity(200, "", stream_flag=False)
    return response


@app.post("/chat/completions")
async def chat_completions_alias(request: Request):
    """Compatibility alias for OpenAI-style clients missing /v1 prefix."""
    return await chat_completions(request)


@app.post("/api/chat/completions")
async def chat_completions_api_alias(request: Request):
    """Compatibility alias used by some OpenWebUI/OpenAI clients."""
    return await chat_completions(request)


async def _generate_sse(
    job_id, model, tool_calls_data, text_content, system_fingerprint=None
):
    """Generate SSE stream for streaming responses."""
    created = int(time.time())
    chunk_id = job_id

    # Include system_fingerprint in first chunk if available (OA-4)
    first_chunk_meta = {}
    if system_fingerprint:
        first_chunk_meta["system_fingerprint"] = system_fingerprint

    def _normalize_tool_calls(raw_tool_calls):
        normalized = []
        for idx, tc in enumerate(raw_tool_calls or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            tc_name = str(fn.get("name") or "").strip()
            if not tc_name:
                continue
            tc_id = str(tc.get("id") or f"call_{int(time.time() * 1000)}_{idx}").strip()
            tc_args = fn.get("arguments")
            if isinstance(tc_args, (dict, list)):
                tc_args = json.dumps(tc_args)
            elif tc_args is None:
                tc_args = "{}"
            else:
                tc_args = str(tc_args)
                if not tc_args.strip():
                    tc_args = "{}"
            normalized.append(
                {
                    "id": tc_id,
                    "type": "function",
                    "function": {"name": tc_name, "arguments": tc_args},
                }
            )
        return normalized

    normalized_tool_calls = _normalize_tool_calls(tool_calls_data)

    if normalized_tool_calls:
        for tc_index, tc in enumerate(normalized_tool_calls):
            chunk = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": tc_index,
                                    "id": tc.get("id", f"call_{tc_index}"),
                                    "type": "function",
                                    "function": {
                                        "name": tc.get("function", {}).get("name", ""),
                                        "arguments": "",
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            await asyncio.sleep(0.01)

            args = tc.get("function", {}).get("arguments", "")
            chunk = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": tc_index,
                                    "id": tc.get("id", f"call_{tc_index}"),
                                    "function": {"arguments": args},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            await asyncio.sleep(0.01)

    if text_content:
        chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": text_content},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(chunk)}\n\n"

    # Determine finish_reason: "tool_calls" only if tool calls present AND no text content
    finish_reason = "tool_calls" if (normalized_tool_calls and not text_content) else "stop"

    final_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": finish_reason,
            }
        ],
    }
    yield f"data: {json.dumps(final_chunk)}\n\n"
    yield "data: [DONE]\n\n"


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    # Check database for use_ai_queue setting
    use_queue = (
        get_setting("use_ai_queue", os.getenv("USE_AI_QUEUE", "false")) == "true"
    )
    backend = AIQueueBackend() if use_queue else RunPodBackend()
    backend_healthy = await backend.health_check()
    return {
        "status": "healthy" if backend_healthy else "unhealthy",
        "backend": type(backend).__name__,
        "timestamp": int(time.time()),
    }


# ==================================================================================
# Usage Tracking API Endpoints
# ==================================================================================


@app.get("/api/admin/activity")
async def get_activity_feed(request: Request):
    """Get recent request activity feed for admin dashboard."""
    auth = validate_session_fastapi(request)
    if not auth.get("valid"):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    qp = request.query_params
    try:
        limit = int(qp.get("limit", "100") or 100)
    except Exception:
        limit = 100
    status_filter = str(qp.get("status", "") or "").strip().lower()
    model_filter = str(qp.get("model", "") or "").strip()
    path_filter = str(qp.get("path", "") or "").strip()
    ip_filter = str(qp.get("ip", "") or "").strip()
    inbound_api_key_id_raw = str(qp.get("inbound_api_key_id", "") or "").strip()
    try:
        inbound_api_key_id = int(inbound_api_key_id_raw) if inbound_api_key_id_raw else None
    except Exception:
        inbound_api_key_id = None
    try:
        since = int(qp.get("since", "0") or 0)
    except Exception:
        since = 0
    include_health = str(qp.get("include_health", "false") or "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    try:
        sql, params = _build_activity_query(
            limit=limit,
            status_filter=status_filter,
            model_filter=model_filter,
            path_filter=path_filter,
            ip_filter=ip_filter,
            inbound_api_key_id=inbound_api_key_id,
            since=since,
            include_health=include_health,
        )
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = [dict(row) for row in cursor.fetchall()]
        return JSONResponse(content={"items": rows, "count": len(rows)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/admin/health")
async def get_endpoint_health(request: Request):
    """Return endpoint health status for admin UI."""
    auth = validate_session_fastapi(request)
    if not auth.get("valid"):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT e.id, e.name, e.endpoint_type,
                       eh.status, eh.failure_count, eh.last_error, eh.last_failure_at,
                       eh.circuit_until, eh.rate_limit_info
                FROM endpoints e
                LEFT JOIN endpoint_health eh ON eh.endpoint_id = e.id
                WHERE e.enabled = 1
                ORDER BY e.name ASC
                """
            )
            rows = [dict(r) for r in cursor.fetchall()]
        return JSONResponse(content={"endpoints": rows})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/admin/usage")
async def get_usage_summary(request: Request):
    """Get usage summary with date filtering."""
    auth = validate_session_fastapi(request)
    if not auth.get("valid"):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    query_params = request.query_params
    start_date = query_params.get("start_date")
    end_date = query_params.get("end_date")
    virtual_model = query_params.get("virtual_model")
    inbound_api_key_id_raw = str(query_params.get("inbound_api_key_id") or "").strip()
    try:
        inbound_api_key_id = int(inbound_api_key_id_raw) if inbound_api_key_id_raw else None
    except Exception:
        inbound_api_key_id = None

    # Default to last 24 hours if no dates specified
    if not start_date:
        start_date = str(int(time.time()) - 86400)
    if not end_date:
        end_date = str(int(time.time()))

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Build base query
            base_query = """
                SELECT 
                    SUM(prompt_tokens) as total_prompt_tokens,
                    SUM(completion_tokens) as total_completion_tokens,
                    SUM(total_tokens) as total_tokens,
                    SUM(cost_estimate) as total_cost,
                    SUM(cached_input_tokens) as total_cached_input_tokens,
                    SUM(cache_creation_tokens) as total_cache_creation_tokens,
                    SUM(cached_cost_estimate) as total_cached_cost,
                    COUNT(*) as request_count,
                    AVG(response_time_ms) as avg_response_time_ms,
                    SUM(cache_attempted) as cache_attempts,
                    SUM(cache_hit) as cache_hits,
                    SUM(CASE WHEN cache_hit = 1 THEN cost_estimate ELSE 0 END) as cache_savings
                FROM request_usage
                WHERE created_at >= ? AND created_at <= ?
            """
            params = [start_date, end_date]

            # Add virtual model filter if specified
            if virtual_model:
                base_query += " AND virtual_model = ?"
                params.append(virtual_model)
            if inbound_api_key_id is not None:
                base_query += " AND inbound_api_key_id = ?"
                params.append(inbound_api_key_id)

            cursor.execute(base_query, params)
            summary = cursor.fetchone()

            # Daily breakdown
            daily_query = """
                SELECT 
                    strftime('%Y-%m-%d', datetime(created_at, 'unixepoch', '-5 hours')) as date,
                    SUM(prompt_tokens) as prompt_tokens,
                    SUM(completion_tokens) as completion_tokens,
                    SUM(cached_input_tokens) as cached_input_tokens,
                    SUM(cache_creation_tokens) as cache_creation_tokens,
                    SUM(cached_cost_estimate) as cached_cost_estimate,
                    SUM(cost_estimate) as cost_estimate,
                    COUNT(*) as requests,
                    SUM(cache_attempted) as cache_attempts,
                    SUM(cache_hit) as cache_hits,
                    SUM(CASE WHEN cache_hit = 1 THEN cost_estimate ELSE 0 END) as cache_savings
                FROM request_usage
                WHERE created_at >= ? AND created_at <= ?
            """
            daily_params = [start_date, end_date]

            if virtual_model:
                daily_query += " AND virtual_model = ?"
                daily_params.append(virtual_model)
            if inbound_api_key_id is not None:
                daily_query += " AND inbound_api_key_id = ?"
                daily_params.append(inbound_api_key_id)

            daily_query += " GROUP BY date ORDER BY date DESC"
            cursor.execute(daily_query, daily_params)

            daily_model_query = """
                SELECT
                    strftime('%Y-%m-%d', datetime(created_at, 'unixepoch', '-5 hours')) as date,
                    virtual_model,
                    SUM(prompt_tokens) as prompt_tokens,
                    SUM(completion_tokens) as completion_tokens,
                    SUM(cached_input_tokens) as cached_input_tokens,
                    SUM(cache_creation_tokens) as cache_creation_tokens,
                    SUM(cached_cost_estimate) as cached_cost_estimate,
                    SUM(cost_estimate) as cost_estimate,
                    COUNT(*) as requests,
                    SUM(cache_attempted) as cache_attempts,
                    SUM(cache_hit) as cache_hits,
                    SUM(CASE WHEN cache_hit = 1 THEN cost_estimate ELSE 0 END) as cache_savings
                FROM request_usage
                WHERE created_at >= ? AND created_at <= ?
            """
            daily_model_params = [start_date, end_date]

            if virtual_model:
                daily_model_query += " AND virtual_model = ?"
                daily_model_params.append(virtual_model)
            if inbound_api_key_id is not None:
                daily_model_query += " AND inbound_api_key_id = ?"
                daily_model_params.append(inbound_api_key_id)

            daily_model_query += (
                " GROUP BY date, virtual_model "
                "ORDER BY date DESC, cost_estimate DESC, virtual_model ASC"
            )
            cursor.execute(daily_model_query, daily_model_params)
            models_by_date = {}
            for mrow in cursor.fetchall():
                date_key = mrow[0]
                models_by_date.setdefault(date_key, []).append(
                    {
                        "virtual_model": mrow[1] or "(unknown)",
                        "prompt_tokens": mrow[2] or 0,
                        "completion_tokens": mrow[3] or 0,
                        "cached_input_tokens": mrow[4] or 0,
                        "cache_creation_tokens": mrow[5] or 0,
                        "cached_cost_estimate": round(mrow[6] or 0, 4),
                        "cost_estimate": round(mrow[7] or 0, 4),
                        "requests": mrow[8] or 0,
                        "cache_attempts": mrow[9] or 0,
                        "cache_hits": mrow[10] or 0,
                        "cache_savings": round(mrow[11] or 0, 4),
                    }
                )

            cursor.execute(daily_query, daily_params)
            daily_breakdown = []
            for row in cursor.fetchall():
                daily_breakdown.append(
                    {
                        "date": row[0],
                        "prompt_tokens": row[1] or 0,
                        "completion_tokens": row[2] or 0,
                        "cached_input_tokens": row[3] or 0,
                        "cache_creation_tokens": row[4] or 0,
                        "cached_cost_estimate": round(row[5] or 0, 4),
                        "cost_estimate": round(row[6] or 0, 4),
                        "requests": row[7] or 0,
                        "cache_attempts": row[8] or 0,
                        "cache_hits": row[9] or 0,
                        "cache_savings": round(row[10] or 0, 4),
                        "models": models_by_date.get(row[0], []),
                    }
                )

            estimated_models_query = """
                SELECT DISTINCT ru.virtual_model
                FROM request_usage ru
                JOIN virtual_models vm ON vm.name = ru.virtual_model
                JOIN endpoints e ON e.id = vm.endpoint_id
                WHERE ru.created_at >= ? AND ru.created_at <= ?
                  AND e.endpoint_type = 'openai_oauth'
            """
            estimated_params = [start_date, end_date]
            if virtual_model:
                estimated_models_query += " AND ru.virtual_model = ?"
                estimated_params.append(virtual_model)
            if inbound_api_key_id is not None:
                estimated_models_query += " AND ru.inbound_api_key_id = ?"
                estimated_params.append(inbound_api_key_id)
            estimated_models_query += " ORDER BY ru.virtual_model"
            cursor.execute(estimated_models_query, estimated_params)
            estimated_models = [
                r[0] for r in cursor.fetchall() if r and isinstance(r[0], str) and r[0].strip()
            ]

            return JSONResponse(
                content={
                    "summary": {
                        "total_prompt_tokens": summary[0] or 0,
                        "total_completion_tokens": summary[1] or 0,
                        "total_tokens": summary[2] or 0,
                        "total_cost": round(summary[3] or 0, 4),
                        "total_cached_input_tokens": summary[4] or 0,
                        "total_cache_creation_tokens": summary[5] or 0,
                        "total_cached_cost": round(summary[6] or 0, 4),
                        "request_count": summary[7] or 0,
                        "avg_response_time_ms": round(summary[8] or 0, 2),
                        "cache_attempts": summary[9] or 0,
                        "cache_hits": summary[10] or 0,
                        "cache_hit_rate": round(
                            (float(summary[10] or 0) / float(summary[9] or 1))
                            if (summary[9] or 0) > 0
                            else 0,
                            4,
                        ),
                        "cache_savings": round(summary[11] or 0, 4),
                    },
                    "daily_breakdown": daily_breakdown,
                    "estimated_models": estimated_models,
                }
            )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/admin/usage/by_model")
async def get_usage_by_model(request: Request):
    """Get usage breakdown by virtual model."""
    auth = validate_session_fastapi(request)
    if not auth.get("valid"):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    query_params = request.query_params
    start_date = query_params.get("start_date")
    end_date = query_params.get("end_date")
    inbound_api_key_id_raw = str(query_params.get("inbound_api_key_id") or "").strip()
    try:
        inbound_api_key_id = int(inbound_api_key_id_raw) if inbound_api_key_id_raw else None
    except Exception:
        inbound_api_key_id = None

    if not start_date:
        start_date = str(int(time.time()) - 86400)
    if not end_date:
        end_date = str(int(time.time()))

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            query = """
                SELECT 
                    virtual_model,
                    SUM(prompt_tokens) as prompt_tokens,
                    SUM(completion_tokens) as completion_tokens,
                    SUM(total_tokens) as total_tokens,
                    SUM(cost_estimate) as cost_estimate,
                    SUM(cached_input_tokens) as cached_input_tokens,
                    SUM(cache_creation_tokens) as cache_creation_tokens,
                    SUM(cached_cost_estimate) as cached_cost_estimate,
                    COUNT(*) as request_count
                FROM request_usage
                WHERE created_at >= ? AND created_at <= ?
            """
            params = [start_date, end_date]
            if inbound_api_key_id is not None:
                query += " AND inbound_api_key_id = ?"
                params.append(inbound_api_key_id)
            query += " GROUP BY virtual_model ORDER BY total_tokens DESC"
            cursor.execute(query, params)

            results = [dict(row) for row in cursor.fetchall()]
            return JSONResponse(content=results)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/admin/usage/by_endpoint")
async def get_usage_by_endpoint(request: Request):
    """Get usage breakdown by endpoint."""
    auth = validate_session_fastapi(request)
    if not auth.get("valid"):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    query_params = request.query_params
    start_date = query_params.get("start_date")
    end_date = query_params.get("end_date")
    inbound_api_key_id_raw = str(query_params.get("inbound_api_key_id") or "").strip()
    try:
        inbound_api_key_id = int(inbound_api_key_id_raw) if inbound_api_key_id_raw else None
    except Exception:
        inbound_api_key_id = None

    if not start_date:
        start_date = str(int(time.time()) - 86400)
    if not end_date:
        end_date = str(int(time.time()))

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            query = """
                SELECT 
                    endpoint_name,
                    SUM(prompt_tokens) as prompt_tokens,
                    SUM(completion_tokens) as completion_tokens,
                    SUM(total_tokens) as total_tokens,
                    COUNT(*) as request_count
                FROM request_usage
                WHERE created_at >= ? AND created_at <= ? AND endpoint_name IS NOT NULL
            """
            params = [start_date, end_date]
            if inbound_api_key_id is not None:
                query += " AND inbound_api_key_id = ?"
                params.append(inbound_api_key_id)
            query += " GROUP BY endpoint_name ORDER BY total_tokens DESC"
            cursor.execute(query, params)

            results = [dict(row) for row in cursor.fetchall()]
            return JSONResponse(content=results)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/admin/usage/export")
async def export_usage_csv(request: Request):
    """Export usage data as CSV."""
    auth = validate_session_fastapi(request)
    if not auth.get("valid"):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    data = await request.json()
    start_date = data.get("start_date")
    end_date = data.get("end_date")
    virtual_model = data.get("virtual_model")
    inbound_api_key_id_raw = str(data.get("inbound_api_key_id") or "").strip()
    try:
        inbound_api_key_id = int(inbound_api_key_id_raw) if inbound_api_key_id_raw else None
    except Exception:
        inbound_api_key_id = None

    if not start_date:
        start_date = str(int(time.time()) - 86400)
    if not end_date:
        end_date = str(int(time.time()))

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            base_query = """
                SELECT 
                    created_at,
                    virtual_model,
                    COALESCE(endpoint_name, 'N/A') as endpoint,
                    request_type,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    cost_estimate,
                    response_time_ms
                FROM request_usage
                WHERE created_at >= ? AND created_at <= ?
            """
            params = [start_date, end_date]

            if virtual_model:
                base_query += " AND virtual_model = ?"
                params.append(virtual_model)
            if inbound_api_key_id is not None:
                base_query += " AND inbound_api_key_id = ?"
                params.append(inbound_api_key_id)

            base_query += " ORDER BY created_at DESC"

            cursor.execute(base_query, params)
            rows = cursor.fetchall()

            # Build CSV
            csv_lines = [
                "Date,Virtual Model,Endpoint,Request Type,Prompt Tokens,Completion Tokens,Total Tokens,Cost ($),Response Time (ms)"
            ]
            for row in rows:
                csv_lines.append(
                    f"{_eastern_datetime_string(row[0])},{row[1]},{row[2]},{row[3]},{row[4]},{row[5]},{row[6]},{row[7]:.4f},{row[8]}"
                )

            csv_content = "\n".join(csv_lines)
            return Response(
                content=csv_content,
                media_type="text/csv",
                headers={
                    "Content-Disposition": f"attachment; filename=usage_report_{int(time.time())}.csv"
                },
            )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.put("/virtual-models/<int:vm_id>/update_cost")
async def update_virtual_model_cost(vm_id: int, request: Request):
    """Update cost per 1M tokens for a virtual model."""
    auth = validate_session_fastapi(request)
    if not auth.get("valid"):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    try:
        data = await request.json()
        cost_in = data.get("cost_per_1m_tokens_in", 0)
        cost_out = data.get("cost_per_1m_tokens_out", 0)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE virtual_models
                SET cost_per_1m_tokens_in = ?, cost_per_1m_tokens_out = ?, updated_at = strftime('%s', 'now')
                WHERE id = ?
            """,
                (cost_in, cost_out, vm_id),
            )
            conn.commit()

            cursor.execute(
                """
                SELECT * FROM virtual_models WHERE id = ?
            """,
                (vm_id,),
            )
            result = cursor.fetchone()

            return JSONResponse(content=dict(result))
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/admin/usage/embeddings")
async def get_embedding_usage(request: Request):
    """Get embedding usage summary."""
    auth = validate_session_fastapi(request)
    if not auth.get("valid"):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    query_params = request.query_params
    start_date = query_params.get("start_date")
    end_date = query_params.get("end_date")
    inbound_api_key_id_raw = str(query_params.get("inbound_api_key_id") or "").strip()
    try:
        inbound_api_key_id = int(inbound_api_key_id_raw) if inbound_api_key_id_raw else None
    except Exception:
        inbound_api_key_id = None

    if not start_date:
        start_date = str(int(time.time()) - 86400)
    if not end_date:
        end_date = str(int(time.time()))

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            query = """
                SELECT 
                    SUM(input_tokens) as total_input_tokens,
                    SUM(output_tokens) as total_output_tokens,
                    SUM(total_tokens) as total_tokens,
                    SUM(cost_estimate) as total_cost,
                    COUNT(*) as request_count
                FROM embedding_usage
                WHERE created_at >= ? AND created_at <= ?
            """
            params = [start_date, end_date]
            if inbound_api_key_id is not None:
                query += " AND inbound_api_key_id = ?"
                params.append(inbound_api_key_id)
            cursor.execute(query, params)

            summary = cursor.fetchone()
            return JSONResponse(
                content={
                    "total_input_tokens": summary[0] or 0,
                    "total_output_tokens": summary[1] or 0,
                    "total_tokens": summary[2] or 0,
                    "total_cost": round(summary[3] or 0, 4),
                    "request_count": summary[4] or 0,
                }
            )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.head("/v1")
async def head_v1():
    """Anthropic API health check."""
    return JSONResponse(content={})


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    """Anthropic API compatible /v1/messages endpoint."""
    auth_response = enforce_runtime_access(request)
    if auth_response is not None:
        return auth_response
    import json as json_module
    import time as time_module

    start_time = time_module.time()
    incoming_source = request.headers.get("x-source", "serverless-proxy")
    data = await request.json()

    model = data.get("model")
    messages = data.get("messages", [])
    max_tokens = data.get("max_tokens", 1024)
    temperature = data.get("temperature", 0.7)
    top_p = data.get("top_p", 1.0)
    stream = data.get("stream", False)
    tools = data.get("tools", [])

    # Convert Claude Code tool format to OpenAI format
    # Claude Code sends: {"name": "...", "description": "...", "parameters": {...}}
    # OpenAI expects: {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    if tools:
        converted_tools = []
        for tool in tools:
            if "function" in tool:
                converted_tools.append(tool)
            elif "name" in tool:
                converted_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.get("name"),
                            "description": tool.get("description", ""),
                            "parameters": tool.get(
                                "parameters", {"type": "object", "properties": {}}
                            ),
                        },
                    }
                )
        tools = converted_tools

    # Handle system at top level (Anthropic newer format)
    system_message = data.get("system")
    if not system_message and messages and messages[0].get("role") == "system":
        # Fallback to first message if it's system
        pass  # Will be handled in loop below

    # Convert Anthropic system messages to OpenAI format
    if not system_message:
        system_message = None
    converted_messages = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "system":
            if isinstance(content, str):
                system_message = content
            elif isinstance(content, list):
                system_message = "".join(
                    [c.get("text", "") for c in content if c.get("type") == "text"]
                )
        elif role == "assistant":
            # Handle tool_use in content
            if isinstance(content, list):
                # Check for tool_use blocks
                tool_calls = []
                text_content = ""
                for block in content:
                    if block.get("type") == "tool_use":
                        tool_calls.append(
                            {
                                "id": block.get(
                                    "id", f"call_{int(time_module.time() * 1000)}"
                                ),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": json_module.dumps(
                                        block.get("input", {})
                                    ),
                                },
                            }
                        )
                    elif block.get("type") == "text":
                        text_content += block.get("text", "")

                if tool_calls:
                    converted_messages.append(
                        {
                            "role": "assistant",
                            "content": text_content,
                            "tool_calls": tool_calls,
                        }
                    )
                else:
                    converted_messages.append(
                        {"role": "assistant", "content": text_content}
                    )
            else:
                converted_messages.append(
                    {"role": "assistant", "content": str(content) if content else ""}
                )
        elif role == "user":
            # Handle tool_result and image_url in content
            if isinstance(content, list):
                user_content_blocks = []
                for block in content:
                    if not isinstance(block, dict):
                        user_content_blocks.append({"type": "text", "text": str(block)})
                        continue

                    if block.get("type") == "tool_result":
                        converted_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": block.get("tool_use_id", ""),
                                "content": block.get("content", ""),
                            }
                        )
                    elif block.get("type") == "text":
                        user_content_blocks.append(
                            {"type": "text", "text": block.get("text", "")}
                        )
                    else:
                        # Preserve any non-tool_result block as-is (image_url, image, input_image, etc.)
                        user_content_blocks.append(block)

                # If we have image blocks, send as list
                if user_content_blocks:
                    converted_messages.append(
                        {"role": "user", "content": user_content_blocks}
                    )
            else:
                converted_messages.append(
                    {"role": "user", "content": str(content) if content else ""}
                )
        else:
            converted_messages.append(
                {"role": role, "content": str(content) if content else ""}
            )

    if system_message:
        converted_messages.insert(0, {"role": "system", "content": system_message})

    # Get backend
    backend = get_backend(model)
    if backend is None:
        return model_not_found_response(model)
    virtual_model = model
    if hasattr(backend, "virtual_model_name"):
        virtual_model = backend.virtual_model_name

    # Build request for chat/completions - use actual model from backend
    actual_model = backend.model if hasattr(backend, "model") else model
    effective_stream = bool(stream)
    if not effective_stream and getattr(backend, "endpoint_type", "") == "openai_oauth":
        effective_stream = True

    chat_data = {
        "model": actual_model,
        "messages": converted_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": top_p,
        "stream": effective_stream,
    }
    if tools:
        chat_data["tools"] = tools

    if hasattr(backend, "endpoint_type") and backend.endpoint_type == "openai_oauth":
        chat_data = _openai_chat_to_openai_oauth_payload(
            messages=converted_messages,
            model=actual_model,
            stream=effective_stream,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            tools=tools,
            tool_choice=None,
            parallel_tool_calls=None,
        )

    # Check for streaming
    if effective_stream and stream:
        # Handle streaming - similar to chat_completions but return SSE
        headers = {"Content-Type": "application/json"}
        auth_header = await resolve_endpoint_auth_header_async(
            {
                "id": getattr(backend, "endpoint_id", None),
                "api_key": getattr(backend, "api_key", ""),
                "oauth_enabled": getattr(backend, "oauth_enabled", 0),
                "oauth_grant_type": getattr(backend, "oauth_grant_type", ""),
                "oauth_token_url": getattr(backend, "oauth_token_url", ""),
                "oauth_client_id": getattr(backend, "oauth_client_id", ""),
                "oauth_client_secret": getattr(backend, "oauth_client_secret", ""),
                "oauth_scope": getattr(backend, "oauth_scope", ""),
                "oauth_refresh_token": getattr(backend, "oauth_refresh_token", ""),
                "oauth_token_expires_at": getattr(backend, "oauth_token_expires_at", 0),
                "oauth_token_request_format": getattr(
                    backend, "oauth_token_request_format", "json"
                ),
                "oauth_client_auth_method": getattr(
                    backend, "oauth_client_auth_method", "client_secret_post"
                ),
            }
        )
        if auth_header:
            headers["Authorization"] = auth_header

        # Add tracking headers - use incoming source
        headers["X-Source"] = incoming_source
        headers["X-Model"] = actual_model
        headers["X-Priority"] = "NORMAL"

        # Get endpoint URL
        endpoint = _backend_effective_url(backend)
        if hasattr(backend, "endpoint_type"):
            if backend.endpoint_type == "deepinfra":
                endpoint = f"{endpoint}/v1/openai/chat/completions"
            elif backend.endpoint_type == "openai_oauth":
                endpoint = _resolve_openai_oauth_response_endpoint(endpoint)
            elif backend.endpoint_type == "openwebui":
                endpoint = f"{endpoint}/api/chat/completions"
            elif backend.endpoint_type == "queue":
                endpoint = f"{endpoint}/v1/chat/completions"
            else:
                endpoint = f"{endpoint}/v1/chat/completions"
        else:
            endpoint = f"{endpoint}/v1/chat/completions"

        async def stream_generator():
            import httpx
            import json as json_module

            message_id = f"msg_{int(time_module.time() * 1000)}"
            sent_message_start = False

            async with httpx.AsyncClient(timeout=1200.0) as client:
                async with client.stream(
                    "POST", endpoint, headers=headers, json=chat_data
                ) as resp:
                    async for line in resp.aiter_lines():
                        if line.strip() and line.startswith("data: "):
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                yield f'data: {{"type":"message_delta","delta":{{"stop_reason":"end_turn","type":"message_delta"}},"usage":{{"output_tokens":0}}}}\n\n'
                                yield "data: [DONE]\n\n"
                                continue
                            try:
                                chunk = json_module.loads(data_str)
                                content = ""
                                if hasattr(backend, "endpoint_type") and backend.endpoint_type == "openai_oauth":
                                    evt_type = str(chunk.get("type") or "").lower()
                                    if evt_type in ("response.output_text.delta", "response.output.delta", "output_text.delta"):
                                        content = str(chunk.get("delta") or chunk.get("text") or "")
                                else:
                                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                                    content = delta.get("content")

                                if content:
                                    if not sent_message_start:
                                        # Send message_start first
                                        yield f'data: {{"type":"message_start","message":{{"id":"{message_id}","type":"message","role":"assistant","content":[{{"type":"text","text":""}}],"model":"{model}","stop_reason":null,"stop_sequence":null,"usage":{{"input_tokens":0,"output_tokens":0}}}}}}\n\n'
                                        sent_message_start = True

                                    # Escape content for JSON
                                    content_escaped = (
                                        content.replace("\\", "\\\\")
                                        .replace('"', '\\"')
                                        .replace("\n", "\\n")
                                        .replace("\r", "\\r")
                                        .replace("\t", "\\t")
                                    )
                                    yield f'data: {{"type":"content_block_delta","delta":{{"type":"text_delta","text":"{content_escaped}"}},"index":0}}\n\n'
                            except Exception as e:
                                pass
                    yield "data: [DONE]\n\n"

        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # Non-streaming
    if not stream and getattr(backend, "endpoint_type", "") == "openai_oauth":
        backend_result, error, status_code = await _call_backend_chat_via_stream_collect(
            backend=backend,
            model=model,
            messages=converted_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            tools=tools,
            incoming_source=incoming_source,
        )
    else:
        backend_result, error, status_code = await backend.chat_completion(
            messages=converted_messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stream=False,
            tools=tools,
        )

    if error:
        return JSONResponse(status_code=status_code, content=error)

    result = backend_result
    if "result" in backend_result:
        result = backend_result["result"]

    # Convert OpenAI format back to Anthropic format
    content = ""
    usage = {}
    finish_reason = "stop"
    content_blocks = []
    tool_calls_data = []

    if "_stream_data" in result:
        parsed = _parse_chat_stream_payload(
            stream_data=result["_stream_data"],
            backend=backend,
            messages=converted_messages,
            tools=tools,
            model=model,
            request_id="anthropic_nonstream",
        )
        content = parsed["text_content"] or parsed["full_content"] or ""
        usage = parsed["usage"] or {}
        tool_calls_data = parsed["tool_calls"] or []
        finish_reason = parsed["finish_reason"] or finish_reason
    elif "choices" in result and result["choices"]:
        choice = result["choices"][0]
        message = choice.get("message", {})
        content = message.get("content", "") or ""
        tool_calls_data = message.get("tool_calls", []) or []
        finish_reason = choice.get("finish_reason") or finish_reason
        usage = result.get("usage", {}) or usage

    # Convert text content
    if content:
        content_blocks.append({"type": "text", "text": content})

    # Convert tool calls to Anthropic format
    if finish_reason in ("tool_calls",):
        stop_reason = "tool_use"
    elif finish_reason in ("length",):
        stop_reason = "max_tokens"
    elif finish_reason in ("stop", "end_turn", None, ""):
        stop_reason = "end_turn"
    else:
        stop_reason = str(finish_reason)

    for tc in tool_calls_data:
        func = tc.get("function", {})
        args = func.get("arguments", {})
        if isinstance(args, str):
            import json as json_module

            try:
                args = json_module.loads(args)
            except:
                args = {"raw": args}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id", f"toolu_{int(time_module.time() * 1000)}"),
                "name": func.get("name", ""),
                "input": args,
            }
        )
        stop_reason = "tool_use"

    # Build Anthropic response
    response = {
        "id": result.get("id", f"msg_{int(time_module.time() * 1000)}"),
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)),
            "output_tokens": usage.get(
                "completion_tokens", usage.get("output_tokens", 0)
            ),
        },
    }

    return JSONResponse(content=response)


@app.get("/v1/models")
async def list_models(request: Request):
    auth_response = enforce_runtime_access(request)
    if auth_response is not None:
        return auth_response
    started_at = time_module.time()
    client_ip, forwarded_for = _resolve_activity_client_ip(request)
    x_source = (request.headers.get("x-source") or "").strip()
    user_agent = (request.headers.get("user-agent") or "").strip()

    virtual_models = get_enabled_virtual_models()

    # Build response with virtual models only
    models = []

    # Add virtual models
    for vm in virtual_models:
        models.append(
            {
                "id": vm["name"],
                "object": "model",
                "created": int(time.time()),
                "owned_by": vm.get("endpoint_name", "configured"),
            }
        )

    response = {
        "object": "list",
        "data": models,
    }

    log_recent_activity(
        request_id="",
        method=request.method,
        path=request.url.path,
        request_type="models",
        virtual_model="",
        actual_model="",
        endpoint_name="",
        endpoint_id=None,
        endpoint_type="",
        client_ip=client_ip,
        forwarded_for=forwarded_for,
        x_source=x_source,
        user_agent=user_agent,
        stream=False,
        status_code=200,
        response_time_ms=int((time_module.time() - started_at) * 1000),
        error_summary="",
    )
    return response


@app.get("/models")
async def list_models_alias(request: Request):
    return await list_models(request)


@app.get("/api/models")
async def list_models_api_alias(request: Request):
    return await list_models(request)


@app.get("/api/v1/models")
async def list_models_api_v1_alias(request: Request):
    return await list_models(request)


@app.get("/api/version")
async def ollama_version(request: Request):
    auth_response = enforce_runtime_access(request)
    if auth_response is not None:
        return auth_response
    return {"version": "serverless-proxy"}


@app.get("/api/tags")
async def ollama_tags(request: Request):
    auth_response = enforce_runtime_access(request)
    if auth_response is not None:
        return auth_response
    virtual_models = get_enabled_virtual_models()
    models = []
    for vm in virtual_models:
        models.append(
            {
                "name": vm["name"],
                "model": vm["name"],
                "modified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "size": 0,
                "digest": "",
                "details": {
                    "family": "proxy",
                    "parameter_size": "unknown",
                    "quantization_level": "unknown",
                },
            }
        )
    return {"models": models}


@app.post("/api/chat")
async def ollama_chat(request: Request):
    auth_response = enforce_runtime_access(request)
    if auth_response is not None:
        return auth_response
    data = await request.json()
    model = data.get("model") or data.get("name")
    if not model:
        return JSONResponse(status_code=400, content={"error": "model is required"})

    backend = get_backend(model)
    if backend is None:
        return JSONResponse(status_code=404, content={"error": "model not found"})

    stream = bool(data.get("stream", False))

    if getattr(backend, "endpoint_type", "") == "ollama":
        payload = dict(data)
        payload["model"] = getattr(backend, "model", model)
        payload["messages"] = _normalize_ollama_messages(payload.get("messages") or [])
        headers = {
            "Content-Type": "application/json",
            "X-Source": request.headers.get("x-source", "serverless-proxy"),
        }
        if getattr(backend, "api_key", ""):
            headers["Authorization"] = f"Bearer {backend.api_key}"

        if stream:

            async def ndjson_generator():
                async with httpx.AsyncClient(timeout=1200.0) as client:
                    async with client.stream(
                        "POST", f"{backend.url}/api/chat", headers=headers, json=payload
                    ) as resp:
                        async for line in resp.aiter_lines():
                            if line:
                                yield line + "\n"

            return StreamingResponse(
                ndjson_generator(), media_type="application/x-ndjson"
            )

        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                f"{backend.url}/api/chat", headers=headers, json=payload
            )
            if resp.status_code != 200:
                return JSONResponse(
                    status_code=resp.status_code, content={"error": resp.text}
                )
            return JSONResponse(content=resp.json())

    if stream:
        if getattr(backend, "endpoint_type", "") == "anthropic":
            return _ollama_streaming_not_supported_response()

        messages = data.get("messages") or []
        options = data.get("options") or {}
        incoming_source = request.headers.get("x-source", "serverless-proxy")
        actual_model = getattr(backend, "model", model)
        chat_data = {
            "model": actual_model,
            "messages": messages,
            "temperature": options.get("temperature", 0.7),
            "max_tokens": options.get("num_predict", 256),
            "top_p": options.get("top_p", 1.0),
            "stream": True,
        }
        if data.get("tools"):
            chat_data["tools"] = data.get("tools")

        if getattr(backend, "endpoint_type", "") == "openai_oauth":
            chat_data = _openai_chat_to_openai_oauth_payload(
                messages=messages,
                model=actual_model,
                stream=True,
                temperature=options.get("temperature", 0.7),
                max_tokens=options.get("num_predict", 256),
                top_p=options.get("top_p", 1.0),
                tools=data.get("tools") or [],
                tool_choice=None,
                parallel_tool_calls=None,
            )

        headers = {"Content-Type": "application/json"}
        auth_header = await resolve_endpoint_auth_header_async(
            {
                "id": getattr(backend, "endpoint_id", None),
                "api_key": getattr(backend, "api_key", ""),
                "oauth_enabled": getattr(backend, "oauth_enabled", 0),
                "oauth_grant_type": getattr(backend, "oauth_grant_type", ""),
                "oauth_token_url": getattr(backend, "oauth_token_url", ""),
                "oauth_client_id": getattr(backend, "oauth_client_id", ""),
                "oauth_client_secret": getattr(backend, "oauth_client_secret", ""),
                "oauth_scope": getattr(backend, "oauth_scope", ""),
                "oauth_refresh_token": getattr(backend, "oauth_refresh_token", ""),
                "oauth_token_expires_at": getattr(backend, "oauth_token_expires_at", 0),
                "oauth_token_request_format": getattr(
                    backend, "oauth_token_request_format", "json"
                ),
                "oauth_client_auth_method": getattr(
                    backend, "oauth_client_auth_method", "client_secret_post"
                ),
            }
        )
        if auth_header:
            headers["Authorization"] = auth_header
        headers["X-Source"] = incoming_source
        headers["X-Model"] = actual_model
        headers["X-Priority"] = "NORMAL"

        endpoint = _backend_effective_url(backend)
        endpoint_type = getattr(backend, "endpoint_type", "")
        if endpoint_type == "deepinfra":
            endpoint = f"{endpoint}/v1/openai/chat/completions"
        elif endpoint_type == "openai_oauth":
            endpoint = _resolve_openai_oauth_response_endpoint(endpoint)
        elif endpoint_type == "openwebui":
            endpoint = f"{endpoint}/api/chat/completions"
        elif endpoint_type == "queue":
            endpoint = f"{endpoint}/v1/chat/completions"
        else:
            endpoint = f"{endpoint}/v1/chat/completions"

        async def ndjson_generator():
            import json as json_module

            async with httpx.AsyncClient(timeout=1200.0) as client:
                async with client.stream(
                    "POST", endpoint, headers=headers, json=chat_data
                ) as resp:
                    if resp.status_code >= 400:
                        error_text = await resp.aread()
                        yield json_module.dumps(
                            {"error": error_text.decode("utf-8", errors="replace")}
                        ) + "\n"
                        return

                    done_sent = False
                    async for line in resp.aiter_lines():
                        if not line.strip() or not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            if not done_sent:
                                yield json_module.dumps(
                                    {
                                        "model": model,
                                        "created_at": time.strftime(
                                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                                        ),
                                        "message": {"role": "assistant", "content": ""},
                                        "done": True,
                                        "done_reason": "stop",
                                    }
                                ) + "\n"
                                done_sent = True
                            continue
                        try:
                            chunk = json_module.loads(data_str)
                        except Exception:
                            continue

                        content = ""
                        if endpoint_type == "openai_oauth":
                            evt_type = str(chunk.get("type") or "").lower()
                            if evt_type in (
                                "response.output_text.delta",
                                "response.output.delta",
                                "output_text.delta",
                            ):
                                content = str(chunk.get("delta") or chunk.get("text") or "")
                        else:
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content") or ""

                        if content:
                            yield json_module.dumps(
                                {
                                    "model": model,
                                    "created_at": time.strftime(
                                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                                    ),
                                    "message": {"role": "assistant", "content": content},
                                    "done": False,
                                }
                            ) + "\n"

                    if not done_sent:
                        yield json_module.dumps(
                            {
                                "model": model,
                                "created_at": time.strftime(
                                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                                ),
                                "message": {"role": "assistant", "content": ""},
                                "done": True,
                                "done_reason": "stop",
                            }
                        ) + "\n"

        return StreamingResponse(ndjson_generator(), media_type="application/x-ndjson")

    messages = data.get("messages") or []
    options = data.get("options") or {}
    if getattr(backend, "endpoint_type", "") == "openai_oauth":
        backend_result, error, status_code = await _call_backend_chat_via_stream_collect(
            backend=backend,
            model=model,
            messages=messages,
            temperature=options.get("temperature", 0.7),
            max_tokens=options.get("num_predict", 256),
            top_p=options.get("top_p", 1.0),
            tools=data.get("tools") or [],
            incoming_source=request.headers.get("x-source", "serverless-proxy"),
        )
    else:
        backend_result, error, status_code = await backend.chat_completion(
            messages=messages,
            model=model,
            temperature=options.get("temperature", 0.7),
            max_tokens=options.get("num_predict", 256),
            top_p=options.get("top_p", 1.0),
            stream=False,
            tools=data.get("tools") or [],
        )
    if error:
        return JSONResponse(status_code=status_code, content=error)
    result = backend_result.get("result", backend_result)
    if "_stream_data" in result:
        assembled = {"choices": [{"message": {"content": ""}}]}
        for line in result["_stream_data"].split("\n"):
            stripped = line.strip()
            if not stripped.startswith("data:"):
                continue
            payload = stripped[5:].lstrip()
            if not payload or payload == "[DONE]":
                continue
            try:
                chunk = json.loads(payload)
            except Exception:
                continue
            if "choices" in chunk and chunk["choices"]:
                delta = chunk["choices"][0].get("delta", {})
                if delta.get("content"):
                    assembled["choices"][0]["message"]["content"] += delta["content"]
        result = assembled
    return JSONResponse(content=_openai_to_ollama_chat_response(result, model))


@app.post("/api/generate")
async def ollama_generate(request: Request):
    auth_response = enforce_runtime_access(request)
    if auth_response is not None:
        return auth_response
    data = await request.json()
    model = data.get("model") or data.get("name")
    if not model:
        return JSONResponse(status_code=400, content={"error": "model is required"})

    backend = get_backend(model)
    if backend is None:
        return JSONResponse(status_code=404, content={"error": "model not found"})

    stream = bool(data.get("stream", False))

    # Native passthrough for Ollama endpoints
    if getattr(backend, "endpoint_type", "") == "ollama":
        payload = {
            "model": getattr(backend, "model", model),
            "prompt": data.get("prompt", ""),
            "stream": stream,
        }
        for k in (
            "suffix",
            "images",
            "think",
            "format",
            "options",
            "system",
            "template",
            "raw",
            "keep_alive",
            "context",
            "width",
            "height",
            "steps",
        ):
            if data.get(k) is not None:
                payload[k] = data.get(k)

        headers = {
            "Content-Type": "application/json",
            "X-Source": request.headers.get("x-source", "serverless-proxy"),
        }
        if getattr(backend, "api_key", ""):
            headers["Authorization"] = f"Bearer {backend.api_key}"

        if stream:

            async def ndjson_generator():
                async with httpx.AsyncClient(timeout=1200.0) as client:
                    async with client.stream(
                        "POST",
                        f"{backend.url}/api/generate",
                        headers=headers,
                        json=payload,
                    ) as resp:
                        async for line in resp.aiter_lines():
                            if line:
                                yield line + "\n"

            return StreamingResponse(
                ndjson_generator(), media_type="application/x-ndjson"
            )

        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                f"{backend.url}/api/generate", headers=headers, json=payload
            )
            if resp.status_code != 200:
                return JSONResponse(
                    status_code=resp.status_code, content={"error": resp.text}
                )
            return JSONResponse(content=resp.json())

    if stream:
        if getattr(backend, "endpoint_type", "") == "anthropic":
            return _ollama_streaming_not_supported_response()

        prompt = data.get("prompt", "")
        messages = []
        if data.get("system"):
            messages.append({"role": "system", "content": data.get("system")})
        messages.append({"role": "user", "content": prompt})

        options = data.get("options") or {}
        incoming_source = request.headers.get("x-source", "serverless-proxy")
        actual_model = getattr(backend, "model", model)
        chat_data = {
            "model": actual_model,
            "messages": messages,
            "temperature": options.get("temperature", 0.7),
            "max_tokens": options.get("num_predict", 256),
            "top_p": options.get("top_p", 1.0),
            "stream": True,
        }

        if getattr(backend, "endpoint_type", "") == "openai_oauth":
            chat_data = _openai_chat_to_openai_oauth_payload(
                messages=messages,
                model=actual_model,
                stream=True,
                temperature=options.get("temperature", 0.7),
                max_tokens=options.get("num_predict", 256),
                top_p=options.get("top_p", 1.0),
                tools=[],
                tool_choice=None,
                parallel_tool_calls=None,
            )

        headers = {"Content-Type": "application/json"}
        auth_header = await resolve_endpoint_auth_header_async(
            {
                "id": getattr(backend, "endpoint_id", None),
                "api_key": getattr(backend, "api_key", ""),
                "oauth_enabled": getattr(backend, "oauth_enabled", 0),
                "oauth_grant_type": getattr(backend, "oauth_grant_type", ""),
                "oauth_token_url": getattr(backend, "oauth_token_url", ""),
                "oauth_client_id": getattr(backend, "oauth_client_id", ""),
                "oauth_client_secret": getattr(backend, "oauth_client_secret", ""),
                "oauth_scope": getattr(backend, "oauth_scope", ""),
                "oauth_refresh_token": getattr(backend, "oauth_refresh_token", ""),
                "oauth_token_expires_at": getattr(backend, "oauth_token_expires_at", 0),
                "oauth_token_request_format": getattr(
                    backend, "oauth_token_request_format", "json"
                ),
                "oauth_client_auth_method": getattr(
                    backend, "oauth_client_auth_method", "client_secret_post"
                ),
            }
        )
        if auth_header:
            headers["Authorization"] = auth_header
        headers["X-Source"] = incoming_source
        headers["X-Model"] = actual_model
        headers["X-Priority"] = "NORMAL"

        endpoint = backend.url
        endpoint_type = getattr(backend, "endpoint_type", "")
        if endpoint_type == "deepinfra":
            endpoint = f"{endpoint}/v1/openai/chat/completions"
        elif endpoint_type == "openai_oauth":
            endpoint = _resolve_openai_oauth_response_endpoint(endpoint)
        elif endpoint_type == "openwebui":
            endpoint = f"{endpoint}/api/chat/completions"
        elif endpoint_type == "queue":
            endpoint = f"{endpoint}/v1/chat/completions"
        else:
            endpoint = f"{endpoint}/v1/chat/completions"

        async def ndjson_generator():
            import json as json_module

            async with httpx.AsyncClient(timeout=1200.0) as client:
                async with client.stream(
                    "POST", endpoint, headers=headers, json=chat_data
                ) as resp:
                    if resp.status_code >= 400:
                        error_text = await resp.aread()
                        yield json_module.dumps(
                            {"error": error_text.decode("utf-8", errors="replace")}
                        ) + "\n"
                        return

                    done_sent = False
                    async for line in resp.aiter_lines():
                        if not line.strip() or not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            if not done_sent:
                                yield json_module.dumps(
                                    {
                                        "model": model,
                                        "created_at": time.strftime(
                                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                                        ),
                                        "response": "",
                                        "done": True,
                                        "done_reason": "stop",
                                    }
                                ) + "\n"
                                done_sent = True
                            continue
                        try:
                            chunk = json_module.loads(data_str)
                        except Exception:
                            continue

                        content = ""
                        if endpoint_type == "openai_oauth":
                            evt_type = str(chunk.get("type") or "").lower()
                            if evt_type in (
                                "response.output_text.delta",
                                "response.output.delta",
                                "output_text.delta",
                            ):
                                content = str(chunk.get("delta") or chunk.get("text") or "")
                        else:
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content") or ""

                        if content:
                            yield json_module.dumps(
                                {
                                    "model": model,
                                    "created_at": time.strftime(
                                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                                    ),
                                    "response": content,
                                    "done": False,
                                }
                            ) + "\n"

                    if not done_sent:
                        yield json_module.dumps(
                            {
                                "model": model,
                                "created_at": time.strftime(
                                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                                ),
                                "response": "",
                                "done": True,
                                "done_reason": "stop",
                            }
                        ) + "\n"

        return StreamingResponse(ndjson_generator(), media_type="application/x-ndjson")

    prompt = data.get("prompt", "")
    messages = []
    if data.get("system"):
        messages.append({"role": "system", "content": data.get("system")})
    messages.append({"role": "user", "content": prompt})

    options = data.get("options") or {}
    if getattr(backend, "endpoint_type", "") == "openai_oauth":
        backend_result, error, status_code = await _call_backend_chat_via_stream_collect(
            backend=backend,
            model=model,
            messages=messages,
            temperature=options.get("temperature", 0.7),
            max_tokens=options.get("num_predict", 256),
            top_p=options.get("top_p", 1.0),
            tools=[],
            incoming_source=request.headers.get("x-source", "serverless-proxy"),
        )
    else:
        backend_result, error, status_code = await backend.chat_completion(
            messages=messages,
            model=model,
            temperature=options.get("temperature", 0.7),
            max_tokens=options.get("num_predict", 256),
            top_p=options.get("top_p", 1.0),
            stream=False,
        )
    if error:
        return JSONResponse(status_code=status_code, content=error)
    result = backend_result.get("result", backend_result)
    if "_stream_data" in result:
        assembled = {"choices": [{"message": {"content": ""}}]}
        for line in result["_stream_data"].split("\n"):
            stripped = line.strip()
            if not stripped.startswith("data:"):
                continue
            payload = stripped[5:].lstrip()
            if not payload or payload == "[DONE]":
                continue
            try:
                chunk = json.loads(payload)
            except Exception:
                continue
            if "choices" in chunk and chunk["choices"]:
                delta = chunk["choices"][0].get("delta", {})
                if delta.get("content"):
                    assembled["choices"][0]["message"]["content"] += delta["content"]
        result = assembled
    raw = _openai_to_ollama_chat_response(result, model)
    return {
        "model": raw.get("model", model),
        "created_at": raw.get(
            "created_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ),
        "response": (raw.get("message") or {}).get("content", ""),
        "done": True,
        "done_reason": raw.get("done_reason", "stop"),
        "prompt_eval_count": raw.get("prompt_eval_count", 0),
        "eval_count": raw.get("eval_count", 0),
    }


@app.post("/api/embed")
@app.post("/api/embeddings")
async def ollama_embed(request: Request):
    auth_response = enforce_runtime_access(request)
    if auth_response is not None:
        return auth_response
    data = await request.json()
    model = data.get("model")
    if not model:
        return JSONResponse(status_code=400, content={"error": "model is required"})

    backend = get_backend(model)
    if backend is None:
        return JSONResponse(status_code=404, content={"error": "model not found"})

    result, error, status_code = await backend.embeddings(
        input_text=data.get("input", ""),
        model=model,
    )
    if error:
        return JSONResponse(status_code=status_code, content=error)

    vectors = [
        row.get("embedding") for row in result.get("data", []) if isinstance(row, dict)
    ]
    return {"model": model, "embeddings": vectors}


@app.post("/api/show")
async def ollama_show(request: Request):
    auth_response = enforce_runtime_access(request)
    if auth_response is not None:
        return auth_response
    data = await request.json()
    model = data.get("model") or data.get("name")
    if not model:
        return JSONResponse(status_code=400, content={"error": "model is required"})

    backend = get_backend(model)
    if backend is None:
        return JSONResponse(status_code=404, content={"error": "model not found"})

    if getattr(backend, "endpoint_type", "") == "ollama":
        return await _ollama_passthrough(request, "/api/show", method="POST")

    actual_model = getattr(backend, "model", model)
    endpoint_type = getattr(backend, "endpoint_type", "") or "proxy"
    family = endpoint_type.replace("_", "-")
    capabilities = ["chat"]
    if endpoint_type in ("openai", "openai_oauth", "openwebui", "deepinfra", "queue", "vllm"):
        capabilities.append("completion")
    if endpoint_type not in ("anthropic",):
        capabilities.append("embedding")

    details = {
        "family": family,
        "families": [family],
        "parameter_size": "unknown",
        "quantization_level": "unknown",
        "parent_model": actual_model,
        "format": endpoint_type,
        "capabilities": capabilities,
    }

    return {
        "license": "unknown",
        "modelfile": "# synthetic metadata generated by serverless-proxy",
        "parameters": "",
        "template": "",
        "details": details,
        "model_info": {
            "proxy_virtual_model": model,
            "proxy_actual_model": actual_model,
            "proxy_endpoint_type": endpoint_type,
            "proxy_translated": True,
        },
        "messages": [],
    }


@app.get("/api/ps")
@app.post("/api/ps")
async def ollama_ps(request: Request):
    auth_response = enforce_runtime_access(request)
    if auth_response is not None:
        return auth_response
    method = request.method.upper()
    return await _ollama_passthrough(request, "/api/ps", method=method)


@app.post("/api/pull")
async def ollama_pull(request: Request):
    auth_response = enforce_runtime_access(request, internal_only=True)
    if auth_response is not None:
        return auth_response
    return await _ollama_passthrough(request, "/api/pull", method="POST")


@app.post("/api/push")
async def ollama_push(request: Request):
    auth_response = enforce_runtime_access(request, internal_only=True)
    if auth_response is not None:
        return auth_response
    return await _ollama_passthrough(request, "/api/push", method="POST")


@app.post("/api/create")
async def ollama_create(request: Request):
    auth_response = enforce_runtime_access(request, internal_only=True)
    if auth_response is not None:
        return auth_response
    return await _ollama_passthrough(request, "/api/create", method="POST")


@app.post("/api/copy")
async def ollama_copy(request: Request):
    auth_response = enforce_runtime_access(request, internal_only=True)
    if auth_response is not None:
        return auth_response
    return await _ollama_passthrough(request, "/api/copy", method="POST")


@app.delete("/api/delete")
@app.post("/api/delete")
async def ollama_delete(request: Request):
    auth_response = enforce_runtime_access(request, internal_only=True)
    if auth_response is not None:
        return auth_response
    method = request.method.upper()
    return await _ollama_passthrough(request, "/api/delete", method=method)


@app.head("/api/blobs/{digest}")
async def ollama_blob_head(request: Request, digest: str):
    auth_response = enforce_runtime_access(request, internal_only=True)
    if auth_response is not None:
        return auth_response
    url, api_key = _resolve_ollama_target_for_request(None)
    if not url:
        return JSONResponse(
            status_code=404,
            content={"error": "No enabled Ollama endpoint is configured"},
        )
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.head(f"{url}/api/blobs/{digest}", headers=headers)
        return Response(status_code=resp.status_code)


@app.post("/api/blobs/{digest}")
async def ollama_blob_post(request: Request, digest: str):
    auth_response = enforce_runtime_access(request, internal_only=True)
    if auth_response is not None:
        return auth_response
    url, api_key = _resolve_ollama_target_for_request(None)
    if not url:
        return JSONResponse(
            status_code=404,
            content={"error": "No enabled Ollama endpoint is configured"},
        )

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    raw_body = await request.body()
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(
            f"{url}/api/blobs/{digest}", headers=headers, content=raw_body
        )
        try:
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except Exception:
            return JSONResponse(
                status_code=resp.status_code, content={"error": resp.text}
            )


@app.post("/v1/completions")
async def completions(request: Request):
    """
    Legacy completions endpoint - converts to chat completions format.
    Many tools still use this for text-only completions.
    """
    auth_response = enforce_runtime_access(request)
    if auth_response is not None:
        return auth_response
    inbound_api_key_id, inbound_api_key_label = _get_request_inbound_key_meta(request)
    start_time = time_module.time()
    client_ip, forwarded_for = _resolve_activity_client_ip(request)
    x_source = (request.headers.get("x-source") or "").strip()
    user_agent = (request.headers.get("user-agent") or "").strip()
    data = await request.json()

    prompt = data.get("prompt", "")
    model = data.get("model")
    temperature = data.get("temperature", 0.7)
    max_tokens = data.get("max_tokens", 256)
    top_p = data.get("top_p", 1.0)
    stream = data.get("stream", False)
    stop = data.get("stop")

    # Convert to chat format
    messages = [{"role": "user", "content": prompt}]

    # Get backend for this model
    backend = get_backend(model)
    if backend is None:
        response = model_not_found_response(model)
        log_recent_activity(
            request_id="",
            method=request.method,
            path=request.url.path,
            request_type="completions",
            virtual_model=model or "",
            actual_model="",
            endpoint_name="",
            endpoint_id=None,
            endpoint_type="",
            inbound_api_key_id=inbound_api_key_id,
            inbound_api_key_label=inbound_api_key_label,
            client_ip=client_ip,
            forwarded_for=forwarded_for,
            x_source=x_source,
            user_agent=user_agent,
            stream=bool(stream),
            status_code=404,
            response_time_ms=int((time_module.time() - start_time) * 1000),
            error_summary=f"model not found: {model}",
        )
        return response

    virtual_model = getattr(backend, "virtual_model_name", model)
    actual_model = getattr(backend, "model", model)
    endpoint_id = getattr(backend, "endpoint_id", None)
    endpoint_type = getattr(backend, "endpoint_type", "")

    cache_enabled = is_virtual_model_cache_enabled(model)
    cache_control_req = (request.headers.get("cache-control") or "").lower()
    cache_key = None

    if cache_enabled and "no-store" not in cache_control_req:
        try:
            cache_key = normalize_request_for_cache(
                {
                    "model": model,
                    "messages": messages,
                    "tools": [],
                    "temperature": temperature,
                    "top_p": top_p,
                    "max_tokens": max_tokens,
                    "stop": stop,
                }
            )
            cached_resp = get_cached_response(cache_key)
            if cached_resp:
                response = JSONResponse(content=cached_resp)
                response.headers["X-Cache"] = "HIT"
                log_recent_activity(
                    request_id="",
                    method=request.method,
                    path=request.url.path,
                    request_type="completions",
                    virtual_model=virtual_model or "",
                    actual_model=actual_model or "",
                    endpoint_name="",
                    endpoint_id=endpoint_id,
                    endpoint_type=endpoint_type or "",
                    inbound_api_key_id=inbound_api_key_id,
                    inbound_api_key_label=inbound_api_key_label,
                    client_ip=client_ip,
                    forwarded_for=forwarded_for,
                    x_source=x_source,
                    user_agent=user_agent,
                    stream=False,
                    status_code=200,
                    response_time_ms=int((time_module.time() - start_time) * 1000),
                    error_summary="",
                )
                return response
        except Exception as e:
            debug_log("warn", f"[CACHE] embeddings lookup failed model={model}: {e}")

    # Call backend
    if not stream and getattr(backend, "endpoint_type", "") == "openai_oauth":
        backend_result, error, status_code = await _call_backend_chat_via_stream_collect(
            backend=backend,
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            tools=[],
            incoming_source=x_source or "serverless-proxy",
        )
    else:
        backend_result, error, status_code = await backend.chat_completion(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stream=stream,
            stop=stop,
        )

    if error:
        response = JSONResponse(status_code=status_code, content=error)
        log_recent_activity(
            request_id="",
            method=request.method,
            path=request.url.path,
            request_type="completions",
            virtual_model=virtual_model or "",
            actual_model=actual_model or "",
            endpoint_name="",
            endpoint_id=endpoint_id,
            endpoint_type=endpoint_type or "",
            inbound_api_key_id=inbound_api_key_id,
            inbound_api_key_label=inbound_api_key_label,
            client_ip=client_ip,
            forwarded_for=forwarded_for,
            x_source=x_source,
            user_agent=user_agent,
            stream=bool(stream),
            status_code=int(status_code or 500),
            response_time_ms=int((time_module.time() - start_time) * 1000),
            error_summary=(error.get("error") if isinstance(error, dict) else str(error)),
        )
        return response

    # Extract result
    result = backend_result
    if "result" in backend_result:
        result = backend_result["result"]

    content = ""
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    job_id = result.get("id", f"cmpl-{int(time_module.time())}")

    if "_stream_data" in result:
        stream_data = result["_stream_data"]
        for line in stream_data.split("\n"):
            stripped = line.strip()
            if not stripped.startswith("data:"):
                continue
            payload = stripped[5:].lstrip()
            if not payload or payload == "[DONE]":
                continue
            try:
                chunk = json.loads(payload)
            except Exception:
                continue
            if "choices" in chunk and chunk["choices"]:
                delta = chunk["choices"][0].get("delta", {})
                if delta.get("content"):
                    content += delta["content"]
            if chunk.get("usage"):
                usage = chunk["usage"]
        if getattr(backend, "endpoint_type", "") == "openai_oauth" and not any(usage.values()):
            usage = _estimate_openai_oauth_usage(
                messages=messages,
                tools=[],
                full_content=content,
                full_reasoning="",
                tool_calls=[],
            )
    elif "choices" in result and result["choices"]:
        content = result["choices"][0].get("message", {}).get("content", "") or ""
        usage = result.get(
            "usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        )

    if stream:
        if "_stream_data" in result:

            async def passthrough_sse():
                yielded_text = False
                for line in result["_stream_data"].split("\n"):
                    stripped = line.strip()
                    if not stripped.startswith("data:"):
                        continue
                    payload = stripped[5:].lstrip()
                    if payload == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(payload)
                    except Exception:
                        continue
                    if "choices" in chunk and chunk["choices"]:
                        delta = chunk["choices"][0].get("delta", {})
                        text = delta.get("content") or ""
                        if text:
                            yielded_text = True
                            yield f"data: {json.dumps({'id': job_id, 'choices': [{'text': text, 'index': 0}], 'model': model})}\n\n"
                if not yielded_text and content:
                    yield f"data: {json.dumps({'id': job_id, 'choices': [{'text': content, 'index': 0}], 'model': model})}\n\n"
                yield "data: [DONE]\n\n"

            response = StreamingResponse(
                passthrough_sse(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )
        else:

            async def generate_sse():
                if content:
                    yield f"data: {json.dumps({'id': job_id, 'choices': [{'text': content, 'index': 0}], 'model': model})}\n\n"
                yield "data: [DONE]\n\n"

            response = StreamingResponse(
                generate_sse(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )
        log_recent_activity(
            request_id=str(job_id),
            method=request.method,
            path=request.url.path,
            request_type="completions",
            virtual_model=virtual_model or "",
            actual_model=actual_model or "",
            endpoint_name="",
            endpoint_id=endpoint_id,
            endpoint_type=endpoint_type or "",
            inbound_api_key_id=inbound_api_key_id,
            inbound_api_key_label=inbound_api_key_label,
            client_ip=client_ip,
            forwarded_for=forwarded_for,
            x_source=x_source,
            user_agent=user_agent,
            stream=True,
            status_code=200,
            response_time_ms=int((time_module.time() - start_time) * 1000),
            error_summary="",
        )
        return response

    # Log usage
    response_time_ms = int((time_module.time() - start_time) * 1000)
    log_completion_usage(
        model,
        None,
        None,
        usage,
        response_time_ms,
        inbound_api_key_id=inbound_api_key_id,
        inbound_api_key_label=inbound_api_key_label,
    )

    response_content = {
        "id": job_id,
        "object": "text_completion",
        "created": int(time_module.time()),
        "model": model,
        "choices": [
            {
                "text": content,
                "index": 0,
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }
    response = JSONResponse(content=response_content)
    if cache_enabled and cache_key:
        try:
            ttl = int(get_setting("cache_ttl_chat", "300") or 300)
            store_cached_response(cache_key, response_content, ttl, virtual_model)
        except Exception as e:
            debug_log("warn", f"[CACHE] completions store failed model={model}: {e}")
    log_recent_activity(
        request_id=str(job_id),
        method=request.method,
        path=request.url.path,
        request_type="completions",
        virtual_model=virtual_model or "",
        actual_model=actual_model or "",
        endpoint_name="",
        endpoint_id=endpoint_id,
        endpoint_type=endpoint_type or "",
        inbound_api_key_id=inbound_api_key_id,
        inbound_api_key_label=inbound_api_key_label,
        client_ip=client_ip,
        forwarded_for=forwarded_for,
        x_source=x_source,
        user_agent=user_agent,
        stream=False,
        status_code=200,
        response_time_ms=int((time_module.time() - start_time) * 1000),
        error_summary="",
    )
    return response


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    """
    Embeddings endpoint for vector representations.
    Routes to backend if supported, otherwise returns error.
    """
    auth_response = enforce_runtime_access(request)
    if auth_response is not None:
        return auth_response
    inbound_api_key_id, inbound_api_key_label = _get_request_inbound_key_meta(request)
    start_time = time_module.time()
    client_ip, forwarded_for = _resolve_activity_client_ip(request)
    x_source = (request.headers.get("x-source") or "").strip()
    user_agent = (request.headers.get("user-agent") or "").strip()
    data = await request.json()

    input_text = data.get("input", "")
    model = data.get("model", os.getenv("EMBEDDING_MODEL", "nomic-embed-text"))

    # Get backend for this model (checks virtual models first)
    backend = get_backend(model)
    if backend is None:
        response = model_not_found_response(model)
        log_recent_activity(
            request_id="",
            method=request.method,
            path=request.url.path,
            request_type="embeddings",
            virtual_model=model or "",
            actual_model="",
            endpoint_name="",
            endpoint_id=None,
            endpoint_type="",
            inbound_api_key_id=inbound_api_key_id,
            inbound_api_key_label=inbound_api_key_label,
            client_ip=client_ip,
            forwarded_for=forwarded_for,
            x_source=x_source,
            user_agent=user_agent,
            stream=False,
            status_code=404,
            response_time_ms=int((time_module.time() - start_time) * 1000),
            error_summary=f"model not found: {model}",
        )
        return response

    virtual_model = getattr(backend, "virtual_model_name", model)
    actual_model = getattr(backend, "model", model)
    endpoint_id = getattr(backend, "endpoint_id", None)
    endpoint_type = getattr(backend, "endpoint_type", "")
    cache_enabled = is_virtual_model_cache_enabled(model)
    cache_control_req = (request.headers.get("cache-control") or "").lower()
    cache_key = None

    if cache_enabled and "no-store" not in cache_control_req:
        try:
            cache_key = normalize_request_for_cache(
                {
                    "model": model,
                    "messages": [{"role": "user", "content": input_text}],
                    "tools": [],
                    "temperature": None,
                    "top_p": None,
                    "max_tokens": None,
                }
            )
            cached_resp = get_cached_response(cache_key)
            if cached_resp:
                response = JSONResponse(content=cached_resp)
                response.headers["X-Cache"] = "HIT"
                log_recent_activity(
                    request_id="",
                    method=request.method,
                    path=request.url.path,
                    request_type="embeddings",
                    virtual_model=virtual_model or "",
                    actual_model=actual_model or "",
                    endpoint_name="",
                    endpoint_id=endpoint_id,
                    endpoint_type=endpoint_type or "",
                    inbound_api_key_id=inbound_api_key_id,
                    inbound_api_key_label=inbound_api_key_label,
                    client_ip=client_ip,
                    forwarded_for=forwarded_for,
                    x_source=x_source,
                    user_agent=user_agent,
                    stream=False,
                    status_code=200,
                    response_time_ms=int((time_module.time() - start_time) * 1000),
                    error_summary="",
                )
                return response
        except Exception as e:
            debug_log("warn", f"[CACHE] embeddings lookup failed model={model}: {e}")

    # Check if backend supports embeddings
    if hasattr(backend, "embeddings"):
        result, error, status_code = await backend.embeddings(
            input_text=input_text, model=model
        )
        if error:
            response = JSONResponse(status_code=status_code, content=error)
            log_recent_activity(
                request_id="",
                method=request.method,
                path=request.url.path,
                request_type="embeddings",
                virtual_model=virtual_model or "",
                actual_model=actual_model or "",
                endpoint_name="",
                endpoint_id=endpoint_id,
                endpoint_type=endpoint_type or "",
                inbound_api_key_id=inbound_api_key_id,
                inbound_api_key_label=inbound_api_key_label,
                client_ip=client_ip,
                forwarded_for=forwarded_for,
                x_source=x_source,
                user_agent=user_agent,
                stream=False,
                status_code=int(status_code or 500),
                response_time_ms=int((time_module.time() - start_time) * 1000),
                error_summary=(error.get("error") if isinstance(error, dict) else str(error)),
            )
            return response

        # Log usage if backend returns token info
        response_time_ms = int((time_module.time() - start_time) * 1000)
        try:
            usage_data = result.get("usage", {})
            input_tokens = (
                usage_data.get("prompt_tokens", 0)
                or usage_data.get("input_tokens", 0)
                or 0
            )
            output_tokens = usage_data.get("completion_tokens", 0) or 0

            if input_tokens > 0 or output_tokens > 0:
                log_embedding_usage(
                    model,
                    None,
                    None,
                    input_tokens,
                    output_tokens,
                    response_time_ms,
                    inbound_api_key_id=inbound_api_key_id,
                    inbound_api_key_label=inbound_api_key_label,
                )
        except:
            pass  # Embeddings might not return token usage

        response = JSONResponse(content=result)
        if cache_enabled and cache_key:
            try:
                ttl = int(get_setting("cache_ttl_embeddings", "3600") or 3600)
                store_cached_response(cache_key, result, ttl, virtual_model)
            except Exception as e:
                debug_log("warn", f"[CACHE] embeddings store failed model={model}: {e}")
        log_recent_activity(
            request_id="",
            method=request.method,
            path=request.url.path,
            request_type="embeddings",
            virtual_model=virtual_model or "",
            actual_model=actual_model or "",
            endpoint_name="",
            endpoint_id=endpoint_id,
            endpoint_type=endpoint_type or "",
            inbound_api_key_id=inbound_api_key_id,
            inbound_api_key_label=inbound_api_key_label,
            client_ip=client_ip,
            forwarded_for=forwarded_for,
            x_source=x_source,
            user_agent=user_agent,
            stream=False,
            status_code=200,
            response_time_ms=int((time_module.time() - start_time) * 1000),
            error_summary="",
        )
        return response

    # Embeddings not supported - return error with OpenAI format
    response = JSONResponse(
        status_code=501,
        content={
            "error": {
                "message": "Embeddings not supported by current backend",
                "type": "invalid_request_error",
                "code": "not_implemented",
            }
        },
    )
    log_recent_activity(
        request_id="",
        method=request.method,
        path=request.url.path,
        request_type="embeddings",
        virtual_model=virtual_model or "",
        actual_model=actual_model or "",
        endpoint_name="",
        endpoint_id=endpoint_id,
        endpoint_type=endpoint_type or "",
        inbound_api_key_id=inbound_api_key_id,
        inbound_api_key_label=inbound_api_key_label,
        client_ip=client_ip,
        forwarded_for=forwarded_for,
        x_source=x_source,
        user_agent=user_agent,
        stream=False,
        status_code=501,
        response_time_ms=int((time_module.time() - start_time) * 1000),
        error_summary="Embeddings not supported by current backend",
    )
    return response


@app.post("/embeddings")
async def embeddings_alias(request: Request):
    return await embeddings(request)


@app.post("/api/v1/embeddings")
async def embeddings_api_v1_alias(request: Request):
    return await embeddings(request)


# ============================================================================
# Flask Admin Routes
# ============================================================================


def validate_session(flask_cookies=None):
    """Proxy session validation to ai-menu-system. Accepts cookies dict for FastAPI compatibility."""
    # Skip auth if disabled
    print(
        f"DEBUG: AUTH_ENABLED = {is_auth_enabled()}, type = {type(is_auth_enabled())}"
    )
    if not is_auth_enabled():
        return {"valid": True, "user": "admin"}

    try:
        if flask_cookies is not None:
            cookies = flask_cookies
        else:
            cookies = flask_request.cookies
        # Forward all cookies to validate endpoint
        resp = httpx.get(f"{AIMENU_URL}/session/validate", cookies=cookies, timeout=5)
        result = resp.json()
        # Debug log
        print(
            f"Session validate: cookies={list(cookies.keys()) if cookies else []}, result={result}"
        )
        return result
    except Exception as e:
        print(f"Session validate error: {e}")
        return {"valid": False}


def validate_session_fastapi(request: Request):
    """FastAPI version of session validation."""
    # Skip auth if disabled
    if not is_auth_enabled():
        return {"valid": True, "user": "admin"}

    try:
        cookies = dict(request.cookies)
        resp = httpx.get(f"{AIMENU_URL}/session/validate", cookies=cookies, timeout=5)
        result = resp.json()
        print(
            f"FastAPI Session validate: cookies={list(cookies.keys())}, result={result}"
        )
        return result
    except Exception as e:
        print(f"FastAPI Session validate error: {e}")
        return {"valid": False}


def get_menu_login_url():
    """Get the public URL for menu login (handles HTTPS)."""
    # Prefer explicit public URL for reverse-proxy/HTTPS setups.
    # Fallback to AIMENU_URL (local auth service) instead of any external domain.
    menu_base = os.getenv("AIMENU_PUBLIC_URL", "").strip()
    if not menu_base:
        menu_base = os.getenv("AIMENU_URL", "http://localhost:5000").strip()
    return menu_base.rstrip("/")


@flask_app.route("/")
def admin_index():
    """Admin dashboard - check auth first."""
    auth = validate_session()
    if not auth.get("valid"):
        return redirect(f"{get_menu_login_url()}/login?redirect=/")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM endpoints ORDER BY name")
        endpoints = [dict(row) for row in cursor.fetchall()]
        cursor.execute("""
            SELECT vm.*, e.name as endpoint_name 
            FROM virtual_models vm 
            LEFT JOIN endpoints e ON vm.endpoint_id = e.id 
            ORDER BY vm.name
        """)
        virtual_models = [dict(row) for row in cursor.fetchall()]

    return render_template(
        "admin_dashboard.html",
        user=auth.get("username"),
        endpoints=endpoints,
        virtual_models=virtual_models,
        auth_enabled=is_auth_enabled(),
        use_ai_queue=get_setting("use_ai_queue", "false") == "true",
        initial_settings_json=json.dumps(_get_all_settings()),
    )


@flask_app.route("/proxy-dashboard")
def proxy_dashboard():
    """Serverless proxy dashboard - check auth server-side."""
    auth = validate_session()
    if not auth.get("valid"):
        return redirect(f"{get_menu_login_url()}/login?redirect=/proxy-dashboard")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM endpoints ORDER BY name")
        endpoints = [dict(row) for row in cursor.fetchall()]
        cursor.execute("""
            SELECT vm.*, e.name as endpoint_name 
            FROM virtual_models vm 
            LEFT JOIN endpoints e ON vm.endpoint_id = e.id 
            ORDER BY vm.name
        """)
        virtual_models = [dict(row) for row in cursor.fetchall()]

    return render_template(
        "admin_dashboard.html",
        user=auth.get("username"),
        endpoints=endpoints,
        virtual_models=virtual_models,
        auth_enabled=is_auth_enabled(),
        use_ai_queue=get_setting("use_ai_queue", "false") == "true",
        initial_settings_json=json.dumps(_get_all_settings()),
    )


@flask_app.route("/endpoints", methods=["GET", "POST"])
def admin_endpoints():
    """Create endpoint or redirect to admin."""
    # Handle POST to create new endpoint
    if flask_request.method == "POST":
        auth = validate_session()
        if not auth.get("valid"):
            return flask_jsonify({"error": "Unauthorized"}), 401

        data = flask_request.get_json()
        if not data:
            return flask_jsonify({"error": "No data provided"}), 400
        data = _apply_endpoint_defaults(data)

        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO endpoints (
                        name, url, api_key, endpoint_type, priority, enabled,
                        oauth_enabled, oauth_grant_type, oauth_token_url,
                        oauth_client_id, oauth_client_secret, oauth_scope,
                        oauth_refresh_token, oauth_token_expires_at,
                        oauth_token_request_format, oauth_client_auth_method,
                        health_check_url
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        data.get("name"),
                        data.get("url"),
                        data.get("api_key", ""),
                        data.get("endpoint_type", "openai"),
                        int(data.get("priority", 0)),
                        1 if data.get("enabled", True) else 0,
                        1 if _coerce_bool(data.get("oauth_enabled", 0)) else 0,
                        data.get("oauth_grant_type", "refresh_token"),
                        data.get("oauth_token_url", ""),
                        data.get("oauth_client_id", ""),
                        data.get("oauth_client_secret", ""),
                        data.get("oauth_scope", ""),
                        data.get("oauth_refresh_token", ""),
                        int(data.get("oauth_token_expires_at", 0) or 0),
                        data.get("oauth_token_request_format", "json"),
                        data.get("oauth_client_auth_method", "client_secret_post"),
                        data.get("health_check_url", ""),
                    ),
                )
                conn.commit()
                new_id = cursor.lastrowid

            return flask_jsonify({"id": new_id, "status": "ok"})
        except Exception as e:
            return flask_jsonify({"error": str(e)}), 400

    # GET redirects to admin
    return redirect("/admin")


@flask_app.route("/endpoints/<int:endpoint_id>", methods=["PUT"])
def update_endpoint(endpoint_id):
    """Update endpoint."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    data = _apply_endpoint_defaults(flask_request.get_json() or {})
    data = _preserve_endpoint_oauth_secrets_on_update(endpoint_id, data)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE endpoints 
            SET name = ?, url = ?, api_key = ?, endpoint_type = ?, priority = ?, enabled = ?,
                oauth_enabled = ?, oauth_grant_type = ?, oauth_token_url = ?,
                oauth_client_id = ?, oauth_client_secret = ?, oauth_scope = ?,
                oauth_refresh_token = ?, oauth_token_expires_at = ?,
                oauth_token_request_format = ?, oauth_client_auth_method = ?, health_check_url = ?,
                updated_at = strftime('%s', 'now')
            WHERE id = ?
        """,
            (
                data.get("name"),
                data.get("url"),
                data.get("api_key"),
                data.get("endpoint_type", "openai"),
                int(data.get("priority", 0)),
                1 if data.get("enabled", True) else 0,
                1 if _coerce_bool(data.get("oauth_enabled", 0)) else 0,
                data.get("oauth_grant_type", "refresh_token"),
                data.get("oauth_token_url", ""),
                data.get("oauth_client_id", ""),
                data.get("oauth_client_secret", ""),
                data.get("oauth_scope", ""),
                data.get("oauth_refresh_token", ""),
                int(data.get("oauth_token_expires_at", 0) or 0),
                data.get("oauth_token_request_format", "json"),
                data.get("oauth_client_auth_method", "client_secret_post"),
                data.get("health_check_url", ""),
                endpoint_id,
            ),
        )
        conn.commit()

    return flask_jsonify({"status": "ok"})


@flask_app.route("/endpoints/<int:endpoint_id>/delete", methods=["GET", "DELETE"])
def delete_endpoint(endpoint_id):
    """Delete endpoint."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM virtual_models WHERE endpoint_id = ?", (endpoint_id,)
        )
        cursor.execute("DELETE FROM endpoints WHERE id = ?", (endpoint_id,))
        conn.commit()

    return flask_jsonify({"status": "ok"})


@flask_app.route("/endpoints/<int:endpoint_id>/test", methods=["POST"])
def test_endpoint(endpoint_id):
    """Test endpoint connectivity."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM endpoints WHERE id = ?", (endpoint_id,))
        row = cursor.fetchone()
        endpoint = dict(row) if row else None

    if not endpoint:
        return flask_jsonify({"error": "Endpoint not found"}), 404

    try:
        headers = {}
        auth_header = resolve_endpoint_auth_header_sync(endpoint)
        if auth_header:
            headers["Authorization"] = auth_header

        endpoint_type = (endpoint.get("endpoint_type") or "").lower()
        if endpoint_type == "openai_oauth":
            check_paths = ["/backend-api/models", "/backend-api/codex/responses", "/health"]
            statuses = []
            for path in check_paths:
                resp = httpx.get(f"{endpoint['url']}{path}", headers=headers, timeout=10)
                statuses.append(f"{path}:{resp.status_code}")
                if resp.status_code in (200, 401, 403):
                    return flask_jsonify(
                        {
                            "status": "ok",
                            "endpoint_status": resp.status_code,
                            "checks": statuses,
                        }
                    )
            return flask_jsonify({"error": "OAuth endpoint health check failed", "checks": statuses}), 502

        resp = httpx.get(f"{endpoint['url']}/health", headers=headers, timeout=10)
        return flask_jsonify({"status": "ok", "endpoint_status": resp.status_code})
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 500


@flask_app.route("/endpoints/<int:endpoint_id>/models")
def fetch_endpoint_models(endpoint_id):
    """Fetch available models from endpoint."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM endpoints WHERE id = ?", (endpoint_id,))
        row = cursor.fetchone()
        endpoint = dict(row) if row else None

    if not endpoint:
        return flask_jsonify({"error": "Endpoint not found"}), 404

    try:
        headers = {}
        auth_header = resolve_endpoint_auth_header_sync(endpoint)
        if auth_header:
            headers["Authorization"] = auth_header

        endpoint_type = (endpoint.get("endpoint_type") or "").lower()
        endpoint_base = str(endpoint.get("url") or "").rstrip("/")
        if endpoint_type == "openwebui":
            model_paths = ["/api/models", "/api/v1/models", "/v1/models", "/models"]
        elif endpoint_type == "openai_oauth":
            model_paths = [
                "/backend-api/models",
                "/backend-api/codex/models",
                "/v1/models",
                "/models",
                "/api/models",
                "/api/v1/models",
            ]
        elif endpoint_type == "ollama":
            model_paths = ["/api/tags", "/v1/models", "/models", "/api/models"]
        else:
            model_paths = ["/v1/models", "/models", "/api/models", "/api/v1/models"]

        parse_errors = []
        attempt_statuses = []
        candidate_bases = [endpoint_base]
        if endpoint_type == "openai_oauth":
            try:
                oauth_response_url = _resolve_openai_oauth_response_endpoint(endpoint_base)
                oauth_models_base = oauth_response_url.rsplit("/backend-api/codex/responses", 1)[0]
                oauth_models_base = oauth_models_base.rstrip("/")
                if oauth_models_base and oauth_models_base not in candidate_bases:
                    candidate_bases.insert(0, oauth_models_base)
            except Exception:
                pass

        for base in candidate_bases:
            for path in model_paths:
                url = f"{base}{path}"
                resp = httpx.get(url, headers=headers, timeout=15)
                attempt_statuses.append(f"{url}:{resp.status_code}")
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except Exception:
                        snippet = (resp.text or "")[:180].replace("\n", " ").strip()
                        parse_errors.append(
                            f"{url}: non-JSON 200 response ({snippet})"
                        )
                        continue
                    models = data.get("models", []) or data.get("data", [])
                    model_list = sorted(
                        [m.get("id") or m.get("name") or m.get("model") for m in models]
                    )
                    return flask_jsonify({"models": model_list})

        if parse_errors:
            return flask_jsonify({"error": "; ".join(parse_errors)}), 502

        if endpoint_type == "openai_oauth":
            fallback_models = []
            try:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        SELECT DISTINCT actual_model
                        FROM virtual_models
                        WHERE endpoint_id = ?
                          AND COALESCE(TRIM(actual_model), '') <> ''
                        ORDER BY actual_model
                        """,
                        (endpoint_id,),
                    )
                    fallback_models = [r[0] for r in cursor.fetchall() if r and r[0]]
            except Exception:
                fallback_models = []

            if fallback_models:
                return flask_jsonify(
                    {
                        "models": fallback_models,
                        "warning": (
                            "Upstream OAuth endpoint does not expose a readable models route for this token. "
                            "Returning models already configured on this proxy endpoint as a fallback."
                        ),
                        "attempts": attempt_statuses,
                    }
                )

            return (
                flask_jsonify(
                    {
                        "error": (
                            "No models endpoint returned success for this OAuth-backed endpoint. "
                            "Some OpenAI/Codex OAuth tokens are scoped to chatgpt/codex backends and do not expose "
                            "OpenAI-compatible /models routes."
                        ),
                        "attempts": attempt_statuses,
                    }
                ),
                404,
            )

        return flask_jsonify({"error": "No models endpoint returned success", "attempts": attempt_statuses}), 404
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 500


@flask_app.route("/virtual-models/<int:vm_id>", methods=["PUT"])
def update_virtual_model(vm_id):
    """Update virtual model."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    data = flask_request.get_json()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE virtual_models 
            SET name = ?, endpoint_id = ?, actual_model = ?, description = ?, 
                cost_per_1m_tokens_in = ?, cost_per_1m_tokens_out = ?,
                cost_per_1m_tokens_in_cached = ?, cost_per_1m_tokens_out_cached = ?,
                disable_streaming = ?, force_non_streaming = ?, custom_headers = ?,
                enabled = ?, cache_enabled = ?, max_tokens = ?, temperature = ?, top_p = ?, system_prompt = ?,
                show_reasoning = ?,
                updated_at = strftime('%s', 'now')
            WHERE id = ?
        """,
            (
                data.get("name"),
                int(data.get("endpoint_id")),
                data.get("actual_model"),
                data.get("description"),
                data.get("cost_per_1m_tokens_in") or 0,
                data.get("cost_per_1m_tokens_out") or 0,
                data.get("cost_per_1m_tokens_in_cached") or 0,
                data.get("cost_per_1m_tokens_out_cached") or 0,
                1 if data.get("disable_streaming") else 0,
                1 if data.get("force_non_streaming") else 0,
                data.get("custom_headers") or "",
                1 if data.get("enabled", True) else 0,
                1 if data.get("cache_enabled", True) else 0,
                data.get("max_tokens") or 0,
                data.get("temperature") or 0,
                data.get("top_p") or 1.0,
                data.get("system_prompt") or "",
                1 if data.get("show_reasoning", True) else 0,
                vm_id,
            ),
        )
        conn.commit()

    if isinstance(data, dict) and "failover" in data:
        save_virtual_model_failover(vm_id, data.get("failover"))

    return flask_jsonify({"status": "ok"})


@flask_app.route("/virtual-models", methods=["POST"])
def create_virtual_model():
    """Create new virtual model."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    data = flask_request.get_json()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO virtual_models (
                name, endpoint_id, actual_model, description,
                cost_per_1m_tokens_in, cost_per_1m_tokens_out,
                cost_per_1m_tokens_in_cached, cost_per_1m_tokens_out_cached,
                disable_streaming, force_non_streaming, custom_headers,
                enabled, cache_enabled, max_tokens, temperature, top_p, system_prompt, show_reasoning
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                data.get("name"),
                int(data.get("endpoint_id")),
                data.get("actual_model"),
                data.get("description"),
                data.get("cost_per_1m_tokens_in") or 0,
                data.get("cost_per_1m_tokens_out") or 0,
                data.get("cost_per_1m_tokens_in_cached") or 0,
                data.get("cost_per_1m_tokens_out_cached") or 0,
                1 if data.get("disable_streaming") else 0,
                1 if data.get("force_non_streaming") else 0,
                data.get("custom_headers") or "",
                1 if data.get("enabled", True) else 0,
                1 if data.get("cache_enabled", True) else 0,
                data.get("max_tokens") or 0,
                data.get("temperature") or 0,
                data.get("top_p") or 1.0,
                data.get("system_prompt") or "",
                1 if data.get("show_reasoning", True) else 0,
            ),
        )
        new_vm_id = cursor.lastrowid
        conn.commit()

    if isinstance(data, dict) and "failover" in data and new_vm_id:
        save_virtual_model_failover(new_vm_id, data.get("failover"))

    return flask_jsonify({"status": "ok"})


@flask_app.route("/virtual-models/<int:vm_id>/delete", methods=["GET", "DELETE"])
def delete_virtual_model(vm_id):
    """Delete virtual model."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM virtual_models WHERE id = ?", (vm_id,))
        conn.commit()

    return flask_jsonify({"status": "ok"})

    return redirect("/virtual-models")


# API endpoints for AJAX
@flask_app.route("/api/endpoints", methods=["GET"])
def api_list_endpoints():
    """API: List endpoints."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM endpoints ORDER BY name")
        endpoints = [dict(row) for row in cursor.fetchall()]

    return flask_jsonify(endpoints)


@flask_app.route("/api/virtual-models", methods=["GET"])
def api_list_virtual_models():
    """API: List virtual models."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT vm.*, e.name as endpoint_name, e.url as endpoint_url,
                   vf.strategy as failover_strategy,
                   vf.targets as failover_targets,
                   vf.max_attempts as failover_max_attempts,
                   vf.cooldown_seconds as failover_cooldown_seconds,
                   vf.failure_threshold as failover_failure_threshold
            FROM virtual_models vm
            LEFT JOIN endpoints e ON vm.endpoint_id = e.id
            LEFT JOIN virtual_model_failover vf ON vf.virtual_model_id = vm.id
            ORDER BY vm.name
        """)
        vms = [dict(row) for row in cursor.fetchall()]

    return flask_jsonify(vms)


@flask_app.route("/api/admin/endpoints", methods=["GET"])
def api_admin_endpoints():
    """API: List all endpoints."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM endpoints ORDER BY name")
        endpoints = [dict(row) for row in cursor.fetchall()]

    return flask_jsonify(endpoints)


@flask_app.route("/api/admin/endpoints/health", methods=["GET"])
@flask_app.route("/api/admin/health", methods=["GET"])
def api_admin_health():
    """API: endpoint health status."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT e.id, e.name, e.endpoint_type,
                       eh.status, eh.failure_count, eh.last_error, eh.last_failure_at,
                       eh.circuit_until, eh.rate_limit_info
                FROM endpoints e
                LEFT JOIN endpoint_health eh ON eh.endpoint_id = e.id
                WHERE e.enabled = 1
                ORDER BY e.name ASC
                """
            )
            rows = [dict(r) for r in cursor.fetchall()]
        return flask_jsonify({"endpoints": rows})
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 500


@flask_app.route("/api/admin/activity", methods=["GET"])
@flask_app.route("/api/admin/endpoints/activity", methods=["GET"])
def api_admin_activity():
    """API: recent request activity feed."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    try:
        limit = int((flask_request.args.get("limit") or "100").strip() or 100)
    except Exception:
        limit = 100
    status_filter = (flask_request.args.get("status") or "").strip().lower()
    model_filter = (flask_request.args.get("model") or "").strip()
    path_filter = (flask_request.args.get("path") or "").strip()
    ip_filter = (flask_request.args.get("ip") or "").strip()
    inbound_api_key_id_raw = (flask_request.args.get("inbound_api_key_id") or "").strip()
    try:
        inbound_api_key_id = int(inbound_api_key_id_raw) if inbound_api_key_id_raw else None
    except Exception:
        inbound_api_key_id = None
    try:
        since = int((flask_request.args.get("since") or "0").strip() or 0)
    except Exception:
        since = 0
    include_health = (flask_request.args.get("include_health") or "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    try:
        sql, params = _build_activity_query(
            limit=limit,
            status_filter=status_filter,
            model_filter=model_filter,
            path_filter=path_filter,
            ip_filter=ip_filter,
            inbound_api_key_id=inbound_api_key_id,
            since=since,
            include_health=include_health,
        )
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = [dict(row) for row in cursor.fetchall()]
        return flask_jsonify({"items": rows, "count": len(rows)})
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 500


@flask_app.route("/api/admin/endpoints", methods=["POST"])
def api_admin_endpoints_create():
    """API: Create new endpoint."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    data = flask_request.json
    if not data:
        return flask_jsonify({"error": "No data provided"}), 400
    data = _apply_endpoint_defaults(data)

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO endpoints (
                    name, url, api_key, endpoint_type, priority, enabled,
                    oauth_enabled, oauth_grant_type, oauth_token_url,
                    oauth_client_id, oauth_client_secret, oauth_scope,
                    oauth_refresh_token, oauth_token_expires_at,
                    oauth_token_request_format, oauth_client_auth_method,
                    health_check_url
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    data.get("name"),
                    data.get("url"),
                    data.get("api_key", ""),
                    data.get("endpoint_type", "openai"),
                    int(data.get("priority", 0)),
                    1 if data.get("enabled", True) else 0,
                    1 if _coerce_bool(data.get("oauth_enabled", 0)) else 0,
                    data.get("oauth_grant_type", "refresh_token"),
                    data.get("oauth_token_url", ""),
                    data.get("oauth_client_id", ""),
                    data.get("oauth_client_secret", ""),
                    data.get("oauth_scope", ""),
                    data.get("oauth_refresh_token", ""),
                    int(data.get("oauth_token_expires_at", 0) or 0),
                    data.get("oauth_token_request_format", "json"),
                    data.get("oauth_client_auth_method", "client_secret_post"),
                    data.get("health_check_url", ""),
                ),
            )
            conn.commit()
            new_id = cursor.lastrowid

        return flask_jsonify({"id": new_id, "status": "ok"})
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 400


@flask_app.route("/api/admin/endpoints/<int:endpoint_id>", methods=["PUT"])
def api_admin_endpoints_update(endpoint_id):
    """API: Update endpoint."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    data = flask_request.json
    if not data:
        return flask_jsonify({"error": "No data provided"}), 400
    data = _apply_endpoint_defaults(data)
    data = _preserve_endpoint_oauth_secrets_on_update(endpoint_id, data)

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE endpoints 
                SET name = ?, url = ?, api_key = ?, endpoint_type = ?, priority = ?, enabled = ?,
                    oauth_enabled = ?, oauth_grant_type = ?, oauth_token_url = ?,
                    oauth_client_id = ?, oauth_client_secret = ?, oauth_scope = ?,
                    oauth_refresh_token = ?, oauth_token_expires_at = ?,
                    oauth_token_request_format = ?, oauth_client_auth_method = ?, health_check_url = ?,
                    updated_at = strftime('%s', 'now')
                WHERE id = ?
            """,
                (
                    data.get("name"),
                    data.get("url"),
                    data.get("api_key", ""),
                    data.get("endpoint_type", "openai"),
                    int(data.get("priority", 0)),
                    1 if data.get("enabled", True) else 0,
                    1 if _coerce_bool(data.get("oauth_enabled", 0)) else 0,
                    data.get("oauth_grant_type", "refresh_token"),
                    data.get("oauth_token_url", ""),
                    data.get("oauth_client_id", ""),
                    data.get("oauth_client_secret", ""),
                    data.get("oauth_scope", ""),
                    data.get("oauth_refresh_token", ""),
                    int(data.get("oauth_token_expires_at", 0) or 0),
                    data.get("oauth_token_request_format", "json"),
                    data.get("oauth_client_auth_method", "client_secret_post"),
                    data.get("health_check_url", ""),
                    endpoint_id,
                ),
            )
            conn.commit()

        return flask_jsonify({"status": "ok"})
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 400


@flask_app.route("/api/admin/endpoints/<int:endpoint_id>", methods=["DELETE"])
def api_admin_endpoints_delete(endpoint_id):
    """API: Delete endpoint."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM virtual_models WHERE endpoint_id = ?", (endpoint_id,)
            )
            cursor.execute("DELETE FROM endpoints WHERE id = ?", (endpoint_id,))
            conn.commit()

        return flask_jsonify({"status": "ok"})
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 400


@flask_app.route("/api/admin/oauth/openai/import-codex", methods=["POST"])
def api_admin_openai_oauth_import_codex():
    """Import OAuth fields from local Codex/ChatGPT auth.json."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    body = flask_request.json or {}
    explicit_path = body.get("path") if isinstance(body, dict) else None
    searched_paths = _candidate_codex_auth_paths(explicit_path)

    chosen_path = None
    parsed = None
    parse_error = None
    for path in searched_paths:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                parsed = json.load(f)
            chosen_path = path
            break
        except Exception as exc:
            parse_error = str(exc)

    if parsed is None:
        return flask_jsonify(
            {
                "error": "No readable auth.json found",
                "searched_paths": searched_paths,
                "parse_error": parse_error,
            }
        ), 404

    extracted = _extract_oauth_fields_from_auth_json(parsed)
    if not extracted.get("oauth_refresh_token"):
        return flask_jsonify(
            {
                "error": "auth.json found, but no refresh token could be extracted",
                "source_path": chosen_path,
            }
        ), 400

    payload = {
        "endpoint_type": "openai_oauth",
        "url": "https://api.openai.com",
        "oauth_enabled": True,
        "oauth_grant_type": "refresh_token",
        "oauth_token_url": extracted.get("oauth_token_url")
        or "https://auth.openai.com/oauth/token",
        "oauth_client_id": extracted.get("oauth_client_id", ""),
        "oauth_client_secret": extracted.get("oauth_client_secret", ""),
        "oauth_refresh_token": extracted.get("oauth_refresh_token", ""),
        "oauth_scope": extracted.get("oauth_scope", ""),
        "oauth_token_request_format": "json",
        "oauth_client_auth_method": "client_secret_post",
    }

    return flask_jsonify({
        "status": "ok",
        "source_path": chosen_path,
        "fields": payload,
    })


@flask_app.route("/api/admin/oauth/openai/start-web-auth", methods=["POST"])
def api_admin_openai_oauth_start_web_auth():
    """Start interactive OpenAI OAuth (authorization code + PKCE)."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    _cleanup_oauth_web_cache()

    body = flask_request.json or {}
    if not isinstance(body, dict):
        body = {}

    client_id = (body.get("oauth_client_id") or "").strip() or OPENAI_WEB_OAUTH_DEFAULT_CLIENT_ID
    authorize_url = (
        (body.get("oauth_authorize_url") or "").strip()
        or OPENAI_WEB_OAUTH_AUTHORIZE_URL
    )
    token_url = (body.get("oauth_token_url") or "").strip() or OPENAI_WEB_OAUTH_TOKEN_URL
    scope = (
        (body.get("oauth_scope") or "").strip()
        or "openid profile email offline_access"
    )
    client_secret = (body.get("oauth_client_secret") or "").strip()

    callback_mode = (body.get("callback_mode") or "local_paste").strip().lower()
    if callback_mode == "proxy_callback":
        callback_base = _resolve_oauth_callback_base()
        redirect_uri = f"{callback_base}/api/admin/oauth/openai/callback"
    else:
        redirect_uri = (
            (body.get("redirect_uri") or "").strip()
            or OPENAI_WEB_OAUTH_DEFAULT_REDIRECT_URI
        )

    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = _pkce_s256(verifier)

    _oauth_web_sessions[state] = {
        "created_at": int(time.time()),
        "client_id": client_id,
        "client_secret": client_secret,
        "verifier": verifier,
        "token_url": token_url,
        "scope": scope,
        "redirect_uri": redirect_uri,
        "authorize_url": authorize_url,
    }

    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": OPENAI_WEB_OAUTH_ORIGINATOR,
    }
    auth_url = f"{authorize_url}?{urllib.parse.urlencode(query)}"
    return flask_jsonify(
        {
            "status": "ok",
            "state": state,
            "authorization_url": auth_url,
            "redirect_uri": redirect_uri,
            "callback_mode": callback_mode,
            "instructions": "Complete login in browser, then paste full redirect URL (or code) into the form and click Complete Web OAuth.",
        }
    )


@flask_app.route("/api/admin/oauth/openai/callback", methods=["GET"])
def api_admin_openai_oauth_callback():
    """OAuth callback endpoint for interactive OpenAI OAuth flow."""
    _cleanup_oauth_web_cache()

    state = (flask_request.args.get("state") or "").strip()
    code = (flask_request.args.get("code") or "").strip()
    auth_error = (flask_request.args.get("error") or "").strip()
    auth_error_description = (flask_request.args.get("error_description") or "").strip()

    if not state:
        return Response("Missing state", status=400)

    session = _oauth_web_sessions.pop(state, None)
    if not session:
        return Response("OAuth session expired or invalid state", status=400)

    if auth_error:
        _oauth_web_results[state] = {
            "created_at": int(time.time()),
            "status": "error",
            "error": f"{auth_error}: {auth_error_description}" if auth_error_description else auth_error,
        }
    elif not code:
        _oauth_web_results[state] = {
            "created_at": int(time.time()),
            "status": "error",
            "error": "Missing authorization code",
        }
    else:
        token_url = session.get("token_url") or OPENAI_WEB_OAUTH_TOKEN_URL
        token_data, err_msg, err_details = _exchange_openai_oauth_code(
            token_url=token_url,
            code=code,
            client_id=session.get("client_id", ""),
            redirect_uri=session.get("redirect_uri", ""),
            code_verifier=session.get("verifier", ""),
            client_secret=session.get("client_secret", ""),
        )
        if err_msg:
            _oauth_web_results[state] = {
                "created_at": int(time.time()),
                "status": "error",
                "error": err_msg,
                "details": err_details,
            }
        else:
            refresh_token = str((token_data or {}).get("refresh_token") or "").strip()
            access_token = str((token_data or {}).get("access_token") or "").strip()
            expires_in = (token_data or {}).get("expires_in")
            try:
                expires_in_int = int(expires_in or 0)
            except Exception:
                expires_in_int = 0
            scope = str((token_data or {}).get("scope") or session.get("scope") or "").strip()
            if not refresh_token and not access_token:
                _oauth_web_results[state] = {
                    "created_at": int(time.time()),
                    "status": "error",
                    "error": "Token exchange succeeded but no token fields were returned",
                }
            else:
                _oauth_web_results[state] = {
                    "created_at": int(time.time()),
                    "status": "ok",
                    "fields": {
                        "endpoint_type": "openai_oauth",
                        "url": "https://api.openai.com",
                        "oauth_enabled": True,
                        "oauth_grant_type": "refresh_token",
                        "oauth_token_url": token_url,
                        "oauth_client_id": session.get("client_id", ""),
                        "oauth_client_secret": session.get("client_secret", ""),
                        "oauth_refresh_token": refresh_token,
                        "oauth_scope": scope,
                        "oauth_token_request_format": "json",
                        "oauth_client_auth_method": "client_secret_post",
                        "oauth_token_expires_at": int(time.time()) + expires_in_int,
                    },
                }

    message_payload = {"type": "openai-oauth-web-complete", "state": state}
    html = f"""
<!doctype html>
<html>
<head><meta charset=\"utf-8\"><title>OAuth Complete</title></head>
<body style=\"font-family: sans-serif; padding: 20px;\">
  <h3>OAuth flow completed</h3>
  <p>You can close this window and return to Serverless Proxy.</p>
  <script>
    (function() {{
      var payload = {json.dumps(message_payload)};
      try {{
        if (window.opener) {{
          window.opener.postMessage(payload, window.location.origin);
        }}
      }} catch (e) {{}}
      setTimeout(function() {{ window.close(); }}, 200);
    }})();
  </script>
</body>
</html>
"""
    return flask_app.response_class(html, mimetype="text/html")


@flask_app.route("/api/admin/oauth/openai/auth-result", methods=["GET"])
def api_admin_openai_oauth_auth_result():
    """Poll OAuth completion result by state."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    _cleanup_oauth_web_cache()
    state = (flask_request.args.get("state") or "").strip()
    if not state:
        return flask_jsonify({"error": "Missing state"}), 400

    result = _oauth_web_results.pop(state, None)
    if not result:
        return flask_jsonify({"status": "pending"})
    return flask_jsonify(result)


@flask_app.route("/api/admin/oauth/openai/complete-web-auth", methods=["POST"])
def api_admin_openai_oauth_complete_web_auth():
    """Complete OAuth by pasting redirect URL or auth code from browser."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    _cleanup_oauth_web_cache()
    body = flask_request.json or {}
    if not isinstance(body, dict):
        body = {}

    state = (body.get("state") or "").strip()
    input_text = (body.get("redirect_url_or_code") or "").strip()
    if not state:
        return flask_jsonify({"error": "Missing state"}), 400
    if not input_text:
        return flask_jsonify({"error": "Paste redirect URL or code first"}), 400

    session = _oauth_web_sessions.pop(state, None)
    if not session:
        return flask_jsonify({"error": "OAuth session expired or invalid state"}), 400

    code, parsed_state, parse_err = _extract_code_and_state_from_redirect_input(input_text)
    if parse_err:
        return flask_jsonify({"error": parse_err}), 400
    if parsed_state and parsed_state != state:
        return flask_jsonify({"error": "State mismatch in pasted redirect URL"}), 400

    token_url = session.get("token_url") or OPENAI_WEB_OAUTH_TOKEN_URL
    token_data, err_msg, err_details = _exchange_openai_oauth_code(
        token_url=token_url,
        code=code,
        client_id=session.get("client_id", ""),
        redirect_uri=session.get("redirect_uri", ""),
        code_verifier=session.get("verifier", ""),
        client_secret=session.get("client_secret", ""),
    )
    if err_msg:
        return flask_jsonify({"error": err_msg, "details": err_details}), 400

    refresh_token = str((token_data or {}).get("refresh_token") or "").strip()
    access_token = str((token_data or {}).get("access_token") or "").strip()
    if not refresh_token and not access_token:
        return flask_jsonify({"error": "Token exchange returned no usable token fields"}), 400

    expires_in = (token_data or {}).get("expires_in")
    try:
        expires_in_int = int(expires_in or 0)
    except Exception:
        expires_in_int = 0

    fields = {
        "endpoint_type": "openai_oauth",
        "url": "https://api.openai.com",
        "oauth_enabled": True,
        "oauth_grant_type": "refresh_token",
        "oauth_token_url": token_url,
        "oauth_client_id": session.get("client_id", ""),
        "oauth_client_secret": session.get("client_secret", ""),
        "oauth_refresh_token": refresh_token,
        "oauth_scope": str((token_data or {}).get("scope") or session.get("scope") or "").strip(),
        "oauth_token_request_format": "json",
        "oauth_client_auth_method": "client_secret_post",
        "oauth_token_expires_at": int(time.time()) + expires_in_int,
    }
    return flask_jsonify({"status": "ok", "fields": fields})


@flask_app.route("/api/admin/virtual-models", methods=["GET"])
def api_admin_virtual_models():
    """API: List all virtual models."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT vm.*, e.name as endpoint_name, e.url as endpoint_url,
                   vf.strategy as failover_strategy,
                   vf.targets as failover_targets,
                   vf.max_attempts as failover_max_attempts,
                   vf.cooldown_seconds as failover_cooldown_seconds,
                   vf.failure_threshold as failover_failure_threshold
            FROM virtual_models vm
            LEFT JOIN endpoints e ON vm.endpoint_id = e.id
            LEFT JOIN virtual_model_failover vf ON vf.virtual_model_id = vm.id
            ORDER BY vm.name
        """)
        vms = [dict(row) for row in cursor.fetchall()]

    return flask_jsonify(vms)


@flask_app.route("/api/admin/virtual-models/<int:vm_id>/failover", methods=["GET"])
def api_admin_virtual_model_failover_get(vm_id):
    """API: Get failover config for a virtual model."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    config = get_failover_config(vm_id)
    if not config:
        return flask_jsonify({"virtual_model_id": vm_id, "enabled": False})

    targets = []
    try:
        targets = json.loads(config.get("targets") or "[]")
        if not isinstance(targets, list):
            targets = []
    except Exception:
        targets = []

    return flask_jsonify(
        {
            "virtual_model_id": vm_id,
            "enabled": True,
            "strategy": config.get("strategy"),
            "targets": targets,
            "max_attempts": config.get("max_attempts"),
            "cooldown_seconds": config.get("cooldown_seconds"),
            "failure_threshold": config.get("failure_threshold"),
        }
    )


@flask_app.route("/api/admin/virtual-models/<int:vm_id>/failover", methods=["PUT", "POST"])
def api_admin_virtual_model_failover_upsert(vm_id):
    """API: Create or update failover config for a virtual model."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    data = flask_request.json or {}
    try:
        save_virtual_model_failover(vm_id, data)
        return flask_jsonify({"status": "ok"})
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 400


@flask_app.route("/api/admin/virtual-models/<int:vm_id>/failover", methods=["DELETE"])
def api_admin_virtual_model_failover_delete(vm_id):
    """API: Delete failover config for a virtual model."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    try:
        save_virtual_model_failover(vm_id, {"enabled": False})
        return flask_jsonify({"status": "ok"})
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 400


def save_setting(key, value):
    """Save a setting to the database."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, strftime('%s', 'now'))",
                (key, value),
            )
            conn.commit()
        return True
    except Exception as e:
        print(f"Error saving setting {key}: {e}")
        return False


def _generate_inbound_api_key() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    full_key = f"spk_{raw}"
    return full_key, full_key[:12]


def _hash_inbound_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _list_inbound_api_keys() -> list[dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, label, key_prefix, enabled, notes, created_at, updated_at,
                   last_used_at, last_used_ip, last_used_user_agent, revoked_at
            FROM inbound_api_keys
            ORDER BY created_at DESC, id DESC
            """
        )
        return [dict(row) for row in cursor.fetchall()]


def _get_all_settings() -> dict[str, str]:
    settings: dict[str, str] = {}
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM settings")
        for row in cursor.fetchall():
            settings[row["key"]] = row["value"]
    settings.setdefault("display_timezone", "Server Local Time")
    settings["server_timezone"] = _get_server_timezone_name()
    settings["display_timezone_effective"] = get_display_timezone_name()
    settings["common_timezones"] = COMMON_TIMEZONES
    return settings


@flask_app.route("/api/admin/inbound-api-keys", methods=["GET"])
def api_admin_inbound_api_keys_list():
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    try:
        return flask_jsonify({"items": _list_inbound_api_keys()})
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 500


@flask_app.route("/api/admin/inbound-api-keys", methods=["POST"])
def api_admin_inbound_api_keys_create():
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    data = flask_request.json or {}
    label = str(data.get("label") or "").strip()
    notes = str(data.get("notes") or "").strip()
    if not label:
        return flask_jsonify({"error": "Label is required"}), 400

    full_key, key_prefix = _generate_inbound_api_key()
    key_hash = _hash_inbound_api_key(full_key)

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO inbound_api_keys (label, key_prefix, key_hash, enabled, notes)
                VALUES (?, ?, ?, 1, ?)
                """,
                (label, key_prefix, key_hash, notes),
            )
            conn.commit()
            new_id = cursor.lastrowid
        return flask_jsonify(
            {
                "id": new_id,
                "label": label,
                "key_prefix": key_prefix,
                "api_key": full_key,
            }
        )
    except sqlite3.IntegrityError:
        return flask_jsonify({"error": "Label already exists"}), 400
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 500


@flask_app.route("/api/admin/inbound-api-keys/<int:key_id>", methods=["PUT"])
def api_admin_inbound_api_keys_update(key_id):
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    data = flask_request.json or {}
    updates = []
    params = []
    if "enabled" in data:
        updates.append("enabled = ?")
        params.append(1 if data.get("enabled") else 0)
        updates.append("updated_at = strftime('%s', 'now')")
    if "notes" in data:
        updates.append("notes = ?")
        params.append(str(data.get("notes") or "").strip())
        if "updated_at = strftime('%s', 'now')" not in updates:
            updates.append("updated_at = strftime('%s', 'now')")
    if not updates:
        return flask_jsonify({"error": "No updates provided"}), 400

    params.append(key_id)
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE inbound_api_keys SET {', '.join(updates)} WHERE id = ?",
                tuple(params),
            )
            conn.commit()
        return flask_jsonify({"status": "ok"})
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 500


@flask_app.route("/api/admin/inbound-api-keys/<int:key_id>", methods=["DELETE"])
def api_admin_inbound_api_keys_delete(key_id):
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM inbound_api_keys WHERE id = ?", (key_id,))
            conn.commit()
        return flask_jsonify({"status": "ok"})
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 500


@flask_app.route("/session/validate", methods=["GET"])
def session_validate():
    """Local session validation - returns valid if auth disabled."""
    if not is_auth_enabled():
        return flask_jsonify(
            {
                "valid": True,
                "user": "admin",
                "auth_enabled": False,
                "use_ai_queue": get_setting("use_ai_queue", "false") == "true",
            }
        )

    # Otherwise check with remote AIMENU
    try:
        cookies = flask_request.cookies
        resp = httpx.get(
            f"{AIMENU_URL}/session/validate", cookies=dict(cookies), timeout=5
        )
        result = resp.json()
        print(f"DEBUG: AIMENU response = {result}")
        result["auth_enabled"] = True
        result["use_ai_queue"] = get_setting("use_ai_queue", "false") == "true"
        return flask_jsonify(result)
    except Exception as e:
        return flask_jsonify({"valid": False})


@flask_app.route("/api/admin/settings", methods=["GET"])
def api_admin_settings_get():
    """API: Get all settings."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    return flask_jsonify(_get_all_settings())


ENV_KEY_MAP = {
    "api_port": "API_PORT",
    "flask_port": "FLASK_PORT",
    "aimenu_url": "AIMENU_URL",
    "use_ai_queue": "USE_AI_QUEUE",
    "ai_queue_url": "AI_QUEUE_URL",
    "payload_audit_enabled": "PAYLOAD_AUDIT_ENABLED",
}


def update_env_file(key, value):
    """Update a setting in the .env file."""
    env_key = ENV_KEY_MAP.get(key)
    if not env_key:
        return False

    env_path = "/app/.env"
    if not os.path.exists(env_path):
        env_path = ".env"

    try:
        lines = []
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                lines = f.readlines()

        found = False
        new_lines = []
        for line in lines:
            if line.strip().startswith(f"{env_key}="):
                new_lines.append(f"{env_key}={value}\n")
                found = True
            else:
                new_lines.append(line)

        if not found:
            new_lines.append(f"{env_key}={value}\n")

        with open(env_path, "w") as f:
            f.writelines(new_lines)

        return True
    except Exception as e:
        print(f"Error updating .env file: {e}")
        return False


@flask_app.route("/api/admin/usage", methods=["GET"])
def api_admin_usage_proxy():
    """Proxy /api/admin/usage requests to FastAPI."""
    import httpx

    fastapi_url = f"http://127.0.0.1:{get_api_port()}/api/admin/usage"
    query_string = flask_request.query_string.decode("utf-8")
    if query_string:
        fastapi_url += "?" + query_string

    try:
        response = httpx.get(fastapi_url, timeout=30.0)
        return flask_jsonify(response.json()), response.status_code
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 500


@flask_app.route("/api/admin/settings", methods=["POST"])
def api_admin_settings_post():
    """API: Update settings."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    data = flask_request.json
    if not data:
        return flask_jsonify({"error": "No data provided"}), 400

    for key, value in data.items():
        save_setting(key, value)
        update_env_file(key, value)

    port_changed = "api_port" in data or "flask_port" in data

    if port_changed:
        restart_message = "Port changed. Attempting to restart..."
        try:
            import subprocess

            result = subprocess.run(
                ["docker", "restart", "serverless-proxy"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                restart_message = "Settings saved. Server restarting..."
            else:
                restart_message = "Settings saved. Please restart the container manually: docker restart serverless-proxy"
        except Exception as e:
            restart_message = f"Settings saved. Please restart manually: docker restart serverless-proxy"
    else:
        restart_message = "Settings saved successfully."

    return flask_jsonify(
        {
            "success": True,
            "message": restart_message,
        }
    )


# ==========================================
# Tool Patterns API Endpoints
# ==========================================


@flask_app.route("/api/admin/tool-patterns", methods=["GET"])
def api_admin_tool_patterns():
    """API: List all tool patterns."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tool_patterns ORDER BY priority DESC")
        patterns = [dict(row) for row in cursor.fetchall()]

    return flask_jsonify(patterns)


@flask_app.route("/tool-patterns", methods=["GET"])
def tool_patterns_list_legacy():
    """Legacy alias for listing tool patterns."""
    return api_admin_tool_patterns()


@flask_app.route("/endpoints/patterns", methods=["GET"])
def tool_patterns_list_endpoints_alias():
    """Endpoints-prefixed alias for environments proxying /endpoints* only."""
    return api_admin_tool_patterns()


@flask_app.route("/api/admin/tool-patterns", methods=["POST"])
def api_admin_tool_patterns_create():
    """API: Create a new tool pattern."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    data = flask_request.json
    if not data:
        return flask_jsonify({"error": "No data provided"}), 400

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO tool_patterns 
                (pattern_name, pattern_type, regex_pattern, tool_name, tool_name_group, 
                 tool_name_json_path, tool_name_mapping, parameter_mapping, enabled, priority)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    data.get("pattern_name"),
                    data.get("pattern_type", "fence"),
                    data.get("regex_pattern", ""),
                    data.get("tool_name"),
                    data.get("tool_name_group"),
                    data.get("tool_name_json_path"),
                    data.get("tool_name_mapping", "{}"),
                    data.get("parameter_mapping", "{}"),
                    1 if data.get("enabled", True) else 0,
                    int(data.get("priority", 50)),
                ),
            )
            conn.commit()
            new_id = cursor.lastrowid

        load_tool_patterns()

        return flask_jsonify({"id": new_id, "status": "ok"})
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 400


@flask_app.route("/tool-patterns", methods=["POST"])
def tool_patterns_create_legacy():
    """Legacy alias for creating tool patterns."""
    return api_admin_tool_patterns_create()


@flask_app.route("/endpoints/patterns", methods=["POST"])
def tool_patterns_create_endpoints_alias():
    """Endpoints-prefixed alias for environments proxying /endpoints* only."""
    return api_admin_tool_patterns_create()


@flask_app.route("/api/admin/tool-patterns/<int:pattern_id>", methods=["PUT"])
def api_admin_tool_patterns_update(pattern_id):
    """API: Update a tool pattern."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    data = flask_request.json
    if not data:
        return flask_jsonify({"error": "No data provided"}), 400

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE tool_patterns 
                SET pattern_name = ?, pattern_type = ?, regex_pattern = ?, tool_name = ?,
                    tool_name_group = ?, tool_name_json_path = ?, tool_name_mapping = ?,
                    parameter_mapping = ?, enabled = ?, priority = ?,
                    updated_at = strftime('%s', 'now')
                WHERE id = ?
            """,
                (
                    data.get("pattern_name"),
                    data.get("pattern_type", "fence"),
                    data.get("regex_pattern", ""),
                    data.get("tool_name"),
                    data.get("tool_name_group"),
                    data.get("tool_name_json_path"),
                    data.get("tool_name_mapping", "{}"),
                    data.get("parameter_mapping", "{}"),
                    1 if data.get("enabled", True) else 0,
                    int(data.get("priority", 50)),
                    pattern_id,
                ),
            )
            conn.commit()

        load_tool_patterns()

        return flask_jsonify({"status": "ok"})
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 400


@flask_app.route("/tool-patterns/<int:pattern_id>", methods=["PUT"])
def tool_patterns_update_legacy(pattern_id):
    """Legacy alias for updating tool patterns."""
    return api_admin_tool_patterns_update(pattern_id)


@flask_app.route("/endpoints/patterns/<int:pattern_id>", methods=["PUT"])
def tool_patterns_update_endpoints_alias(pattern_id):
    """Endpoints-prefixed alias for environments proxying /endpoints* only."""
    return api_admin_tool_patterns_update(pattern_id)


@flask_app.route("/api/admin/tool-patterns/<int:pattern_id>", methods=["DELETE"])
def api_admin_tool_patterns_delete(pattern_id):
    """API: Delete a tool pattern."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM tool_patterns WHERE id = ?", (pattern_id,))
            conn.commit()

        load_tool_patterns()

        return flask_jsonify({"status": "ok"})
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 400


@flask_app.route("/tool-patterns/<int:pattern_id>", methods=["DELETE"])
def tool_patterns_delete_legacy(pattern_id):
    """Legacy alias for deleting tool patterns."""
    return api_admin_tool_patterns_delete(pattern_id)


@flask_app.route("/endpoints/patterns/<int:pattern_id>", methods=["DELETE"])
def tool_patterns_delete_endpoints_alias(pattern_id):
    """Endpoints-prefixed alias for environments proxying /endpoints* only."""
    return api_admin_tool_patterns_delete(pattern_id)


if __name__ == "__main__":
    import uvicorn
    import threading

    # Run Flask in background thread
    def run_flask():
        flask_app.run(
            host="0.0.0.0", port=get_flask_port(), debug=False, use_reloader=False
        )

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Run FastAPI
    uvicorn.run(app, host="0.0.0.0", port=get_api_port())
