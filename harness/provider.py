from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class ToolCall:
    id: str | None
    name: str
    arguments: dict[str, Any]


@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall]
    usage: TokenUsage | None
    raw_content: Any | None = None


async def complete(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> LLMResponse:
    """Single completion using the configured provider."""
    provider = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
    if provider in {"ollama", "local"}:
        return await _complete_ollama(messages, tools)
    if provider in {"gemini", "google"}:
        return await _complete_gemini(messages, tools)
    raise RuntimeError(f"Unsupported LLM_PROVIDER '{provider}'. Use 'ollama' or 'gemini'.")


async def _complete_ollama(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> LLMResponse:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    model_id = os.getenv("OLLAMA_MODEL") or os.getenv("MODEL_ID") or "qwen3:14b"
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": _to_ollama_messages(messages),
        "stream": False,
        "think": _ollama_think_setting(),
        "options": {"temperature": 0.2},
    }
    if tools:
        payload["tools"] = [_to_ollama_tool(tool) for tool in tools]

    async with httpx.AsyncClient(timeout=float(os.getenv("OLLAMA_TIMEOUT", "120"))) as client:
        response = await client.post(f"{base_url}/api/chat", json=payload)

    if response.status_code >= 400:
        raise RuntimeError(_ollama_error_message(response))

    data = response.json()
    message = data.get("message", {})
    tool_calls = _extract_ollama_tool_calls(message)
    text = None if tool_calls else _strip_thinking(message.get("content") or "")
    usage = TokenUsage(
        input_tokens=int(data.get("prompt_eval_count", 0) or 0),
        output_tokens=int(data.get("eval_count", 0) or 0),
    )
    return LLMResponse(text=text or None, tool_calls=tool_calls, usage=usage, raw_content=data)


async def _complete_gemini(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> LLMResponse:
    """Single Gemini completion using manual function calling."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    model_id = os.getenv("GEMINI_MODEL") or os.getenv("MODEL_ID") or "gemini-2.5-flash"
    system_instruction = _system_instruction(messages)
    contents = [_to_gemini_content(message, types) for message in messages if message.get("role") != "system"]

    config_kwargs: dict[str, Any] = {
        "temperature": 0.2,
    }
    if system_instruction:
        config_kwargs["system_instruction"] = system_instruction
    if tools:
        config_kwargs["tools"] = [types.Tool(function_declarations=tools)]
        config_kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)

    response = await client.aio.models.generate_content(
        model=model_id,
        contents=contents,
        config=types.GenerateContentConfig(**config_kwargs),
    )

    raw_content = None
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        raw_content = getattr(candidates[0], "content", None)

    tool_calls = _extract_gemini_tool_calls(response)
    text = None if tool_calls else _extract_gemini_text(response)

    usage_metadata = getattr(response, "usage_metadata", None)
    usage = None
    if usage_metadata:
        usage = TokenUsage(
            input_tokens=int(getattr(usage_metadata, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(usage_metadata, "candidates_token_count", 0) or 0),
        )

    return LLMResponse(text=text, tool_calls=tool_calls, usage=usage, raw_content=raw_content)


async def complete_with_retry(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_retries: int = 3,
) -> LLMResponse:
    for attempt in range(max_retries + 1):
        try:
            return await complete(messages, tools)
        except Exception as exc:
            if not _retryable(exc) or attempt >= max_retries:
                raise
            retry_after = _retry_after(exc)
            delay = retry_after if retry_after is not None else [1, 4, 16][attempt]
            await asyncio.sleep(delay)
    raise RuntimeError("retry loop exited unexpectedly")


def _system_instruction(messages: list[dict[str, Any]]) -> str | None:
    texts = []
    for message in messages:
        if message.get("role") != "system":
            continue
        for part in message.get("parts", []):
            if "text" in part:
                texts.append(part["text"])
    return "\n\n".join(texts) if texts else None


def _to_gemini_content(message: dict[str, Any], types: Any) -> Any:
    if raw := message.get("raw"):
        return raw

    parts = []
    for part in message.get("parts", []):
        if "text" in part:
            parts.append(types.Part(text=str(part["text"])))
        elif "function_call" in part:
            call = part["function_call"]
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(
                        id=call.get("id"),
                        name=call["name"],
                        args=call.get("args", {}),
                    )
                )
            )
        elif "function_response" in part:
            response = part["function_response"]
            parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        id=response.get("id"),
                        name=response["name"],
                        response=response.get("response", {}),
                    )
                )
            )
    return types.Content(role=message["role"], parts=parts)


def _to_ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ollama_messages = []
    for message in messages:
        role = message.get("role")
        content_parts = []
        tool_calls = []
        tool_responses = []

        for part in message.get("parts", []):
            if "text" in part:
                content_parts.append(str(part["text"]))
            elif "function_call" in part:
                call = part["function_call"]
                tool_calls.append(
                    {
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": call.get("args", {}),
                        },
                    }
                )
            elif "function_response" in part:
                response = part["function_response"]
                tool_responses.append(
                    {
                        "role": "tool",
                        "tool_name": response["name"],
                        "content": str(response.get("response", {}).get("result", "")),
                    }
                )

        if tool_responses:
            ollama_messages.extend(tool_responses)
            continue

        ollama_role = "assistant" if role == "model" else role
        ollama_message: dict[str, Any] = {
            "role": ollama_role,
            "content": "\n".join(content_parts),
        }
        if tool_calls:
            ollama_message["tool_calls"] = tool_calls
        ollama_messages.append(ollama_message)

    return ollama_messages


def _to_ollama_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
        },
    }


def _extract_ollama_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    calls = []
    for index, call in enumerate(message.get("tool_calls", []) or [], start=1):
        function = call.get("function", {})
        arguments = function.get("arguments", {}) or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        calls.append(
            ToolCall(
                id=call.get("id") or f"ollama_call_{index}",
                name=function.get("name", ""),
                arguments=dict(arguments),
            )
        )
    return calls


def _extract_gemini_tool_calls(response: Any) -> list[ToolCall]:
    calls = []
    for idx, call in enumerate(getattr(response, "function_calls", None) or []):
        calls.append(
            ToolCall(
                id=getattr(call, "id", None),
                name=getattr(call, "name", ""),
                arguments=dict(getattr(call, "args", {}) or {}),
            )
        )
    if calls:
        return calls

    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return []
    content = getattr(candidates[0], "content", None)
    for idx, part in enumerate(getattr(content, "parts", None) or []):
        call = getattr(part, "function_call", None)
        if call:
            calls.append(
                ToolCall(
                    id=getattr(call, "id", None),
                    name=getattr(call, "name", ""),
                    arguments=dict(getattr(call, "args", {}) or {}),
                )
            )
    return calls


def _extract_gemini_text(response: Any) -> str | None:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    content = getattr(candidates[0], "content", None)
    parts = getattr(content, "parts", None) or []
    texts = [getattr(part, "text", None) for part in parts]
    joined = "".join(text for text in texts if text)
    return joined or None


def _ollama_think_setting() -> bool | str:
    raw = os.getenv("OLLAMA_THINK", "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"high", "medium", "low"}:
        return raw
    return False


def _ollama_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    detail = payload.get("error") or response.text
    return f"Ollama API error {response.status_code}: {detail}"


def _strip_thinking(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("<think>") and "</think>" in stripped:
        return stripped.split("</think>", 1)[1].strip()
    return stripped


def _retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status is not None:
        try:
            status_int = int(status)
        except (TypeError, ValueError):
            status_int = 0
        return status_int == 429 or 500 <= status_int < 600
    text = str(exc).lower()
    if "context" in text and ("overflow" in text or "too long" in text):
        return False
    if "llama-server binary not found" in text:
        return False
    return any(marker in text for marker in ("429", "rate limit", "503", "500", "unavailable"))


def _retry_after(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None
