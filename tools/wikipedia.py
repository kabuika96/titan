from __future__ import annotations

import re
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from harness.tool import ToolResult, define_tool

USER_AGENT = "titan-research-agent/0.1 (interview assessment)"


class WikipediaParams(BaseModel):
    query: str = Field(description="Search query for Wikipedia")
    sentences: int = Field(default=5, ge=1, le=10, description="Number of summary sentences")


async def execute(params: WikipediaParams) -> ToolResult:
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": USER_AGENT}) as client:
            search = await client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": params.query,
                    "srlimit": 1,
                    "format": "json",
                },
            )
            search.raise_for_status()
            search_data = search.json()
            results = search_data.get("query", {}).get("search", [])
            if not results:
                return ToolResult(output="", error=f"No Wikipedia result for query: {params.query}")

            title = results[0]["title"]
            summary = await client.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title, safe='')}"
            )
            summary.raise_for_status()
            summary_data = summary.json()

        extract = summary_data.get("extract") or ""
        extract = _limit_sentences(extract, params.sentences)
        url = summary_data.get("content_urls", {}).get("desktop", {}).get(
            "page", f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'), safe='/:')}"
        )
        description = summary_data.get("description")
        output = f"Title: {title}\n"
        if description:
            output += f"Description: {description}\n"
        output += f"Summary: {extract}\nSource: {url}"
        return ToolResult(
            output=output,
            metadata={"title": title, "url": url, "query": params.query},
        )
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return ToolResult(output="", error=f"Wikipedia API unavailable: {exc}")


def _limit_sentences(text: str, count: int) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(sentences[:count]).strip()


tool = define_tool(
    id="wikipedia",
    description=(
        "Search Wikipedia for factual information, concepts, history, and general "
        "knowledge. Returns an article summary with a source URL."
    ),
    param_model=WikipediaParams,
    execute_fn=execute,
)

