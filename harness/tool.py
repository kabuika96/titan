from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

DEFAULT_MAX_OUTPUT_CHARS = 4000


@dataclass
class ToolResult:
    output: str
    metadata: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class ToolDef:
    id: str
    description: str
    parameters: dict[str, Any]
    execute: Callable[[BaseModel], Awaitable[ToolResult]]
    pydantic_model: type[BaseModel]
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS


def define_tool(
    id: str,
    description: str,
    param_model: type[BaseModel],
    execute_fn: Callable[[BaseModel], Awaitable[ToolResult] | ToolResult],
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> ToolDef:
    """Create a tool definition and centrally enforce output truncation."""

    async def wrapped(params: BaseModel) -> ToolResult:
        result = execute_fn(params)
        if inspect.isawaitable(result):
            result = await result
        output = result.output or ""
        if len(output) > max_output_chars:
            output = output[:max_output_chars].rstrip() + "\n[truncated]"
        return ToolResult(output=output, metadata=result.metadata, error=result.error)

    return ToolDef(
        id=id,
        description=description,
        parameters=_provider_schema(param_model),
        execute=wrapped,
        pydantic_model=param_model,
        max_output_chars=max_output_chars,
    )


def _provider_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Convert Pydantic JSON Schema to the provider function schema subset."""
    schema = model.model_json_schema()
    return _simplify_schema(schema)


def _simplify_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [_simplify_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    if "anyOf" in value:
        non_null = [
            option
            for option in value["anyOf"]
            if not (isinstance(option, dict) and option.get("type") == "null")
        ]
        if len(non_null) == 1:
            simplified = _simplify_schema(non_null[0])
            if "description" in value and isinstance(simplified, dict):
                simplified.setdefault("description", value["description"])
            return simplified

    allowed = {
        "type",
        "description",
        "properties",
        "required",
        "items",
        "enum",
    }
    simplified: dict[str, Any] = {}
    for key, item in value.items():
        if key == "properties" and isinstance(item, dict):
            simplified[key] = {
                property_name: _simplify_schema(property_schema)
                for property_name, property_schema in item.items()
            }
        elif key in allowed:
            simplified[key] = _simplify_schema(item)
    return simplified
