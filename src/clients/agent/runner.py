import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock, query

from context.config import DATA_DIR, settings
from observability.tracing import observation, record
from observability.tracing.agent import cli_otel_env

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


def build_options(model: str | None = None, system_prompt: str = SYSTEM_PROMPT) -> ClaudeAgentOptions:
    s = settings()
    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model or s.agent_model,
        max_turns=15,
        mcp_servers={"helix": {"type": "http", "url": s.mcp_url}},
        strict_mcp_config=True,
        tools=[],
        allowed_tools=["mcp__helix__*"],
        permission_mode="dontAsk",
        env=agent_env() | cli_otel_env(),
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


async def ask(question: str, as_of: str, model: str | None = None, system_prompt: str = SYSTEM_PROMPT) -> AgentRun:
    # The Agent SDK passes this observation to the CLI as TRACEPARENT, so the CLI's spans nest under it.
    with observation("answer-question", "agent", input={"question": question, "as_of": as_of}) as obs:
        run = await _run(question, as_of, model, system_prompt)
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


async def _run(question: str, as_of: str, model: str | None, system_prompt: str) -> AgentRun:
    texts, calls, result = [], [], None
    async for message in query(prompt=user_prompt(question, as_of), options=build_options(model, system_prompt)):
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
        duration_ms=result.duration_ms if result else 0,
        session_id=result.session_id if result else None,
        is_error=bool(result.is_error) if result else True,
    )
