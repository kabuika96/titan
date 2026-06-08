from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.provider import LLMResponse
from harness.tool import ToolResult


class Tracer:
    def __init__(self, question: str, trace_dir: str = "traces") -> None:
        self.question = question
        self.trace_dir = Path(trace_dir)
        self.started = time.perf_counter()
        self.data: dict[str, Any] = {
            "question": question,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "steps": [],
            "final_answer": None,
            "tools_used": [],
            "total_steps": 0,
            "total_duration_ms": 0,
        }

    def step_start(self, step: int) -> None:
        self.data["total_steps"] = step

    def log_llm(self, step: int, response: LLMResponse) -> None:
        self.data["steps"].append(
            {
                "type": "llm",
                "step": step,
                "text": response.text,
                "tool_calls": [
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                    for call in response.tool_calls
                ],
                "usage": None
                if response.usage is None
                else {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
            }
        )

    def log_tool_call(self, name: str, args: dict[str, Any]) -> None:
        self.data["steps"].append({"type": "tool_call", "tool": name, "input": args})
        if name not in self.data["tools_used"]:
            self.data["tools_used"].append(name)

    def log_tool_result(self, name: str, result: ToolResult, duration_ms: int) -> None:
        self.data["steps"].append(
            {
                "type": "tool_result",
                "tool": name,
                "output": result.output,
                "metadata": result.metadata,
                "error": result.error,
                "duration_ms": duration_ms,
            }
        )

    def log_tool_error(self, name: str, error: str) -> None:
        self.data["steps"].append({"type": "tool_error", "tool": name, "error": error})

    def log_event(self, action: str, payload: dict[str, Any]) -> None:
        self.data["steps"].append({"type": "context_management", "action": action, **payload})

    def log_final(self, answer: str | None) -> None:
        self.data["final_answer"] = answer or ""

    def save(self) -> Path:
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.data["total_duration_ms"] = int((time.perf_counter() - self.started) * 1000)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        slug = re.sub(r"[^a-z0-9]+", "-", self.question.lower()).strip("-")[:48] or "run"
        path = self.trace_dir / f"{timestamp}_{slug}.json"
        path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        return path
