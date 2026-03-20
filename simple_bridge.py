#!/usr/bin/env python3
"""
Simple proxy to bridge OpenAI-compatible requests to RunPod Serverless
Supports both vLLM and Ollama endpoints via ENDPOINT_TYPE env var
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
import os
import time
import json
import asyncio
import re

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

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "")
ENDPOINT_TYPE = os.getenv("ENDPOINT_TYPE", "ollama").lower()  # "ollama" or "vllm"

# AI Queue Master integration (optional)
USE_AI_QUEUE = os.getenv("USE_AI_QUEUE", "false").lower() == "true"
AI_QUEUE_URL = os.getenv("AI_QUEUE_URL", "http://host.docker.internal:8102")
AI_QUEUE_API_KEY = os.getenv("AI_QUEUE_API_KEY", "")
AI_QUEUE_PRIORITY = os.getenv("AI_QUEUE_PRIORITY", "NORMAL")  # HIGH, NORMAL, LOW
AI_QUEUE_SOURCE = os.getenv("AI_QUEUE_SOURCE", "runpod-proxy")


async def wait_for_completion(client, job_id, max_wait=300):
    start = time.time()
    while time.time() - start < max_wait:
        await asyncio.sleep(2)
        status_resp = await client.get(
            f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{job_id}",
            headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
        )
        if status_resp.status_code == 200:
            data = status_resp.json()
            if data.get("status") == "COMPLETED":
                return data
            elif data.get("status") in ["FAILED", "CANCELLED"]:
                return data
    return {"status": "TIMEOUT"}


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


def build_input_payload_vllm(messages, temperature, max_tokens, top_p, tools=None):
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
    if tools:
        payload["tools"] = tools
    return payload


def build_input_payload_ollama(messages, temperature, max_tokens, top_p, tools=None):
    """Build Ollama format payload - convert messages to prompt with tool definitions."""
    # Include tool definitions in system prompt if tools are provided
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

    # Add tool instructions if present
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


def extract_content_ollama(result):
    """Extract content from Ollama response format."""
    output = result.get("output", [])

    if isinstance(output, list) and len(output) > 0:
        # Try different response formats
        item = output[0]
        if isinstance(item, dict):
            # Format: {"choices": [{"text": "..."}]}
            if "choices" in item:
                choices = item.get("choices", [])
                if choices and isinstance(choices[0], dict):
                    text = choices[0].get("text", "")
                    if text:
                        return text
            # Format: {"response": "..."}
            if "response" in item:
                return item.get("response", "")
            # Format: {"text": "..."}
            if "text" in item:
                return item.get("text", "")

    return ""


async def handle_ai_queue_request(messages, model, tools, timeout=1200):
    """Route request through AI Queue Master instead of directly to RunPod."""
    headers = {
        "Authorization": f"Bearer {AI_QUEUE_API_KEY}",
        "Content-Type": "application/json",
        "X-Source": AI_QUEUE_SOURCE,
        "X-Priority": AI_QUEUE_PRIORITY,
    }

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools

    async with httpx.AsyncClient(timeout=float(timeout)) as client:
        response = await client.post(
            f"{AI_QUEUE_URL}/v1/chat/completions",
            headers=headers,
            json=payload,
        )

        if response.status_code != 200:
            return (
                None,
                {"error": f"AI Queue error: {response.text}"},
                response.status_code,
            )

        result = response.json()

        # Handle error responses from queue
        if result.get("error"):
            return None, {"error": result.get("error")}, 500

        return result, None, 200


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    data = await request.json()

    messages = data.get("messages", [])
    model = data.get("model", os.getenv("MODEL_NAME", "qwen3.5:27b"))
    temperature = data.get("temperature", 0.7)
    max_tokens = data.get("max_tokens", 256)
    top_p = data.get("top_p", 1.0)
    stream = data.get("stream", False)
    tools = data.get("tools", [])

    # Route through AI Queue Master if enabled
    if USE_AI_QUEUE:
        import logging

        logger = logging.getLogger("uvicorn")
        logger.warning(
            f"[AI_QUEUE] Received request: model={model}, stream={stream}, tools={len(tools)}"
        )

        queue_result, error, status_code = await handle_ai_queue_request(
            messages, model, tools
        )

        if error:
            logger.error(f"[AI_QUEUE] Error: {error}")
            return JSONResponse(status_code=status_code, content=error)

        logger.warning(
            f"[AI_QUEUE] Raw result keys: {list(queue_result.keys()) if queue_result else 'None'}"
        )

        # Extract content from queue response (OpenAI format)
        content = ""
        tool_calls_data = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        if "choices" in queue_result and queue_result["choices"]:
            choice = queue_result["choices"][0]
            content = choice.get("message", {}).get("content", "") or ""
            tool_calls_data = choice.get("message", {}).get("tool_calls", []) or []
            usage = queue_result.get("usage", usage)

        # Process content to extract tool calls
        extracted_tc, text_content = process_content(content)
        if extracted_tc:
            tool_calls_data = extracted_tc
        elif not tool_calls_data:
            text_content = text_content or content

        job_id = queue_result.get("id", f"chat-{int(time.time())}")

        # Handle streaming response
        if stream:

            async def generate_sse():
                created = int(time.time())
                chunk_id = job_id

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

                final_chunk = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "tool_calls"
                            if tool_calls_data
                            else "stop",
                        }
                    ],
                }
                yield f"data: {json.dumps(final_chunk)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                generate_sse(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )

        # Non-streaming response
        if tool_calls_data:
            return JSONResponse(
                content={
                    "id": job_id,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": text_content,
                            },
                            "tool_calls": tool_calls_data,
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": usage,
                }
            )

        return JSONResponse(
            content={
                "id": job_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": text_content,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            }
        )

    # Direct RunPod mode (existing logic)
    # Build payload based on endpoint type
    if ENDPOINT_TYPE == "ollama":
        input_data = build_input_payload_ollama(
            messages, temperature, max_tokens, top_p, tools
        )
    else:
        input_data = build_input_payload_vllm(
            messages, temperature, max_tokens, top_p, tools
        )

    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.post(
            f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/runsync",
            headers={
                "Authorization": f"Bearer {RUNPOD_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"input": input_data},
        )

        if response.status_code != 200:
            return JSONResponse(
                status_code=500, content={"error": f"RunPod error: {response.text}"}
            )

        result = response.json()
        job_id = result.get("id", f"chat-{int(time.time())}")

        if result.get("status") != "COMPLETED":
            result = await wait_for_completion(client, job_id)
            if result.get("status") == "TIMEOUT":
                return JSONResponse(
                    status_code=408, content={"error": "Request timed out"}
                )
            if result.get("status") in ["FAILED", "CANCELLED"]:
                return JSONResponse(
                    status_code=500,
                    content={"error": f"Job {result.get('status', 'unknown').lower()}"},
                )

        # Extract content based on endpoint type
        if ENDPOINT_TYPE == "ollama":
            content = extract_content_ollama(result)
        else:
            output = result.get("output", [])
            if isinstance(output, list) and len(output) > 0:
                choice_data = output[0].get("choices", [{}])[0]
                tokens = choice_data.get("tokens", [])
                content = tokens[0] if tokens else ""
            else:
                content = ""

        tool_calls_data, text_content = process_content(content)
        if not tool_calls_data:
            text_content = text_content or content

        # Get usage if available
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if ENDPOINT_TYPE != "ollama":
            output = result.get("output", [])
            if isinstance(output, list) and len(output) > 0:
                usage = output[0].get("usage", usage)

        if stream:

            async def generate_stream():
                created = int(time.time())
                chunk_id = f"chatcmpl-{job_id}"

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

                final_chunk = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "tool_calls"
                            if tool_calls_data
                            else "stop",
                        }
                    ],
                }
                yield f"data: {json.dumps(final_chunk)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                generate_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )

        # Non-streaming response
        if tool_calls_data:
            return JSONResponse(
                content={
                    "id": job_id,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": text_content,
                            },
                            "tool_calls": tool_calls_data,
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": usage,
                }
            )

        return JSONResponse(
            content={
                "id": job_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": text_content,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            }
        )


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
