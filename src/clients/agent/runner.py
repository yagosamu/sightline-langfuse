import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock, query

from context.config import DATA_DIR, settings
from observability.tracing import attribute_headers, observation, record, trace_attributes
from observability.tracing.agent import MODEL_USAGE_NAME, cli_otel_env, model_usage_generations

SYSTEM_PROMPT = Path(__file__).with_name("system_prompt.md").read_text()
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
                  session_id: str | None = None, tags: list[str] | None = None) -> ClaudeAgentOptions:
    """user_id, session_id and tags label the trace in every process: the CLI through its environment,
    Helix through the MCP request headers."""
    s = settings()
    helix = {"type": "http", "url": s.mcp_url}
    if headers := attribute_headers(user_id, session_id, tags):
        helix["headers"] = headers
    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model or s.agent_model,
        max_turns=15,
        mcp_servers={"helix": helix},
        strict_mcp_config=True,
        tools=[],
        allowed_tools=["mcp__helix__*"],
        permission_mode="dontAsk",
        env=agent_env() | cli_otel_env(user_id, session_id),
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


async def ask(question: str, as_of: str, model: str | None = None, system_prompt: str = SYSTEM_PROMPT, *,
              user_id: str | None = None, session_id: str | None = None, tags: list[str] | None = None) -> AgentRun:
    options = build_options(model, system_prompt, user_id, session_id, tags)
    # The Agent SDK passes this observation to the CLI as TRACEPARENT, so the CLI's spans nest under it.
    with trace_attributes(user_id, session_id, tags),             observation("answer-question", "agent", input={"question": question, "as_of": as_of}) as obs:
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
                    calls.append({"tool": block.name.removeprefix("mcp__helix__"), "input": block.input})
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
