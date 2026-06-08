from __future__ import annotations

from typing import Any

from harness.tool import ToolDef

_registry: dict[str, ToolDef] = {}


def register(tool: ToolDef) -> None:
    _registry[tool.id] = tool


def clear() -> None:
    _registry.clear()


def resolve(tool_ids: list[str] | None = None) -> list[ToolDef]:
    if tool_ids is None:
        return list(_registry.values())
    return [tool for tool_id in tool_ids if (tool := _registry.get(tool_id))]


def get(tool_id: str) -> ToolDef | None:
    return _registry.get(tool_id)


def to_llm_tools(tool_ids: list[str] | None = None) -> list[dict[str, Any]]:
    return [
        {
            "name": tool.id,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        for tool in resolve(tool_ids)
    ]

