from __future__ import annotations

import os
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag

from harness import provider, registry
from harness.provider import LLMResponse
from harness.tracer import Tracer

MAX_STEPS = int(os.getenv("MAX_STEPS", "15"))
PRUNE_AFTER_STEPS = 6
CONTEXT_BUDGET = 900_000


async def run(question: str, agent_config: dict[str, Any]) -> str:
    system = load_skills(agent_config["skills"])
    messages = [system_msg(system), user_msg(question)]
    tools = registry.to_llm_tools(agent_config.get("tools"))
    tracer = Tracer(question, trace_dir=agent_config.get("trace_dir", "traces"))
    max_steps = int(agent_config.get("max_steps") or MAX_STEPS)
    verbose = bool(agent_config.get("verbose"))

    if verbose:
        await _print_progress_note(
            "start",
            {"question": question},
            verbose,
            f"Investigating {question}.",
        )

    for step in range(1, max_steps + 1):
        if step > PRUNE_AFTER_STEPS:
            before = estimate_context_size(messages)
            messages = prune_tool_outputs(messages, keep_last_n=2)
            after = estimate_context_size(messages)
            if after < before:
                tracer.log_event(
                    "prune",
                    {
                        "reason": "step_threshold",
                        "context_size_before": before,
                        "context_size_after": after,
                    },
                )

        if estimate_context_size(messages) > CONTEXT_BUDGET:
            before = estimate_context_size(messages)
            messages = await compact(messages, system)
            tracer.log_event(
                "compact",
                {
                    "reason": "context_overflow",
                    "context_size_before": before,
                    "context_size_after": estimate_context_size(messages),
                },
            )

        tracer.step_start(step)
        response = await provider.complete_with_retry(messages, tools)
        tracer.log_llm(step, response)

        if not response.tool_calls:
            if not _has_tool_results(messages) and _needs_evidence_first(question, response.text):
                await _print_progress_note(
                    "evidence_check",
                    {
                        "question": question,
                        "issue": "No retrieved tool evidence is available yet for this in-scope research question.",
                    },
                    verbose,
                    "Gathering retrieved evidence before answering.",
                )
                messages.append(
                    user_msg(
                        "You answered before retrieving evidence. For this in-scope research "
                        "question, choose an appropriate tool first. Only cite sources returned "
                        "by tools."
                    )
                )
                continue
            await _print_progress_note(
                "synthesis",
                {
                    "question": question,
                    "status": "Retrieved evidence is available and the final answer is ready.",
                },
                verbose,
                "Writing the final answer from the retrieved evidence.",
            )
            tracer.log_final(response.text)
            tracer.save()
            return response.text or ""

        messages.append(assistant_msg(response))

        for call in response.tool_calls:
            tool = registry.get(call.name)
            if not tool:
                error = f"Error: unknown tool '{call.name}'"
                messages.append(tool_result_msg(call.id, call.name, error))
                tracer.log_tool_error(call.name, "unknown tool")
                continue

            await _print_tool_call(step, call.name, call.arguments, verbose)
            try:
                params = tool.pydantic_model(**call.arguments)
                if call.name == "web_fetch" and not _web_fetch_url_was_discovered(params.url, messages):
                    error = (
                        "Error: web_fetch requires a URL that appeared in a prior "
                        "web_search result or another tool result."
                    )
                    messages.append(tool_result_msg(call.id, call.name, error))
                    tracer.log_tool_error(call.name, "URL was not discovered by a prior tool result")
                    await _print_tool_error(error, verbose)
                    continue
                tracer.log_tool_call(call.name, call.arguments)
                start = time.perf_counter()
                result = await tool.execute(params)
                duration_ms = int((time.perf_counter() - start) * 1000)
                tracer.log_tool_result(call.name, result, duration_ms)
                if result.error:
                    await _print_tool_error(result.error, verbose)
                elif verbose:
                    await _print_evidence(call.name, result, duration_ms, verbose)
                messages.append(tool_result_msg(call.id, call.name, _tool_result_text(result)))
            except Exception as exc:
                error = f"Error: {exc}"
                messages.append(tool_result_msg(call.id, call.name, error))
                tracer.log_tool_error(call.name, str(exc))
                await _print_tool_error(error, verbose)

    messages.append(
        user_msg(
            "You have used all available research steps. Synthesize your answer now "
            "using only the information you have already gathered. Cite your sources."
        )
    )
    response = await provider.complete_with_retry(messages, tools=None)
    tracer.log_final(response.text)
    tracer.save()
    return response.text or ""


def load_skills(skill_paths: list[str]) -> str:
    parts = []
    for skill_path in skill_paths:
        parts.append(Path(skill_path).read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def system_msg(text: str) -> dict[str, Any]:
    return {"role": "system", "parts": [{"text": text}]}


def user_msg(text: str) -> dict[str, Any]:
    return {"role": "user", "parts": [{"text": text}]}


def assistant_msg(response: LLMResponse) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    if response.text:
        parts.append({"text": response.text})
    for call in response.tool_calls:
        parts.append({"function_call": {"id": call.id, "name": call.name, "args": call.arguments}})
    message: dict[str, Any] = {"role": "model", "parts": parts}
    if response.raw_content is not None:
        message["raw"] = response.raw_content
    return message


def tool_result_msg(call_id: str | None, name: str, text: str) -> dict[str, Any]:
    return {
        "role": "user",
        "parts": [
            {
                "function_response": {
                    "id": call_id,
                    "name": name,
                    "response": {"result": text},
                }
            }
        ],
    }


def prune_tool_outputs(messages: list[dict[str, Any]], keep_last_n: int = 2) -> list[dict[str, Any]]:
    remaining = keep_last_n
    pruned: list[dict[str, Any]] = []
    for message in reversed(messages):
        new_message = dict(message)
        new_parts = []
        for part in message.get("parts", []):
            if "function_response" not in part:
                new_parts.append(part)
                continue
            if remaining > 0:
                remaining -= 1
                new_parts.append(part)
                continue
            response = dict(part["function_response"])
            response["response"] = {"result": "[pruned]"}
            new_parts.append({"function_response": response})
        new_message["parts"] = new_parts
        pruned.append(new_message)
    return list(reversed(pruned))


async def compact(messages: list[dict[str, Any]], system: str) -> list[dict[str, Any]]:
    template = Path("skills/compaction.md").read_text(encoding="utf-8")
    transcript = _messages_as_text(messages)
    response = await provider.complete_with_retry(
        [system_msg(template), user_msg(transcript)],
        tools=None,
        max_retries=1,
    )
    original_question = _first_human_user_text(messages)
    summary = response.text or ""
    return [
        system_msg(system),
        user_msg(f"{summary}\n\nContinue answering the original question: {original_question}"),
    ]


def estimate_context_size(messages: list[dict[str, Any]]) -> int:
    return len(_messages_as_text(messages))


def _messages_as_text(messages: list[dict[str, Any]]) -> str:
    chunks = []
    for message in messages:
        chunks.append(f"role={message.get('role')}")
        for part in message.get("parts", []):
            if "text" in part:
                chunks.append(part["text"])
            elif "function_call" in part:
                chunks.append(f"function_call={part['function_call']}")
            elif "function_response" in part:
                chunks.append(f"function_response={part['function_response']}")
        if message.get("raw") is not None and not message.get("parts"):
            chunks.append(str(message["raw"]))
    return "\n".join(chunks)


def _has_tool_results(messages: list[dict[str, Any]]) -> bool:
    return any(
        "function_response" in part
        for message in messages
        for part in message.get("parts", [])
    )


def _needs_evidence_first(question: str, answer: str | None) -> bool:
    combined = f"{question}\n{answer or ''}".lower()
    out_of_scope_markers = [
        "outside my research capabilities",
        "outside the scope",
        "out of scope",
        "not a research",
        "personal opinion",
        "subjective",
    ]
    if any(marker in combined for marker in out_of_scope_markers):
        return False
    if "restaurant" in question.lower() and "best" in question.lower():
        return False
    return True


def _first_human_user_text(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") != "user":
            continue
        for part in message.get("parts", []):
            if "text" in part:
                return part["text"]
    return ""


def _tool_result_text(result: Any) -> str:
    if result.output and result.error:
        return f"{result.output}\n\nTool warning: {result.error}"
    return result.output or result.error or ""


def _web_fetch_url_was_discovered(url: str, messages: list[dict[str, Any]]) -> bool:
    target = _url_match_key(url)
    if not target:
        return False

    for message in messages:
        for part in message.get("parts", []):
            function_response = part.get("function_response")
            if not function_response:
                continue
            response = function_response.get("response", {})
            text = str(response.get("result", ""))
            if target in _url_match_key(text):
                return True
    return False


def _url_match_key(value: str) -> str:
    return urldefrag(str(value).strip())[0].rstrip("/")


async def _print_tool_call(step: int, name: str, args: dict[str, Any], verbose: bool) -> None:
    if not verbose:
        return
    route = _tool_route(name)
    await _print_progress_note(
        "action",
        {
            "tool": name,
            "route": route,
            "arguments": args,
        },
        verbose,
        f"Using {name} to gather {route} evidence.",
    )


async def _print_evidence(name: str, result: Any, duration_ms: int, verbose: bool) -> None:
    await _print_progress_note(
        "evidence",
        {
            "tool": name,
            "duration_ms": duration_ms,
            "evidence": _evidence_lines(name, result),
        },
        verbose,
        _fallback_evidence_sentence(name, result),
    )


async def _print_tool_error(error: str, verbose: bool) -> None:
    if not verbose:
        return
    await _print_progress_note(
        "warning",
        {"error": error},
        verbose,
        f"Handling a tool problem: {error}",
    )


async def _print_progress_note(
    event: str,
    payload: dict[str, Any],
    verbose: bool,
    fallback: str,
) -> None:
    if not verbose:
        return

    try:
        response = await provider.complete_with_retry(
            [
                system_msg(
                    "You write public progress updates for a CLI research agent. "
                    "Write exactly one concise, human-readable action sentence or sentence fragment. "
                    "Write from the assistant's perspective, but do not use 'I', 'we', "
                    "'the agent', or 'the model'. Prefer active phrasing like "
                    "'Investigating...', 'Looking up...', 'Checking...', or 'Writing...'. "
                    "Use only the supplied event data. Do not reveal hidden reasoning, "
                    "private chain-of-thought, or implementation details. "
                    "Do not mention tool-call mechanics, event names, character counts, "
                    "or internal status fields. "
                    "Do not use bullets, headings, JSON, markdown, or citations unless "
                    "the source was explicitly supplied. Keep it under 30 words."
                ),
                user_msg(json.dumps({"event": event, "data": payload}, ensure_ascii=True)),
            ],
            tools=None,
            max_retries=0,
        )
        note = _clean_progress_note(response.text)
    except Exception:
        note = fallback

    if note:
        print(f"-> {note}\n", file=sys.stderr)


def _clean_progress_note(text: str | None) -> str:
    note = " ".join((text or "").split())
    for prefix in ("Thinking:", "Progress:", "Note:", "->"):
        if note.startswith(prefix):
            note = note[len(prefix) :].strip()
    banned_starts = (
        "the research agent ",
        "the agent ",
        "the assistant ",
        "the model ",
        "i am ",
        "i'm ",
        "i will ",
        "i have ",
        "we are ",
    )
    lower_note = note.lower()
    for banned in banned_starts:
        if lower_note.startswith(banned):
            note = note[len(banned) :].strip()
            break
    lower_note = note.lower()
    for filler in ("has started ", "is currently ", "is now ", "is ", "has "):
        if lower_note.startswith(filler):
            note = note[len(filler) :].strip()
            break
    if not note:
        return ""
    return note[0].upper() + note[1:]


def _fallback_evidence_sentence(name: str, result: Any) -> str:
    metadata = result.metadata or {}
    if name == "wikipedia":
        title = metadata.get("title", "the selected Wikipedia article")
        return f"Found a Wikipedia source for {title} and can use it as evidence."
    if name == "arxiv":
        papers = metadata.get("papers", [])
        return f"Found {len(papers)} arXiv paper results to use as research evidence."
    if name == "fred_search":
        series = metadata.get("series", [])
        return f"Found {len(series)} matching FRED series and can choose the relevant data source."
    if name == "fred_data":
        observations = metadata.get("observations", [])
        latest = observations[-1] if observations else {}
        return f"Retrieved the latest FRED observation: {latest.get('date')} = {latest.get('value')}."
    if name == "web_search":
        results = metadata.get("results", [])
        return f"Found {len(results)} public web results that can be fetched if more detail is needed."
    if name == "web_fetch":
        title = metadata.get("title", "the requested web page")
        return f"Fetched {title} and converted it to markdown for review."
    return "Received tool evidence and can use it to continue the answer."


def _evidence_lines(name: str, result: Any) -> list[str]:
    metadata = result.metadata or {}
    if name == "wikipedia":
        title = metadata.get("title", "unknown article")
        url = metadata.get("url", "")
        return [
            f"Source: Wikipedia: {title}",
            f"URL: {url}",
            f"Found: {_extract_field(result.output, 'Summary')}",
        ]

    if name == "arxiv":
        papers = metadata.get("papers", [])
        lines = [f"Papers found: {len(papers)}"]
        for paper in papers[:3]:
            title = _shorten(str(paper.get("title", "")).replace("\n", " "), 120)
            arxiv_id = paper.get("arxiv_id", "")
            lines.append(f"Paper: {arxiv_id} - {title}")
        return lines

    if name == "fred_search":
        series = metadata.get("series", [])
        lines = [f"Series found: {len(series)}"]
        for item in series[:3]:
            lines.append(f"Series: {item.get('id')} - {_shorten(str(item.get('title', '')), 120)}")
        return lines

    if name == "fred_data":
        observations = metadata.get("observations", [])
        latest = observations[-1] if observations else {}
        return [
            f"Series: {metadata.get('series_id')}",
            f"URL: {metadata.get('url', '')}",
            f"Latest: {latest.get('date')} = {latest.get('value')}",
        ]

    if name == "web_search":
        results = metadata.get("results", [])
        lines = [f"Results found: {len(results)}"]
        for result in results[:3]:
            lines.append(f"Result: {_shorten(str(result.get('title', '')), 100)} - {result.get('url')}")
        return lines

    if name == "web_fetch":
        return [
            f"Title: {metadata.get('title')}",
            f"URL: {metadata.get('url')}",
            f"Content type: {metadata.get('content_type')}",
        ]

    return [f"Output: {_shorten(result.output or '', 180)}"]


def _extract_field(output: str, field: str) -> str:
    prefix = f"{field}:"
    for line in output.splitlines():
        if line.startswith(prefix):
            return _shorten(line[len(prefix) :].strip(), 220)
    return _shorten(output.strip(), 220)


def _shorten(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _tool_route(name: str) -> str:
    routes = {
        "wikipedia": "factual lookup / general background",
        "arxiv": "academic literature search",
        "fred_search": "economic data series discovery",
        "fred_data": "economic time-series retrieval",
        "web_search": "public web source discovery",
        "web_fetch": "public web page retrieval",
    }
    return routes.get(name, "tool-directed evidence gathering")
