from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import httpx
from pydantic import BaseModel, Field

from harness.tool import ToolResult, define_tool

USER_AGENT = "titan-research-agent/0.1 (interview assessment)"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


class ArxivParams(BaseModel):
    query: str = Field(description="Search query for academic papers")
    max_results: int = Field(default=5, ge=1, le=10, description="Number of papers to return")


async def execute(params: ArxivParams) -> ToolResult:
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
            response = await client.get(
                "https://export.arxiv.org/api/query",
                params={
                    "search_query": f"all:{params.query}",
                    "start": 0,
                    "max_results": params.max_results,
                    "sortBy": "relevance",
                    "sortOrder": "descending",
                },
            )
            response.raise_for_status()

        root = ET.fromstring(response.text)
        entries = root.findall("atom:entry", ATOM_NS)
        if not entries:
            return ToolResult(output="", error=f"No arXiv results for query: {params.query}")

        papers = []
        lines = [f"arXiv results for: {params.query}"]
        for index, entry in enumerate(entries, start=1):
            title = _text(entry, "atom:title")
            paper_url = _text(entry, "atom:id")
            arxiv_id = paper_url.rsplit("/", 1)[-1] if paper_url else ""
            published = _text(entry, "atom:published")[:10]
            summary = re.sub(r"\s+", " ", _text(entry, "atom:summary")).strip()
            authors = [
                _text(author, "atom:name")
                for author in entry.findall("atom:author", ATOM_NS)
                if _text(author, "atom:name")
            ]
            author_text = ", ".join(authors[:4]) + (" et al." if len(authors) > 4 else "")

            papers.append(
                {
                    "title": title,
                    "arxiv_id": arxiv_id,
                    "url": paper_url,
                    "published": published,
                    "authors": authors,
                }
            )
            lines.extend(
                [
                    f"{index}. {title}",
                    f"   arXiv ID: {arxiv_id}",
                    f"   Authors: {author_text}",
                    f"   Published: {published}",
                    f"   Abstract: {summary}",
                    f"   Source: {paper_url}",
                ]
            )

        return ToolResult(output="\n".join(lines), metadata={"query": params.query, "papers": papers})
    except (httpx.HTTPError, ET.ParseError) as exc:
        return ToolResult(output="", error=f"arXiv API unavailable: {exc}")


def _text(element: ET.Element, path: str) -> str:
    found = element.find(path, ATOM_NS)
    return (found.text or "").strip() if found is not None else ""


tool = define_tool(
    id="arxiv",
    description=(
        "Search arXiv for academic papers, especially ML, AI, quantitative finance, "
        "and research literature. Returns paper summaries, arXiv IDs, and source URLs."
    ),
    param_model=ArxivParams,
    execute_fn=execute,
)

