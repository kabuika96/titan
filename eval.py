from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent import default_agent_config, load_local_env, model_description, register_default_tools
from harness import loop

DEFAULT_DATASET = "evals/research_dataset.json"
DEFAULT_TRACE_DIR = "traces/eval"


@dataclass
class EvalResult:
    case_id: str
    status: str
    duration_s: float
    tools_used: list[str]
    reason: str = ""


def main() -> None:
    load_local_env()

    parser = argparse.ArgumentParser(description="Run the research-agent eval dataset.")
    parser.add_argument("--dataset", default=DEFAULT_DATASET, help="Path to eval dataset JSON")
    parser.add_argument("--case", action="append", dest="case_ids", help="Run only this case id; repeatable")
    parser.add_argument("--limit", type=int, default=None, help="Run only the first N selected cases")
    parser.add_argument("--max-steps", type=int, default=None, help="Override max agent loop steps for every case")
    parser.add_argument("--trace-dir", default=DEFAULT_TRACE_DIR, help="Directory for eval traces")
    parser.add_argument("--list", action="store_true", help="List dataset cases without running them")
    args = parser.parse_args()

    dataset = load_dataset(Path(args.dataset))
    cases = select_cases(dataset["cases"], args.case_ids, args.limit)

    if args.list:
        for case in cases:
            print(f"{case['id']}: {case['question']}")
        return

    results = asyncio.run(run_dataset(dataset, cases, args))
    failed = sum(1 for result in results if result.status == "FAIL")
    raise SystemExit(1 if failed else 0)


def load_dataset(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return {"name": path.stem, "cases": payload}
    if "cases" not in payload or not isinstance(payload["cases"], list):
        raise ValueError(f"Eval dataset must contain a 'cases' list: {path}")
    return payload


def select_cases(cases: list[dict[str, Any]], case_ids: list[str] | None, limit: int | None) -> list[dict[str, Any]]:
    selected = cases
    if case_ids:
        requested = set(case_ids)
        selected = [case for case in selected if case["id"] in requested]
        missing = sorted(requested - {case["id"] for case in selected})
        if missing:
            raise ValueError(f"Unknown eval case id(s): {', '.join(missing)}")
    if limit is not None:
        selected = selected[:limit]
    return selected


async def run_dataset(dataset: dict[str, Any], cases: list[dict[str, Any]], args: argparse.Namespace) -> list[EvalResult]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    trace_root = Path(args.trace_dir) / run_id
    results: list[EvalResult] = []
    started = time.perf_counter()

    print(f"Eval: {dataset.get('name', 'dataset')} | model: {model_description()} | cases: {len(cases)}")
    register_default_tools()

    for case in cases:
        result = await run_case(case, trace_root, args.max_steps)
        results.append(result)
        print(format_result(result), flush=True)

    duration_s = time.perf_counter() - started
    passed = sum(1 for result in results if result.status == "PASS")
    failed = sum(1 for result in results if result.status == "FAIL")
    skipped = sum(1 for result in results if result.status == "SKIP")
    print(f"Summary: {passed} pass, {failed} fail, {skipped} skip in {duration_s:.1f}s")
    return results


async def run_case(case: dict[str, Any], trace_root: Path, max_steps_override: int | None) -> EvalResult:
    missing_env = [name for name in case.get("required_env", []) if not os.getenv(name)]
    if missing_env:
        return EvalResult(
            case_id=case["id"],
            status="SKIP",
            duration_s=0.0,
            tools_used=[],
            reason=f"missing env: {', '.join(missing_env)}",
        )

    case_trace_dir = trace_root / case["id"]
    agent_config = default_agent_config(verbose=False)
    agent_config["trace_dir"] = str(case_trace_dir)
    max_steps = max_steps_override if max_steps_override is not None else case.get("max_steps")
    if max_steps is not None:
        agent_config["max_steps"] = int(max_steps)

    started = time.perf_counter()
    try:
        answer = await loop.run(str(case["question"]), agent_config)
        trace = read_case_trace(case_trace_dir)
    except Exception as exc:
        return EvalResult(
            case_id=case["id"],
            status="FAIL",
            duration_s=time.perf_counter() - started,
            tools_used=[],
            reason=f"runtime error: {exc}",
        )

    failures = score_case(case, answer, trace)
    return EvalResult(
        case_id=case["id"],
        status="FAIL" if failures else "PASS",
        duration_s=time.perf_counter() - started,
        tools_used=list(trace.get("tools_used", [])),
        reason="; ".join(failures),
    )


def read_case_trace(trace_dir: Path) -> dict[str, Any]:
    files = sorted(trace_dir.glob("*.json"), key=lambda path: path.stat().st_mtime)
    if not files:
        raise RuntimeError(f"No trace written in {trace_dir}")
    return json.loads(files[-1].read_text(encoding="utf-8"))


def score_case(case: dict[str, Any], answer: str, trace: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    tools_used = set(trace.get("tools_used", []))
    answer_text = answer or trace.get("final_answer") or ""

    if not answer_text.strip():
        failures.append("empty answer")

    for tool in case.get("expected_tools", []):
        if tool not in tools_used:
            failures.append(f"missing tool {tool}")

    for tool in case.get("forbidden_tools", []):
        if tool in tools_used:
            failures.append(f"forbidden tool {tool}")

    if case.get("require_citation", True) and not has_citation(answer_text):
        failures.append("missing citation")

    for term in case.get("must_include", []):
        if term.lower() not in answer_text.lower():
            failures.append(f"missing term '{term}'")

    for term in case.get("must_not_include", []):
        if term.lower() in answer_text.lower():
            failures.append(f"forbidden term '{term}'")

    if case.get("expect_out_of_scope"):
        if tools_used:
            failures.append("out-of-scope case used tools")
        if not looks_out_of_scope(answer_text):
            failures.append("out-of-scope answer not explicit")

    failures.extend(tool_error_failures(case, trace))
    return failures


def has_citation(text: str) -> bool:
    return bool(re.search(r"\[(Wikipedia|arXiv|FRED|Web):[^\]]+\]\(https?://[^)]+\)", text))


def looks_out_of_scope(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "outside my research capabilities",
            "outside the scope",
            "out of scope",
            "subjective",
        )
    )


def tool_error_failures(case: dict[str, Any], trace: dict[str, Any]) -> list[str]:
    expected_tools = set(case.get("expected_tools", []))
    failures = []
    for step in trace.get("steps", []):
        if step.get("type") == "tool_result" and step.get("tool") in expected_tools and step.get("error"):
            failures.append(f"{step['tool']} returned error")
        if step.get("type") == "tool_error" and step.get("tool") in expected_tools:
            failures.append(f"{step['tool']} errored")
    return failures


def format_result(result: EvalResult) -> str:
    tools = ",".join(result.tools_used) if result.tools_used else "-"
    suffix = f" | {result.reason}" if result.reason else ""
    return f"{result.status:<4} {result.case_id:<32} {result.duration_s:>5.1f}s tools={tools}{suffix}"


if __name__ == "__main__":
    main()
