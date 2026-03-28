#!/usr/bin/env python3
"""
Simple proxy to bridge OpenAI-compatible requests to RunPod Serverless
Supports both vLLM and Ollama endpoints via ENDPOINT_TYPE env var
Supports multiple backends via backend abstraction layer
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import os
import time
import json
import asyncio
import re
from abc import ABC, abstractmethod
from typing import Any, Optional

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


# ============================================================================
# Backend Factory
# ============================================================================


def get_backend() -> LLMBackend:
    """Get the configured backend based on environment variables."""
    use_queue = os.getenv("USE_AI_QUEUE", "false").lower() == "true"

    if use_queue:
        return AIQueueBackend()
    else:
        return RunPodBackend()


# Initialize backend
BACKEND = get_backend()

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "")
ENDPOINT_TYPE = os.getenv("ENDPOINT_TYPE", "ollama").lower()  # "ollama" or "vllm"


def extract_tool_calls(content):
    """Extract tool calls from content and return structured tool_calls + remaining text."""
    if not content:
        return [], content

    fence_pattern = re.compile(r"```\s*(\w*)\s*\n?(.*?)```", re.DOTALL)
    inline_pattern = re.compile(
        r"(?:^|\s)(?:assistant)?commentary to=([\w_.]+)\s+(?:code|json|tool_call|func)\s*(\{[^}]*\})",
        re.MULTILINE,
    )
    tool_use_pattern = re.compile(
        r"<tool_use\s+code\s+name=\"(\w+)\"\s*>(.*?)</tool_use>", re.DOTALL
    )
    tool_code_pattern = re.compile(r"<tool_code>(.*?)</tool_code>", re.DOTALL)

    fence_matches = list(fence_pattern.finditer(content))
    inline_matches = list(inline_pattern.finditer(content))
    tool_use_matches = list(tool_use_pattern.finditer(content))
    tool_code_matches = list(tool_code_pattern.finditer(content))

    all_ranges = []
    for m in fence_matches:
        all_ranges.append(("fence", m.start(), m.end(), m))
    for m in inline_matches:
        all_ranges.append(("inline", m.start(), m.end(), m))
    for m in tool_use_matches:
        all_ranges.append(("tool_use", m.start(), m.end(), m))
    for m in tool_code_matches:
        all_ranges.append(("tool_code", m.start(), m.end(), m))
    all_ranges.sort(key=lambda x: x[1])

    if not all_ranges:
        return [], content

    tool_calls = []
    parts = []
    last_end = 0

    for match_type, start, end, match in all_ranges:
        if start > last_end:
            parts.append(content[last_end:start])

        if match_type == "fence":
            lang = match.group(1).strip()
            inner = match.group(2).strip()

            if not inner:
                continue

            is_json_content = inner.startswith("{")

            if lang == "tool_call" or is_json_content:
                json_objs = parse_json_objects(inner)
                for obj in json_objs:
                    name = obj.get("name")
                    args = obj.get("arguments")
                    if name and args:
                        if isinstance(args, str):
                            try:
                                args = json.loads(
                                    args.replace("\r\n", "\n").replace("\r", "\n")
                                )
                            except (json.JSONDecodeError, ValueError):
                                args_fixed = args.replace("\n", "\\n").replace(
                                    "\r", "\\r"
                                )
                                try:
                                    args = json.loads(args_fixed)
                                except (json.JSONDecodeError, ValueError):
                                    pass
                        args_str = json.dumps(args, ensure_ascii=False)
                        tool_calls.append(
                            {
                                "id": f"call_{int(time.time() * 1000)}_{len(tool_calls)}",
                                "type": "function",
                                "function": {"name": name, "arguments": args_str},
                            }
                        )
            else:
                full_call = inner
                if lang:
                    full_call = (lang + " " + inner).strip()
                bare = _parse_bare_call(full_call)
                if bare:
                    tool_calls.append(
                        {
                            "id": f"call_{int(time.time() * 1000)}_{len(tool_calls)}",
                            "type": "function",
                            "function": {"name": bare[0], "arguments": bare[1]},
                        }
                    )
        elif match_type == "tool_use":
            tool_name = match.group(1)
            args_inner = match.group(2).strip()
            try:
                args_obj = json.loads(args_inner)
                actual_args = args_obj.get("arguments", {})
                args_str = json.dumps(actual_args, ensure_ascii=False)
            except (json.JSONDecodeError, ValueError, AttributeError):
                args_str = _fix_json_newlines(args_inner)
            tool_calls.append(
                {
                    "id": f"call_{int(time.time() * 1000)}_{len(tool_calls)}",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": args_str},
                }
            )
        elif match_type == "tool_code":
            inner = match.group(1).strip()
            json_objs = parse_json_objects(inner)
            for obj in json_objs:
                name = obj.get("name")
                args = obj.get("arguments")
                if name and args:
                    if isinstance(args, str):
                        try:
                            args = json.loads(
                                args.replace("\r\n", "\n").replace("\r", "\n")
                            )
                        except (json.JSONDecodeError, ValueError):
                            args_fixed = args.replace("\n", "\\n").replace("\r", "\\r")
                            try:
                                args = json.loads(args_fixed)
                            except (json.JSONDecodeError, ValueError):
                                pass
                    args_str = json.dumps(args, ensure_ascii=False)
                    tool_calls.append(
                        {
                            "id": f"call_{int(time.time() * 1000)}_{len(tool_calls)}",
                            "type": "function",
                            "function": {"name": name, "arguments": args_str},
                        }
                    )
        else:
            tool_name = match.group(1)
            args_str = match.group(2)
            tool_calls.append(
                {
                    "id": f"call_{int(time.time() * 1000)}_{len(tool_calls)}",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": args_str},
                }
            )

        last_end = end

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
        part = part.strip()
        if part:
            cleaned_parts.append(part)

    remaining_text = "\n".join(cleaned_parts) if cleaned_parts else None
    if remaining_text:
        remaining_text = re.sub(r"\n{3,}", "\n\n", remaining_text)
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
        return tool_calls, cleaned if cleaned else None

    if "final:" in content:
        content = content.split("final:")[-1].strip()
    elif "assistantfinal" in content:
        content = content.split("assistantfinal")[-1].strip()
    elif "final " in content:
        content = content.split("final ")[-1].strip()

    if content.startswith("analysis"):
        content = content[8:].strip()

    return None, content


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions endpoint."""
    data = await request.json()

    messages = data.get("messages", [])
    model = data.get("model", os.getenv("MODEL_NAME", "project-system-ai"))
    temperature = data.get("temperature", 0.7)
    max_tokens = data.get("max_tokens", 256)
    top_p = data.get("top_p", 1.0)
    stream = data.get("stream", False)
    tools = data.get("tools", [])

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
        "parallel_tool_calls": data.get("parallel_tool_calls", True),
    }

    # Call backend (handles both AI Queue and RunPod)
    backend_result, error, status_code = await BACKEND.chat_completion(
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

    # Extract content from response
    content = ""
    tool_calls_data = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    if "choices" in result and result["choices"]:
        choice = result["choices"][0]
        content = choice.get("message", {}).get("content", "") or ""
        tool_calls_data = choice.get("message", {}).get("tool_calls", []) or []
        usage = result.get("usage", usage)

    # Extract system_fingerprint if present (OA-4)
    system_fingerprint = result.get("system_fingerprint")

    # Process content to extract tool calls
    extracted_tc, text_content = process_content(content)
    if extracted_tc:
        tool_calls_data = extracted_tc
    elif not tool_calls_data:
        text_content = text_content or content

    job_id = result.get("id", f"chat-{int(time.time())}")

    # Handle streaming response
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
    backend_healthy = await BACKEND.health_check()
    return {
        "status": "healthy" if backend_healthy else "unhealthy",
        "backend": type(BACKEND).__name__,
        "timestamp": int(time.time()),
    }


@app.get("/v1/models")
async def list_models():
    model_name = os.getenv("MODEL_NAME", "qwen3.5:27b")
    return {
        "object": "list",
        "data": [
            {
                "id": model_name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "runpod",
            }
        ],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8002)
