from __future__ import annotations

import os
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from harness.tool import ToolResult, define_tool

FRED_BASE_URL = "https://api.stlouisfed.org/fred"


class FredSearchParams(BaseModel):
    search_text: str = Field(description="Words to match against FRED economic data series")
    limit: int = Field(default=5, ge=1, le=10, description="Maximum series matches to return")


class FredDataParams(BaseModel):
    series_id: str = Field(description="FRED series ID, for example UNRATE, GDP, or FEDFUNDS")
    observation_start: Optional[str] = Field(default=None, description="Start date in YYYY-MM-DD format")
    observation_end: Optional[str] = Field(default=None, description="End date in YYYY-MM-DD format")
    limit: int = Field(default=24, ge=1, le=120, description="Maximum observations to return")


async def search_execute(params: FredSearchParams) -> ToolResult:
    api_key = os.getenv("FRED_API_KEY")
    if not api_key:
        return ToolResult(output="", error="FRED_API_KEY not configured")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{FRED_BASE_URL}/series/search",
                params={
                    "api_key": api_key,
                    "file_type": "json",
                    "search_text": params.search_text,
                    "limit": params.limit,
                    "order_by": "search_rank",
                },
            )
            response.raise_for_status()
            data = response.json()

        series = data.get("seriess", [])
        if not series:
            return ToolResult(output="", error=f"No FRED series found for: {params.search_text}")

        lines = [f"FRED series search results for: {params.search_text}"]
        metadata = []
        for index, item in enumerate(series, start=1):
            series_id = item.get("id", "")
            url = f"https://fred.stlouisfed.org/series/{series_id}"
            metadata.append({"id": series_id, "title": item.get("title"), "url": url})
            lines.extend(
                [
                    f"{index}. {series_id}: {item.get('title', '')}",
                    f"   Frequency: {item.get('frequency', '')}",
                    f"   Units: {item.get('units', '')}",
                    f"   Seasonal adjustment: {item.get('seasonal_adjustment', '')}",
                    f"   Last updated: {item.get('last_updated', '')}",
                    f"   Source: {url}",
                ]
            )
        return ToolResult(output="\n".join(lines), metadata={"series": metadata})
    except (httpx.HTTPError, ValueError) as exc:
        return ToolResult(output="", error=f"FRED API unavailable: {exc}")


async def data_execute(params: FredDataParams) -> ToolResult:
    api_key = os.getenv("FRED_API_KEY")
    if not api_key:
        return ToolResult(output="", error="FRED_API_KEY not configured")

    request_params: dict[str, object] = {
        "api_key": api_key,
        "file_type": "json",
        "series_id": params.series_id,
        "limit": params.limit,
        "sort_order": "desc",
    }
    if params.observation_start:
        request_params["observation_start"] = params.observation_start
    if params.observation_end:
        request_params["observation_end"] = params.observation_end

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{FRED_BASE_URL}/series/observations", params=request_params)
            response.raise_for_status()
            data = response.json()

        observations = list(reversed(data.get("observations", [])))
        if not observations:
            return ToolResult(output="", error=f"No FRED observations found for series: {params.series_id}")

        url = f"https://fred.stlouisfed.org/series/{params.series_id}"
        lines = [
            f"FRED observations for {params.series_id}",
            f"Observation window: {data.get('observation_start')} to {data.get('observation_end')}",
            f"Units: {data.get('units')}",
            f"Source: {url}",
            "Data:",
        ]
        for observation in observations:
            lines.append(f"- {observation.get('date')}: {observation.get('value')}")

        return ToolResult(
            output="\n".join(lines),
            metadata={
                "series_id": params.series_id,
                "url": url,
                "observations": observations,
            },
        )
    except (httpx.HTTPError, ValueError) as exc:
        return ToolResult(output="", error=f"FRED API unavailable: {exc}")


search_tool = define_tool(
    id="fred_search",
    description=(
        "Find FRED economic data series IDs by keyword. Use this before fred_data "
        "when you do not know the exact series ID."
    ),
    param_model=FredSearchParams,
    execute_fn=search_execute,
)

data_tool = define_tool(
    id="fred_data",
    description="Fetch observations for a known FRED economic data series ID.",
    param_model=FredDataParams,
    execute_fn=data_execute,
)
