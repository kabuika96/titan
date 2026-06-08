# Research Agent Framework

A small CLI research agent:

```bash
.venv/bin/python agent.py "What is Basel III?"
```

The harness is intentionally thin: it registers tools, runs a bounded provider tool-calling loop, manages context, and writes traces. The skill prompt carries the research strategy, tool routing guidance, and citation rules.

## Architecture

```text
question
  -> loop: check context -> LLM provider -> execute tools -> repeat
  -> cited answer on stdout
```

Files:

- `agent.py`: CLI entrypoint.
- `harness/tool.py`: tool dataclasses and `define_tool()` factory with centralized truncation.
- `harness/registry.py`: tool registry and provider tool-declaration conversion.
- `harness/provider.py`: Ollama/Gemini wrapper with manual function calling, token usage, and retry.
- `harness/loop.py`: bounded agentic state machine with pruning and compaction.
- `harness/tracer.py`: JSON traces under `traces/`.
- `tools/wikipedia.py`: MediaWiki search plus Wikipedia page summary.
- `tools/arxiv.py`: arXiv Atom search parser.
- `tools/fred.py`: FRED series search and observation retrieval.
- `tools/web_search.py`: DuckDuckGo-backed public web discovery.
- `tools/web_fetch.py`: generic public URL fetcher that extracts clean markdown with trafilatura.
- `skills/research.md`: research strategy and citation rules.
- `skills/compaction.md`: overflow summary template.

## Design Decisions

- Ollama with `qwen3:14b` is the default LLM so the app runs locally without cloud quota or API keys.
- Gemini via Google AI Studio remains available as an optional provider for reviewers who prefer a hosted model.
- The implementation avoids LangChain/LlamaIndex; the useful abstraction here is only a registry plus a loop.
- Tool calls are model-directed, but code validates tool names and arguments before execution.
- stdout contains only the final answer. Progress, warnings, and errors go to stderr.
- FRED is split into `fred_search` and `fred_data` so the model discovers series IDs before fetching observations.
- `web_search` and `web_fetch` supplement the specific tools. Wikipedia, arXiv, and FRED stay preferred for their domains; generic web search is a fallback for current information, insufficient specific-tool results, or cross-checking.
- `web_fetch` only runs on URLs that appeared in an earlier tool result, which keeps arbitrary fetches and hallucinated URLs out of the normal path.
- Context management starts with cheap pruning of old tool outputs, then uses an LLM compaction prompt only if the context budget is exceeded.

## Setup

Requires Python 3.11+. On macOS, avoid the system `/usr/bin/python3`; it is often Python 3.9 linked against LibreSSL, which causes noisy dependency warnings.

```bash
uv venv --python 3.12 --seed .venv
uv pip install -r requirements.txt
```

If you already have a modern interpreter installed, this also works:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Default LLM setup:

```bash
ollama pull qwen3:14b
```

Start the local Ollama server. On macOS, the official app is the most reliable path:

```bash
brew install --cask ollama-app
open -a Ollama
```

If you have a healthy CLI install, `ollama serve` also works. If the Homebrew formula errors with `llama-server binary not found`, use the app/cask runtime instead.

Environment variables:

```bash
export LLM_PROVIDER="ollama"                  # default
export OLLAMA_MODEL="qwen3:14b"               # default
export OLLAMA_BASE_URL="http://localhost:11434"
export FRED_API_KEY="..."                     # free; needed only for FRED data questions
```

To use Gemini instead:

```bash
export LLM_PROVIDER="gemini"
export GEMINI_API_KEY="..."                   # free from Google AI Studio
export GEMINI_MODEL="gemini-2.5-flash"        # default Gemini model
```

The CLI also loads a local `.env` file automatically:

```text
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen3:14b
FRED_API_KEY=...
```

Gemini `.env` example:

```text
LLM_PROVIDER=gemini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash
FRED_API_KEY=...
```

## Run

```bash
.venv/bin/python agent.py "What is the Fed discount window?"
.venv/bin/python agent.py "What is the current US unemployment rate?"
.venv/bin/python agent.py --max-steps 1 "Compare 2008 and COVID monetary policy responses"
```

Startup status is written to stderr and includes the selected provider/model:

```text
Researching: "What is the Fed discount window?"
Model: ollama/qwen3:14b (http://localhost:11434)
```

After `source .venv/bin/activate`, the shorter `python agent.py "..."` form also works.

Progress narration is shown by default on stderr, while stdout still contains only the final answer:

```text
-> Investigating the Fed discount window with sourced evidence.

-> Looking up the Fed discount window on Wikipedia for reliable background.

-> Found the relevant Wikipedia article and can now write a concise sourced answer.
```

These are LLM-written public progress notes derived from structured event data; they do not expose hidden model reasoning.

Use `--quiet` to suppress progress notes:

```bash
.venv/bin/python agent.py --quiet "What is the Fed discount window?"
```

Each run writes a trace file to `traces/{timestamp}_{question}.json`.

## Reference Questions

| # | Question | Expected tools | Expected behavior |
|---|----------|----------------|-------------------|
| 1 | Fed discount window | `wikipedia` | Single-source factual answer |
| 2 | Basel III capital requirements | `wikipedia` | Single-source factual answer |
| 3 | ML for credit risk | `arxiv` | Academic search over multiple papers |
| 4 | 2008 vs COVID monetary policy | `wikipedia` | Multi-step comparison |
| 5 | US unemployment rate | `fred_search`, `fred_data` | Data retrieval and formatting |
| 6 | Yield curve inversions plus papers | `wikipedia`, `arxiv` | Cross-tool synthesis |
| 7 | Best restaurant in NYC | none | Out-of-scope response |
| 8 | Quantum computing and banking | `wikipedia`, `arxiv`, optional `web_search`, `web_fetch` | Hedged synthesis |

The generic web path is `web_search` -> `web_fetch`. Use it for relevant public web sources outside the specific Wikipedia/arXiv/FRED tools, for recent information, or for cross-checking claims.

For the multi-step comparison, run questions 4 and 6 once normally and once with `--max-steps 1`, then capture the outputs in `outputs/`.

## Verification Status

Completed locally in this shell:

- `python -m compileall agent.py harness tools`
- Standalone Wikipedia tool smoke test against `Basel III`
- Standalone arXiv tool smoke test against `credit risk machine learning`
- Direct FRED `UNRATE` data smoke test with `FRED_API_KEY`
- Direct `web_search` error-path smoke test against DuckDuckGo rate limiting
- Direct `web_fetch` smoke test against a public HTML page with trafilatura extraction
- Loop URL-discovery guard accepts URLs from tool results and rejects undiscovered URLs
- Mocked provider loop: provider requests `wikipedia`, loop executes it, tracer writes JSON, final answer returns on stdout
- CLI missing-key path exits cleanly with `GEMINI_API_KEY is not configured`
- Real Gemini loop against `What is Basel III?`
- Real Gemini plus FRED loop against `What is the latest US unemployment rate?`
- Ollama local server and `qwen3:14b` model discovery
- Real Ollama loop against `What is Basel III?`
- Real Ollama plus FRED loop against `What is the latest US unemployment rate?`

Not completed yet:

- Full 8-question output capture
- Successful live `web_search` result capture; DuckDuckGo returned `202 Ratelimit` from this network during the latest local check

## Extensibility

To add a tool:

1. Define a Pydantic params model.
2. Implement `async execute(params) -> ToolResult`.
3. Export `tool = define_tool(...)`.
4. Register it in `agent.py`.

No harness changes are needed unless the tool contract itself changes.

## Limitations

- Tool calls execute sequentially; parallel independent calls could use `asyncio.gather`.
- There is no persistence beyond per-run traces.
- There is no response cache for repeated public API calls.
- FRED requires a free API key, so economic-data answers degrade gracefully when it is missing.
- The default Ollama provider requires a working local Ollama server with a tool-capable model such as `qwen3:14b`.
- `web_search` depends on DuckDuckGo's public search surface and can fail if that service blocks, throttles, or changes its response behavior.
- `web_fetch` only fetches public `http`/`https` URLs that were discovered by a previous tool result and refuses localhost/private-network addresses.

## API References

- Ollama tool calling: https://docs.ollama.com/capabilities/tool-calling
- Ollama chat API: https://docs.ollama.com/api/chat
- Gemini function calling: https://ai.google.dev/gemini-api/docs/function-calling
- Gemini token usage metadata: https://ai.google.dev/gemini-api/docs/tokens
- MediaWiki search API: https://www.mediawiki.org/wiki/API:Search
- arXiv API manual: https://info.arxiv.org/help/api/user-manual.html
- FRED series search: https://fred.stlouisfed.org/docs/api/fred/series_search.html
- FRED observations: https://fred.stlouisfed.org/docs/api/fred/series_observations.html
- duckduckgo-search package: https://pypi.org/project/duckduckgo-search/
- trafilatura docs: https://trafilatura.readthedocs.io/
