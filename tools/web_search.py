from __future__ import annotations

import asyncio

from duckduckgo_search import DDGS
from pydantic import BaseModel, Field

from harness.tool import ToolResult, define_tool


class WebSearchParams(BaseModel):
    query: str = Field(description="Search query for public web pages")
    max_results: int = Field(default=5, ge=1, le=10, description="Maximum number of search results to return")


async def execute(params: WebSearchParams) -> ToolResult:
    try:
        results = await asyncio.to_thread(_search, params.query, params.max_results)
    except Exception as exc:
        return ToolResult(output="", error=f"web_search unavailable: {exc}")

    normalized = [_normalize_result(result) for result in results]
    normalized = [result for result in normalized if result["url"]]
    if not normalized:
        return ToolResult(output="", error=f"No web search results for query: {params.query}")

    lines = [f"Web search results for: {params.query}"]
    for index, result in enumerate(normalized, start=1):
        lines.append(
            "\n".join(
                [
                    f"{index}. {result['title'] or result['url']}",
                    f"Snippet: {result['snippet']}",
                    f"URL: {result['url']}",
                ]
            )
        )

    return ToolResult(
        output="\n\n".join(lines),
        metadata={"query": params.query, "results": normalized},
    )


def _search(query: str, max_results: int) -> list[dict]:
    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results))


def _normalize_result(result: dict) -> dict[str, str]:
    return {
        "title": str(result.get("title") or "").strip(),
        "url": str(result.get("href") or result.get("url") or "").strip(),
        "snippet": str(result.get("body") or result.get("snippet") or "").strip(),
    }


tool = define_tool(
    id="web_search",
    description=(
        "Search the public web with DuckDuckGo and return result titles, snippets, and URLs. "
        "Use this to discover sources before web_fetch when specific tools are insufficient."
    ),
    param_model=WebSearchParams,
    execute_fn=execute,
    max_output_chars=5000,
)
