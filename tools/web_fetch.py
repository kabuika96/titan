from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

import httpx
import trafilatura
from pydantic import BaseModel, Field

from harness.tool import ToolResult, define_tool

USER_AGENT = "titan-research-agent/0.1 (interview assessment)"


class WebFetchParams(BaseModel):
    url: str = Field(description="Public http or https URL discovered by web_search or another tool result")


async def execute(params: WebFetchParams) -> ToolResult:
    try:
        _validate_public_url(params.url)
    except ValueError as exc:
        return ToolResult(output="", error=str(exc))

    try:
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1"},
        ) as client:
            response = await client.get(params.url)
            response.raise_for_status()

        final_url = str(response.url)
        try:
            _validate_public_url(final_url)
        except ValueError as exc:
            return ToolResult(output="", error=f"Refused redirected URL: {exc}")

        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if not _is_supported_content_type(content_type):
            return ToolResult(output="", error=f"Unsupported content type for web_fetch: {content_type or 'unknown'}")

        title = _html_title(response.text) or final_url
        extracted = _extract_markdown(response.text, final_url)
        if not extracted:
            return ToolResult(output="", error="web_fetch could not extract readable page content")

        output = f"# {title}\n\nSource: {final_url}\n\n{extracted}".strip()
        return ToolResult(
            output=output,
            metadata={
                "url": final_url,
                "title": title,
                "content_type": content_type,
            },
        )
    except (httpx.HTTPError, UnicodeDecodeError, ValueError) as exc:
        return ToolResult(output="", error=f"web_fetch unavailable: {exc}")


def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("web_fetch only supports http and https URLs")
    if not parsed.hostname:
        raise ValueError("web_fetch URL must include a hostname")

    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise ValueError("web_fetch refuses localhost URLs")

    try:
        default_port = 443 if parsed.scheme == "https" else 80
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(host, parsed.port or default_port, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve host '{host}': {exc}") from exc

    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError("web_fetch refuses private, local, reserved, or multicast network addresses")


def _is_supported_content_type(content_type: str) -> bool:
    return (
        content_type in {"", "text/html", "application/xhtml+xml"}
        or content_type.startswith("text/")
        or content_type in {"application/json", "application/xml", "application/rss+xml", "application/atom+xml"}
    )


def _extract_markdown(source: str, url: str) -> str:
    for options in (
        {"output_format": "markdown", "include_links": True},
        {"include_links": True},
        {},
    ):
        try:
            extracted = trafilatura.extract(source, url=url, **options)
        except (TypeError, ValueError):
            continue
        if extracted and extracted.strip():
            return _normalize_markdown(extracted)
    return ""


def _html_title(source: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", source, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match.group(1))).strip()
    return title or None


def _normalize_markdown(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text.strip())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


tool = define_tool(
    id="web_fetch",
    description=(
        "Fetch a public http or https URL discovered by web_search or another tool result, "
        "then extract clean readable content as markdown with trafilatura. Use this for "
        "relevant web sources that are not covered by wikipedia, arxiv, or FRED."
    ),
    param_model=WebFetchParams,
    execute_fn=execute,
    max_output_chars=6000,
)
