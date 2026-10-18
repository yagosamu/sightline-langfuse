import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolResultBlock,
                              ToolUseBlock, UserMessage, query)

from context.config import DATA_DIR, settings
from observability.prompts import SYSTEM_PROMPT_NAME, LoadedPrompt, load_prompt
from observability.tracing import attribute_headers, observation, record, trace_attributes
from observability.tracing.agent import MODEL_USAGE_NAME, cli_otel_env, model_usage_generations

SYSTEM_PROMPT = Path(__file__).with_name("system_prompt.md").read_text(encoding="utf-8")
TRUTH_FILE = DATA_DIR / "truth" / "questions.jsonl"


@dataclass
class AgentRun:
    question: str
    as_of: str
    answer: str
    tool_calls: list[dict] = field(default_factory=list)
    num_turns: int = 0
    cost_usd: float | None = None
    model_usage: dict = field(default_factory=dict)
    duration_ms: int = 0
    session_id: str | None = None
    is_error: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    def retrieved_sources(self) -> set[str]:
        """Source paths of the facts Helix returned to the agent."""
        return {path for call in self.tool_calls for fact in call.get("result", {}).get("facts", [])
                for path in fact["sources"]}

    def read_sources(self) -> set[str]:
        """Source documents the agent read in full with read_source."""
        return {call["result"]["path"] for call in self.tool_calls
                if call["tool"] == "read_source" and call.get("result", {}).get("path")
                and not call["result"].get("error")}


def compact_result(tool: str, content: str | list[dict] | None) -> dict:
    """What a Helix tool returned, small enough to keep with the run: facts with status and sources."""
    texts = [content] if isinstance(content, str) else [b.get("text", "") for b in content or [] if b.get("type") == "text"]
    try:
        data = json.loads("".join(texts))
    except ValueError:
        return {"error": "unreadable tool result"}
    if not isinstance(data, dict):
        return {"error": "unexpected tool result"}
    if tool == "search_context":
        return {"facts": [{"fact": f["fact"], "status": f["status"], "sources": [s["path"] for s in f.get("sources", [])]}
                          for f in data.get("facts", [])],
                "hidden_as_future": data.get("hidden_as_future", 0)}
    if tool == "find_entities":
        return {"entities": [e["name"] for e in data.get("entities", [])]}
    if tool == "read_source":
        return {"path": data.get("path"), "error": data.get("error")}
    if tool == "list_sources":
        return {"documents": sum(len(docs) for docs in data.get("systems", {}).values())}
    return {}


def load_truth() -> dict[str, dict]:
    return {q["id"]: q for q in map(json.loads, TRUTH_FILE.read_text().splitlines())}


def agent_env() -> dict[str, str]:
    if os.environ.get("HELIX_AGENT_PROVIDER") == "openrouter":
        return {
            "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
            "ANTHROPIC_AUTH_TOKEN": os.environ["OPENROUTER_API_KEY"],
            "ANTHROPIC_API_KEY": "",
        }
    return {}


def build_options(model: str | None = None, system_prompt: str = SYSTEM_PROMPT, user_id: str | None = None,
                  session_id: str | None = None, tags: list[str] | None = None,
                  prompt: LoadedPrompt | None = None, cli_config_dir: str | None = None) -> ClaudeAgentOptions:
    """user_id, session_id and tags label the trace in every process: the CLI through its environment,
    Helix through the MCP request headers. A managed prompt is linked to the CLI's generations too.

    `cli_config_dir` runs the Claude Code CLI with its own empty config directory (CLAUDE_CONFIG_DIR).
    """
    s = settings()
    helix = {"type": "http", "url": s.mcp_url}
    if headers := attribute_headers(user_id, session_id, tags):
        helix["headers"] = headers
    config = {"CLAUDE_CONFIG_DIR": cli_config_dir} if cli_config_dir else {}
    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model or s.agent_model,
        max_turns=15,
        mcp_servers={"helix": helix},
        strict_mcp_config=True,
        tools=[],
        allowed_tools=["mcp__helix__*"],
        permission_mode="dontAsk",
        env=agent_env() | cli_otel_env(user_id, session_id, prompt) | config,
    )


def user_prompt(question: str, as_of: str) -> str:
    return f"As-of date: {as_of}\n\nQuestion: {question}"


def check_helix() -> None:
    url = settings().mcp_url
    try:
        httpx.get(url, timeout=3)
    except httpx.HTTPError as exc:
        raise SystemExit(f"Helix MCP server is not reachable at {url} ({exc.__class__.__name__}). "
                         "Start it with: uv run python -m context.mcp") from exc


def system_prompt_for(label: str | None = None) -> LoadedPrompt:
    """The system prompt with this label from Langfuse, or system_prompt.md when Langfuse is unavailable."""
    return load_prompt(SYSTEM_PROMPT_NAME, fallback=SYSTEM_PROMPT, label=label)


async def ask(question: str, as_of: str, model: str | None = None, system_prompt: str | None = None, *,
              prompt_label: str | None = None, user_id: str | None = None, session_id: str | None = None,
              tags: list[str] | None = None, isolate_cli_config: bool = False) -> AgentRun:
    """Answer one question. `system_prompt` overrides the managed prompt (it is then not linked).

    `isolate_cli_config` gives this question's CLI a fresh, temporary config directory. Use it when one
    process asks many questions (experiments): after the first run in a config directory, the CLI stops
    exporting its interaction and tool spans, which breaks the trace. A fresh directory also keeps a
    developer's own settings, hooks and plugins out of the run. It drops a Claude login, so it needs
    ANTHROPIC_API_KEY.
    """
    arguments = (question, as_of, model, system_prompt, prompt_label, user_id, session_id, tags)
    if not isolate_cli_config:
        return await _ask(*arguments, cli_config_dir=None)
    with tempfile.TemporaryDirectory(prefix="claude-config-", ignore_cleanup_errors=True) as config_dir:
        return await _ask(*arguments, cli_config_dir=config_dir)


async def _ask(question: str, as_of: str, model: str | None, system_prompt: str | None, prompt_label: str | None,
               user_id: str | None, session_id: str | None, tags: list[str] | None,
               cli_config_dir: str | None) -> AgentRun:
    prompt = system_prompt_for(prompt_label) if system_prompt is None else LoadedPrompt(
        text=system_prompt, name=SYSTEM_PROMPT_NAME, label="override", version=None, source="argument")
    options = build_options(model, prompt.text, user_id, session_id, tags, prompt=prompt, cli_config_dir=cli_config_dir)
    attributes = trace_attributes(user_id, session_id, tags, prompt=prompt.client)
    # The Agent SDK passes this observation to the CLI as TRACEPARENT, so the CLI's spans nest under it.
    with attributes, observation("answer-question", "agent", input={"question": question, "as_of": as_of}) as obs:
        run = await _run(question, as_of, options)
        for generation in model_usage_generations(run.model_usage):
            with observation(MODEL_USAGE_NAME, "generation", **generation):
                pass
        record(obs, output=run.answer, level="ERROR" if run.is_error else "DEFAULT", metadata={
            "as_of": as_of,
            "model": model or settings().agent_model,
            "cost_usd": run.cost_usd,
            "num_turns": run.num_turns,
            "duration_ms": run.duration_ms,
            "tool_call_count": len(run.tool_calls),
            "session_id": run.session_id,
            "is_error": run.is_error,
            **prompt.trace_metadata(),
        })
    return run


async def _run(question: str, as_of: str, options: ClaudeAgentOptions) -> AgentRun:
    texts, calls, result = [], [], None
    async for message in query(prompt=user_prompt(question, as_of), options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    texts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    calls.append({"id": block.id, "tool": block.name.removeprefix("mcp__helix__"), "input": block.input})
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    call = next((c for c in calls if c["id"] == block.tool_use_id), None)
                    if call is not None:
                        call["result"] = ({"error": "tool error"} if block.is_error
                                          else compact_result(call["tool"], block.content))
        elif isinstance(message, ResultMessage):
            result = message
    return AgentRun(
        question=question,
        as_of=as_of,
        answer=(result.result if result and result.result else "\n".join(texts)).strip(),
        tool_calls=calls,
        num_turns=result.num_turns if result else 0,
        cost_usd=result.total_cost_usd if result else None,
        model_usage=(result.model_usage or {}) if result else {},
        duration_ms=result.duration_ms if result else 0,
        session_id=result.session_id if result else None,
        is_error=bool(result.is_error) if result else True,
    )
