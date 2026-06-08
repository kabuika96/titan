from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from harness import loop, registry
from tools import arxiv, fred, web_fetch, web_search, wikipedia


def main() -> None:
    load_local_env()

    parser = argparse.ArgumentParser(description="CLI research agent with citations.")
    parser.add_argument("question", nargs="+", help="Research question to answer")
    parser.add_argument("--max-steps", type=int, default=None, help="Override maximum agent loop steps")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show public progress notes on stderr. This is the default; kept for compatibility.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress public progress notes. Startup status and errors still go to stderr.",
    )
    args = parser.parse_args()
    question = " ".join(args.question)

    print(f'Researching: "{question}"', file=sys.stderr)
    print(f"Model: {model_description()}", file=sys.stderr)

    register_default_tools()

    agent = default_agent_config(verbose=not args.quiet or args.verbose)
    if args.max_steps is not None:
        agent["max_steps"] = args.max_steps

    try:
        answer = asyncio.run(loop.run(question, agent))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    else:
        sys.stdout.write(f"\n\n{answer}\n\n\n")


def load_local_env(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def register_default_tools() -> None:
    registry.clear()
    registry.register(wikipedia.tool)
    registry.register(arxiv.tool)
    registry.register(fred.search_tool)
    registry.register(fred.data_tool)
    registry.register(web_search.tool)
    registry.register(web_fetch.tool)


def default_agent_config(verbose: bool = True) -> dict:
    return {
        "tools": ["wikipedia", "arxiv", "fred_search", "fred_data", "web_search", "web_fetch"],
        "skills": ["skills/research.md"],
        "verbose": verbose,
    }


def model_description() -> str:
    provider = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
    if provider in {"ollama", "local"}:
        model = os.getenv("OLLAMA_MODEL") or os.getenv("MODEL_ID") or "qwen3:14b"
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        return f"ollama/{model} ({base_url})"
    if provider in {"gemini", "google"}:
        model = os.getenv("GEMINI_MODEL") or os.getenv("MODEL_ID") or "gemini-2.5-flash"
        return f"gemini/{model}"
    return f"{provider}/unknown"


if __name__ == "__main__":
    main()
