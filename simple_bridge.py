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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request as FastAPIRequest
import httpx
import os
import time
import json
import asyncio
import re
import sqlite3
from abc import ABC, abstractmethod
from typing import Any, Optional
from contextlib import contextmanager

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


def get_api_port():
    """Get API port from DB, fallback to env var or default."""
    return int(get_setting("api_port", os.getenv("API_PORT", "8002")))


def get_flask_port():
    """Get Flask/Admin port from DB, fallback to env var or default."""
    return int(get_setting("flask_port", os.getenv("FLASK_PORT", "5001")))


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
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                cost_estimate REAL DEFAULT 0,
                response_time_ms INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (endpoint_id) REFERENCES endpoints(id)
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

        # Create indexes for performance
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_usage_virtual_model ON request_usage(virtual_model)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_usage_created_at ON request_usage(created_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_embedding_usage_virtual_model ON embedding_usage(virtual_model)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_embedding_usage_created_at ON embedding_usage(created_at)"
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


def log_chat_usage(virtual_model, endpoint_name, endpoint_id, usage, response_time_ms):
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
                (virtual_model, endpoint_name, endpoint_id, request_type, 
                 prompt_tokens, completion_tokens, total_tokens, 
                 cached_input_tokens, cache_creation_tokens,
                 cost_estimate, cost_in, cost_out, cached_cost_estimate, response_time_ms)
                VALUES (?, ?, ?, 'chat', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    virtual_model,
                    endpoint_name,
                    endpoint_id,
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
        # Log error but don't fail request
        print(f"Error logging chat usage: {e}")


def log_completion_usage(
    virtual_model, endpoint_name, endpoint_id, usage, response_time_ms
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
                    (virtual_model, endpoint_name, endpoint_id, request_type, 
                     prompt_tokens, completion_tokens, total_tokens, 
                     cached_input_tokens, cache_creation_tokens,
                     cost_estimate, cost_in, cost_out, cached_cost_estimate, response_time_ms)
                    VALUES (?, ?, ?, 'completion', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        virtual_model,
                        endpoint_name,
                        endpoint_id,
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
                (virtual_model, endpoint_name, endpoint_id, 
                 input_tokens, output_tokens, total_tokens, 
                 cost_estimate, cost_in, cost_out, response_time_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    virtual_model,
                    endpoint_name,
                    endpoint_id,
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

                if response.status_code != 200:
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
        pass

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
                       e.endpoint_type as endpoint_type
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


def create_backend_from_virtual_model(vm: dict) -> LLMBackend:
    """Create a backend from virtual model configuration."""
    endpoint_type = vm.get("endpoint_type", "openai")

    class VirtualModelBackend(LLMBackend):
        def __init__(self):
            self.url = vm["endpoint_url"]
            self.api_key = vm.get("endpoint_api_key", "")
            self.model = vm["actual_model"]
            self.endpoint_type = endpoint_type
            self.disable_streaming = vm.get("disable_streaming", 0) == 1
            self.virtual_model_name = vm.get("name")
            self.custom_headers = vm.get("custom_headers", "")
            self.show_reasoning = vm.get("show_reasoning", 1) == 1

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
            # Force non-streaming if disabled in model config
            if self.disable_streaming:
                stream = False

            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            # Add X-Source header for tracking - use incoming from kwargs if available
            incoming_src = kwargs.get("_incoming_source", "serverless-proxy")
            headers["X-Source"] = incoming_src

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

            for k, v in kwargs.items():
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
            elif self.endpoint_type == "openai":
                endpoint = f"{self.url}/v1/chat/completions"
            elif self.endpoint_type == "together":
                endpoint = f"{self.url}/v1/chat/completions"
            elif self.endpoint_type == "deepinfra":
                endpoint = f"{self.url}/v1/openai/chat/completions"
            elif self.endpoint_type == "anthropic":
                endpoint = f"{self.url}/v1/messages"
            elif self.endpoint_type == "queue":
                endpoint = f"{self.url}/v1/chat/completions"
            else:
                endpoint = f"{self.url}/v1/chat/completions"

            timeout = 1200.0 if stream else 300.0
            print(
                f"[VM_BACKEND] Calling {endpoint}, model={payload.get('model')}, stream={stream}"
            )

            async with httpx.AsyncClient(timeout=timeout) as client:
                try:
                    response = await client.post(
                        endpoint, headers=headers, json=payload
                    )
                except Exception as e:
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
                    return (
                        None,
                        {
                            "error": {
                                "message": f"Error: {response.text}",
                                "type": "internal_server_error",
                            }
                        },
                        response.status_code,
                    )

                try:
                    if stream:
                        # For streaming, return the raw text (SSE format)
                        return {"_stream_data": response.text}, None, 200
                    result = response.json()
                    # Debug: log response details
                    choices = result.get("choices", [])
                    if choices:
                        first_choice = choices[0]
                        msg = first_choice.get("message", {})
                        content = msg.get("content") or ""
                        tc = msg.get("tool_calls") or []
                        finish = first_choice.get("finish_reason")
                        print(
                            f"[VM_BACKEND_RESP] Content: '{content[:100]}', Tool calls: {len(tc)}, Finish: {finish}"
                        )
                        if tc:
                            print(f"[VM_BACKEND_RESP] Tool calls raw: {tc[:500]}")
                    else:
                        print(
                            f"[VM_BACKEND_RESP] No choices in response: {result.keys()}"
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

            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{self.url}/v1/embeddings", headers=headers, json=payload
                )

                if response.status_code != 200:
                    return (
                        None,
                        {"error": {"message": f"Error: {response.text}"}},
                        response.status_code,
                    )

                return response.json(), None, 200

        async def health_check(self) -> bool:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(f"{self.url}/health")
                    return response.status_code == 200
            except Exception:
                return False

    return VirtualModelBackend()


def get_backend(model_name: str = None) -> LLMBackend:
    """Get the configured backend based on virtual model or environment variables."""

    # First check if model_name is a virtual model
    if model_name:
        vm = get_virtual_model(model_name)
        if vm:
            return create_backend_from_virtual_model(vm)

    # Check database for use_ai_queue setting, fallback to env var
    use_queue = (
        get_setting("use_ai_queue", os.getenv("USE_AI_QUEUE", "false")) == "true"
    )

    if use_queue:
        return AIQueueBackend()
    else:
        return RunPodBackend()


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
                        if pattern["type"] == "bracket":
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


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions endpoint."""
    import time as time_module

    start_time = time_module.time()

    data = await request.json()

    messages = data.get("messages", [])
    model = data.get("model")
    temperature = data.get("temperature", 0.7)
    max_tokens = data.get("max_tokens", 256)
    top_p = data.get("top_p", 1.0)
    stream = data.get("stream", False)
    tools = data.get("tools", [])

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
    if hasattr(backend, "virtual_model_name"):
        virtual_model = backend.virtual_model_name

    # Additional OpenAI parameters
    extra_params = {
        "stop": data.get("stop"),
        "presence_penalty": data.get("presence_penalty"),
        "frequency_penalty": data.get("frequency_penalty"),
        "logit_bias": data.get("logit_bias"),
        "user": data.get("user"),
        "tool_choice": data.get("tool_choice"),
        "response_format": data.get("response_format"),
        "seed": data.get("seed"),
    }
    # Only add parallel_tool_calls if tools are present
    if tools and data.get("parallel_tool_calls") is not None:
        extra_params["parallel_tool_calls"] = data.get("parallel_tool_calls")

    # Get incoming X-Source to forward
    incoming_source = request.headers.get("x-source", "serverless-proxy")

    # Preserve original stream request from client (before any modifications)
    original_stream = stream

    # Compat mode: if client asks for stream but doesn't accept SSE, force non-streaming
    # BUT still track original request to return proper response format to client
    accept_header = request.headers.get("accept", "")
    user_agent = request.headers.get("user-agent", "")
    wants_sse = "text/event-stream" in accept_header.lower()
    # Also accept */* as valid - it means client accepts anything
    accepts_all = accept_header.strip() == "*/*"
    ua_lower = user_agent.lower()
    is_openai_js = "openai/js" in ua_lower or "openclaw" in ua_lower
    is_opencode = "opencode" in ua_lower or "ai-sdk" in ua_lower
    if stream and (is_openai_js or is_opencode) and not wants_sse and not accepts_all:
        print(
            f"[COMPAT] Forcing non-streaming: accept={accept_header} user-agent={user_agent}",
            flush=True,
        )
        stream = False

    # Call backend (handles both AI Queue and RunPod)
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
        return JSONResponse(status_code=status_code, content=error)

    # For RunPod backend, extract result from wrapper
    result = backend_result
    if "result" in backend_result:
        result = backend_result["result"]

    # Check if this is a streaming response from a virtual model backend
    if "_stream_data" in result:
        # Parse existing SSE data instead of making another request
        stream_data = result["_stream_data"]
        full_content = ""
        full_reasoning = ""
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        stream_tool_calls = []
        has_tool_calls = False
        finish_reason = None

        for line in stream_data.split("\n"):
            if line.strip() and line.startswith("data: "):
                if line[6:].strip() == "[DONE]":
                    continue
                try:
                    chunk = json.loads(line[6:])
                    if "choices" in chunk and chunk["choices"]:
                        delta = chunk["choices"][0].get("delta", {})
                        tc = delta.get("tool_calls")
                        if tc:
                            stream_tool_calls.extend(tc)
                            has_tool_calls = True
                        # Handle content and reasoning separately
                        # When tool calls are present, content is skipped to avoid leaking thinking
                        # But reasoning_content should still be captured
                        if not has_tool_calls:
                            if delta.get("content"):
                                full_content += delta["content"]
                        if delta.get("reasoning_content"):
                            full_reasoning += delta["reasoning_content"]
                        finish_reason = chunk["choices"][0].get("finish_reason")
                    if "usage" in chunk and chunk["usage"]:
                        usage = chunk["usage"]
                except:
                    pass

        extracted_tc, text_content = process_content(full_content)
        if stream_tool_calls and not extracted_tc:
            extracted_tc = stream_tool_calls

        print(
            f"[DEBUG] Parsed stream: content='{full_content[:50]}...' if full_content else '(empty)', reasoning='{full_reasoning[:50]}...' if full_reasoning else '(empty)', tc={len(extracted_tc) if extracted_tc else 0}, finish={finish_reason}",
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

        # Generate proper SSE with extracted tool calls
        job_id = f"chat-{int(time_module.time())}"

        # Log usage for streaming requests BEFORE returning
        response_time_ms = int((time_module.time() - start_time) * 1000)
        log_chat_usage(model, None, None, usage, response_time_ms)

        async def stream_generator():
            async for chunk_data in _generate_sse(
                job_id=job_id,
                model=model,
                tool_calls_data=extracted_tc,
                text_content=text_content,
            ):
                yield chunk_data

        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    # Extract content from response
    content = ""
    reasoning_content = ""
    tool_calls_data = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    if "choices" in result and result["choices"]:
        choice = result["choices"][0]
        message = choice.get("message", {})
        content = message.get("content", "") or ""
        reasoning_content = message.get("reasoning_content", "") or ""
        tool_calls_data = message.get("tool_calls", []) or []
        usage = result.get("usage", usage)

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

    # Process content to extract tool calls
    extracted_tc, text_content = process_content(content)
    if extracted_tc:
        tool_calls_data = extracted_tc
    elif not tool_calls_data:
        text_content = text_content or content

    job_id = result.get("id", f"chat-{int(time_module.time())}")

    # Handle streaming response - use stream (actual value used) not original_stream
    if stream:
        return StreamingResponse(
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

    # Non-streaming response
    response_content = {
        "id": job_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": usage,
    }

    # Add system_fingerprint if available (OA-4)
    if system_fingerprint:
        response_content["system_fingerprint"] = system_fingerprint

    # Determine finish_reason: "tool_calls" only if tool calls present AND no text content
    finish_reason = "tool_calls" if (tool_calls_data and not text_content) else "stop"

    if tool_calls_data:
        response_content["choices"].append(
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text_content,
                },
                "tool_calls": tool_calls_data,
                "finish_reason": finish_reason,
            }
        )
    else:
        response_content["choices"].append(
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text_content,
                },
                "finish_reason": finish_reason,
            }
        )

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
    log_chat_usage(virtual_model, endpoint_name, endpoint_id, usage, response_time_ms)

    # Build and return response
    response_content = {
        "id": job_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": usage,
    }

    # Add system_fingerprint if available (OA-4)
    if system_fingerprint:
        response_content["system_fingerprint"] = system_fingerprint

    # Determine finish_reason: "tool_calls" only if tool calls present AND no text content
    finish_reason = "tool_calls" if (tool_calls_data and not text_content) else "stop"

    if tool_calls_data:
        response_content["choices"].append(
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text_content,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": tc["function"]["arguments"],
                            },
                        }
                        for tc in tool_calls_data
                    ],
                },
                "finish_reason": finish_reason,
            }
        )
    elif text_content is not None:
        response_content["choices"].append(
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text_content,
                },
                "finish_reason": finish_reason,
            }
        )

    return JSONResponse(content=response_content)


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

    if tool_calls_data:
        for tc_index, tc in enumerate(tool_calls_data):
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
                                    "id": tc["id"],
                                    "type": "function",
                                    "function": {
                                        "name": tc["function"]["name"],
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

            args = tc["function"]["arguments"]
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
                                    "id": tc["id"],
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
    finish_reason = "tool_calls" if (tool_calls_data and not text_content) else "stop"

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
                    AVG(response_time_ms) as avg_response_time_ms
                FROM request_usage
                WHERE created_at >= ? AND created_at <= ?
            """
            params = [start_date, end_date]

            # Add virtual model filter if specified
            if virtual_model:
                base_query += " AND virtual_model = ?"
                params.append(virtual_model)

            cursor.execute(base_query, params)
            summary = cursor.fetchone()

            # Daily breakdown
            daily_query = """
                SELECT 
                    strftime('%Y-%m-%d', datetime(created_at, 'unixepoch')) as date,
                    SUM(prompt_tokens) as prompt_tokens,
                    SUM(completion_tokens) as completion_tokens,
                    SUM(cached_input_tokens) as cached_input_tokens,
                    SUM(cache_creation_tokens) as cache_creation_tokens,
                    SUM(cached_cost_estimate) as cached_cost_estimate,
                    SUM(cost_estimate) as cost_estimate,
                    COUNT(*) as requests
                FROM request_usage
                WHERE created_at >= ? AND created_at <= ?
            """
            daily_params = [start_date, end_date]

            if virtual_model:
                daily_query += " AND virtual_model = ?"
                daily_params.append(virtual_model)

            daily_query += " GROUP BY date ORDER BY date DESC"
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
                    }
                )

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
                    },
                    "daily_breakdown": daily_breakdown,
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

    if not start_date:
        start_date = str(int(time.time()) - 86400)
    if not end_date:
        end_date = str(int(time.time()))

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
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
                GROUP BY virtual_model
                ORDER BY total_tokens DESC
            """,
                [start_date, end_date],
            )

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

    if not start_date:
        start_date = str(int(time.time()) - 86400)
    if not end_date:
        end_date = str(int(time.time()))

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT 
                    endpoint_name,
                    SUM(prompt_tokens) as prompt_tokens,
                    SUM(completion_tokens) as completion_tokens,
                    SUM(total_tokens) as total_tokens,
                    COUNT(*) as request_count
                FROM request_usage
                WHERE created_at >= ? AND created_at <= ? AND endpoint_name IS NOT NULL
                GROUP BY endpoint_name
                ORDER BY total_tokens DESC
            """,
                [start_date, end_date],
            )

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

    if not start_date:
        start_date = str(int(time.time()) - 86400)
    if not end_date:
        end_date = str(int(time.time()))

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            base_query = """
                SELECT 
                    datetime(created_at, 'unixepoch', 'localtime') as date,
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

            base_query += " ORDER BY created_at DESC"

            cursor.execute(base_query, params)
            rows = cursor.fetchall()

            # Build CSV
            csv_lines = [
                "Date,Virtual Model,Endpoint,Request Type,Prompt Tokens,Completion Tokens,Total Tokens,Cost ($),Response Time (ms)"
            ]
            for row in rows:
                csv_lines.append(
                    f"{row[0]},{row[1]},{row[2]},{row[3]},{row[4]},{row[5]},{row[6]},{row[7]:.4f},{row[8]}"
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

    if not start_date:
        start_date = str(int(time.time()) - 86400)
    if not end_date:
        end_date = str(int(time.time()))

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT 
                    SUM(input_tokens) as total_input_tokens,
                    SUM(output_tokens) as total_output_tokens,
                    SUM(total_tokens) as total_tokens,
                    SUM(cost_estimate) as total_cost,
                    COUNT(*) as request_count
                FROM embedding_usage
                WHERE created_at >= ? AND created_at <= ?
            """,
                [start_date, end_date],
            )

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
            # Handle tool_result in content
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_result":
                        converted_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": block.get("tool_use_id", ""),
                                "content": block.get("content", ""),
                            }
                        )
                    else:
                        converted_messages.append(
                            {"role": "user", "content": block.get("text", "")}
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
    virtual_model = model
    if hasattr(backend, "virtual_model_name"):
        virtual_model = backend.virtual_model_name

    # Build request for chat/completions - use actual model from backend
    actual_model = backend.model if hasattr(backend, "model") else model
    chat_data = {
        "model": actual_model,
        "messages": converted_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": top_p,
        "stream": stream,
    }
    if tools:
        chat_data["tools"] = tools

    # Check for streaming
    if stream:
        # Handle streaming - similar to chat_completions but return SSE
        headers = {"Content-Type": "application/json"}
        if hasattr(backend, "api_key") and backend.api_key:
            headers["Authorization"] = f"Bearer {backend.api_key}"

        # Add tracking headers - use incoming source
        headers["X-Source"] = incoming_source
        headers["X-Model"] = actual_model
        headers["X-Priority"] = "NORMAL"

        # Get endpoint URL
        endpoint = backend.url
        if hasattr(backend, "endpoint_type"):
            if backend.endpoint_type == "deepinfra":
                endpoint = f"{endpoint}/v1/openai/chat/completions"
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
                                delta = chunk.get("choices", [{}])[0].get("delta", {})
                                content = delta.get("content")
                                finish = delta.get("finish_reason")

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
    content_blocks = []
    tool_calls_data = []

    if "choices" in result and result["choices"]:
        message = result["choices"][0].get("message", {})
        content = message.get("content", "") or ""
        tool_calls_data = message.get("tool_calls", []) or []
        finish_reason = message.get("finish_reason", "end_turn")

    # Convert text content
    if content:
        content_blocks.append({"type": "text", "text": content})

    # Convert tool calls to Anthropic format
    stop_reason = finish_reason
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
        if stop_reason != "tool_calls":
            stop_reason = "tool_use"

    usage = result.get("usage", {})

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
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }

    return JSONResponse(content=response)


@app.get("/v1/models")
async def list_models():
    # Get virtual models from database
    virtual_models = []
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT vm.name, vm.actual_model, e.name as endpoint_name
                FROM virtual_models vm
                JOIN endpoints e ON vm.endpoint_id = e.id
                WHERE vm.enabled = 1 AND e.enabled = 1
            """)
            virtual_models = [dict(row) for row in cursor.fetchall()]
    except Exception:
        pass

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

    return {
        "object": "list",
        "data": models,
    }


@app.post("/v1/completions")
async def completions(request: Request):
    """
    Legacy completions endpoint - converts to chat completions format.
    Many tools still use this for text-only completions.
    """
    start_time = time_module.time()
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

    # Call backend
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
        return JSONResponse(status_code=status_code, content=error)

    # Extract result
    result = backend_result
    if "result" in backend_result:
        result = backend_result["result"]

    content = ""
    if "choices" in result and result["choices"]:
        content = result["choices"][0].get("message", {}).get("content", "") or ""

    job_id = result.get("id", f"cmpl-{int(time_module.time())}")
    usage = result.get(
        "usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    )

    if stream:

        async def generate_sse():
            created = int(time_module.time())
            if content:
                yield f"data: {json.dumps({'id': job_id, 'choices': [{'text': content, 'index': 0}], 'model': model})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            generate_sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # Log usage
    response_time_ms = int((time_module.time() - start_time) * 1000)
    log_completion_usage(model, None, None, usage, response_time_ms)

    return JSONResponse(
        content={
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
    )


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    """
    Embeddings endpoint for vector representations.
    Routes to backend if supported, otherwise returns error.
    """
    start_time = time_module.time()
    data = await request.json()

    input_text = data.get("input", "")
    model = data.get("model", os.getenv("EMBEDDING_MODEL", "nomic-embed-text"))

    # Get backend for this model (checks virtual models first)
    backend = get_backend(model)

    # Check if backend supports embeddings
    if hasattr(backend, "embeddings"):
        result, error, status_code = await backend.embeddings(
            input_text=input_text, model=model
        )
        if error:
            return JSONResponse(status_code=status_code, content=error)

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
                    model, None, None, input_tokens, output_tokens, response_time_ms
                )
        except:
            pass  # Embeddings might not return token usage

        return JSONResponse(content=result)

    # Embeddings not supported - return error with OpenAI format
    return JSONResponse(
        status_code=501,
        content={
            "error": {
                "message": "Embeddings not supported by current backend",
                "type": "invalid_request_error",
                "code": "not_implemented",
            }
        },
    )


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
    menu_base = os.getenv("AIMENU_PUBLIC_URL", "https://menu.troden.com")
    return menu_base


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

        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO endpoints (name, url, api_key, endpoint_type, priority, enabled)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (
                        data.get("name"),
                        data.get("url"),
                        data.get("api_key", ""),
                        data.get("endpoint_type", "openai"),
                        int(data.get("priority", 0)),
                        1 if data.get("enabled", True) else 0,
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

    data = flask_request.get_json()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE endpoints 
            SET name = ?, url = ?, api_key = ?, endpoint_type = ?, priority = ?, enabled = ?,
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
        endpoint = dict(cursor.fetchone()) if cursor.fetchone() else None

    if not endpoint:
        return flask_jsonify({"error": "Endpoint not found"}), 404

    try:
        headers = {}
        if endpoint.get("api_key"):
            headers["Authorization"] = f"Bearer {endpoint['api_key']}"

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
        if endpoint.get("api_key"):
            headers["Authorization"] = f"Bearer {endpoint['api_key']}"

        # Try /v1/models first
        resp = httpx.get(f"{endpoint['url']}/v1/models", headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            models = data.get("data", [])
            model_list = sorted([m.get("id") for m in models])
            return flask_jsonify({"models": model_list})

        # Fallback to /models
        resp = httpx.get(f"{endpoint['url']}/models", headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            models = data.get("models", []) or data.get("data", [])
            model_list = sorted([m.get("id") or m.get("name") for m in models])
            return flask_jsonify({"models": model_list})

        return flask_jsonify({"error": f"Status: {resp.status_code}"}), resp.status_code
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
                enabled = ?, max_tokens = ?, temperature = ?, top_p = ?, system_prompt = ?,
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
                data.get("max_tokens") or 0,
                data.get("temperature") or 0,
                data.get("top_p") or 1.0,
                data.get("system_prompt") or "",
                1 if data.get("show_reasoning", True) else 0,
                vm_id,
            ),
        )
        conn.commit()

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
                enabled, max_tokens, temperature, top_p, system_prompt, show_reasoning
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                data.get("max_tokens") or 0,
                data.get("temperature") or 0,
                data.get("top_p") or 1.0,
                data.get("system_prompt") or "",
                1 if data.get("show_reasoning", True) else 0,
            ),
        )
        conn.commit()

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
            SELECT vm.*, e.name as endpoint_name, e.url as endpoint_url
            FROM virtual_models vm
            LEFT JOIN endpoints e ON vm.endpoint_id = e.id
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


@flask_app.route("/api/admin/endpoints", methods=["POST"])
def api_admin_endpoints_create():
    """API: Create new endpoint."""
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
                INSERT INTO endpoints (name, url, api_key, endpoint_type, priority, enabled)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    data.get("name"),
                    data.get("url"),
                    data.get("api_key", ""),
                    data.get("endpoint_type", "openai"),
                    int(data.get("priority", 0)),
                    1 if data.get("enabled", True) else 0,
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

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE endpoints 
                SET name = ?, url = ?, api_key = ?, endpoint_type = ?, priority = ?, enabled = ?,
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


@flask_app.route("/api/admin/virtual-models", methods=["GET"])
def api_admin_virtual_models():
    """API: List all virtual models."""
    auth = validate_session()
    if not auth.get("valid"):
        return flask_jsonify({"error": "Unauthorized"}), 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT vm.*, e.name as endpoint_name, e.url as endpoint_url
            FROM virtual_models vm
            LEFT JOIN endpoints e ON vm.endpoint_id = e.id
            ORDER BY vm.name
        """)
        vms = [dict(row) for row in cursor.fetchall()]

    return flask_jsonify(vms)


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

    settings = {}
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM settings")
        for row in cursor.fetchall():
            settings[row["key"]] = row["value"]

    return flask_jsonify(settings)


ENV_KEY_MAP = {
    "api_port": "API_PORT",
    "flask_port": "FLASK_PORT",
    "aimenu_url": "AIMENU_URL",
    "use_ai_queue": "USE_AI_QUEUE",
    "ai_queue_url": "AI_QUEUE_URL",
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
