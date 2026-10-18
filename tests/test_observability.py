import os
import subprocess
import sys
import time

import pytest

from observability import tracing
from observability.tracing import helix
from observability.tracing.agent import cli_otel_env, model_usage_generations

# Runs in a fresh process, so the parent's environment and Langfuse client do not leak in.
HELIX_TOOL_CALLS = """
import asyncio
from context.mcp import server
from observability import tracing

msa = asyncio.run(server.read_source(path="drive/Orion_Freight_Systems_MSA_2025-08-15.pdf"))
assert "7.3 SLA refund right" in msa["content"], msa
assert asyncio.run(server.list_sources())["systems"]["drive"]
tracing.shutdown()
print("enabled" if tracing.langfuse_enabled() else "disabled")
"""


@pytest.fixture
def no_langfuse(monkeypatch):
    for key in tracing.REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(tracing, "_client", None)


def test_tracing_is_off_without_keys(no_langfuse):
    assert not tracing.langfuse_enabled()
    assert set(tracing.missing_env()) == set(tracing.REQUIRED_ENV)
    assert tracing.get_langfuse() is None


async def test_traced_function_works_without_langfuse(no_langfuse):
    @tracing.traced("search-context", "retriever")
    async def tool(query: str) -> dict:
        return {"query": query}

    assert await tool(query="who approves refunds") == {"query": "who approves refunds"}


async def test_traced_function_errors_still_raise(no_langfuse):
    @tracing.traced("read-source", "tool")
    async def tool() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await tool()


@pytest.fixture
def fake_langfuse(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-1")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-2")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://langfuse-web:3000/")


def test_cli_otel_env_is_empty_without_keys(no_langfuse):
    assert cli_otel_env() == {}


def test_cli_otel_env_exports_traces_to_langfuse(fake_langfuse):
    env = cli_otel_env()
    assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == "http://langfuse-web:3000/api/public/otel/v1/traces"
    # base64("pk-lf-1:sk-lf-2")
    assert env["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] == (
        "Authorization=Basic cGstbGYtMTpzay1sZi0y,x-langfuse-ingestion-version=4")
    assert env["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"] == "http/protobuf"
    assert env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] == "1"
    assert env["OTEL_SERVICE_NAME"] == "ask-orbisa"


def test_cli_otel_env_never_writes_to_stdout(fake_langfuse):
    exporters = [v for k, v in cli_otel_env().items() if k.endswith("_EXPORTER")]
    assert exporters and "console" not in ",".join(exporters)


def test_model_usage_becomes_one_generation_per_model():
    model_usage = {
        "claude-opus-5-5": {"inputTokens": 12, "outputTokens": 2449, "cacheReadInputTokens": 35756,
                            "cacheCreationInputTokens": 19627, "costUSD": 0.1489},
        "claude-haiku-5-5": {"inputTokens": 1220, "outputTokens": 24, "costUSD": 0.0013},
    }
    generations = model_usage_generations(model_usage)
    assert [g["model"] for g in generations] == ["claude-haiku-5-5", "claude-opus-5-5"]
    assert generations[1]["usage_details"] == {"input": 12, "output": 2449, "cache_read_input_tokens": 35756,
                                               "cache_creation_input_tokens": 19627}
    assert generations[0]["usage_details"]["cache_read_input_tokens"] == 0
    assert generations[1]["output"] == {"model": "claude-opus-5-5", "tokens": generations[1]["usage_details"],
                                        "cost_usd": 0.1489, "source": "ResultMessage.model_usage"}
    assert sum(g["cost_details"]["total"] for g in generations) == pytest.approx(0.1502)
    assert model_usage_generations(None) == []


def test_agent_options_export_traces_only_when_langfuse_is_configured(no_langfuse, monkeypatch):
    from clients.agent.runner import build_options

    assert not any(k.startswith(("OTEL_", "CLAUDE_CODE_")) for k in build_options().env)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-1")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-2")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:3000")
    env = build_options().env
    assert env["OTEL_TRACES_EXPORTER"] == "otlp"
    assert "TRACEPARENT" not in env  # the SDK injects it from the active span; setting it would pin the parent


def test_observation_and_record_are_noops_without_langfuse(no_langfuse):
    with tracing.observation("search-graph", "retriever") as obs:
        tracing.record(obs, output={"edges": 3})
    assert obs is None


def test_search_context_summary():
    result = {
        "as_of": "2026-09-29",
        "hidden_as_future": 2,
        "facts": [
            {"fact": "Tomás approves refunds", "status": "true on as_of", "valid_from": "2026-03-03",
             "valid_until": None, "sources": [{"path": "slack/revops/2026-03-03.json"}]},
            {"fact": "Finance approves refunds", "status": "no longer true on as_of", "valid_from": "2023-02-01",
             "valid_until": "2026-03-03", "sources": [{"path": "confluence/Refund-Policy-2023_98304112.html"}]},
        ],
    }
    output, metadata = helix.search_context(result)
    assert output["facts"][0]["sources"] == ["slack/revops/2026-03-03.json"]
    assert metadata == {
        "as_of": "2026-09-29",
        "fact_count": 2,
        "hidden_as_future": 2,
        "status_counts": {"true on as_of": 1, "no longer true on as_of": 1},
        "source_systems": ["confluence", "slack"],
        "source_paths": ["confluence/Refund-Policy-2023_98304112.html", "slack/revops/2026-03-03.json"],
    }


def test_read_source_summary_keeps_only_a_preview():
    content = "x" * 10_000
    output, metadata = helix.read_source({"path": "drive/msa.pdf", "truncated": False, "content": content})
    assert len(output["preview"]) == helix.PREVIEW_CHARS
    assert output["length"] == 10_000
    assert metadata == {"path": "drive/msa.pdf", "system": "drive", "length": 10_000}


def test_read_source_summary_keeps_errors():
    output, metadata = helix.read_source({"path": "nope", "error": "not a source document"})
    assert metadata["error"] == "not a source document"


async def test_mcp_tool_schemas_survive_tracing():
    from context.mcp import server

    schemas = {t.name: sorted(t.input_schema.get("properties", {})) for t in await server.mcp.list_tools()}
    assert schemas == {"search_context": ["as_of", "limit", "query"], "find_entities": ["limit", "query"],
                       "read_source": ["path"], "list_sources": []}


def run_helix_tools(tmp_path, **langfuse_env: str) -> subprocess.CompletedProcess:
    empty_env_file = tmp_path / "empty.env"
    empty_env_file.write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith("LANGFUSE_")}
    # graphiti_core calls load_dotenv() on import, which finds the repo .env from its own location in
    # .venv. It never overrides a variable that is already set, so empty values keep the keys missing.
    env.update(dict.fromkeys(tracing.REQUIRED_ENV, "") | langfuse_env, HELIX_ENV_FILE=str(empty_env_file))
    return subprocess.run([sys.executable, "-c", HELIX_TOOL_CALLS], env=env, capture_output=True, text=True, timeout=120)


def test_helix_tools_work_without_langfuse_keys(tmp_path):
    run = run_helix_tools(tmp_path)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "disabled"


def test_helix_tools_work_when_langfuse_is_unreachable(tmp_path):
    started = time.monotonic()
    run = run_helix_tools(tmp_path, LANGFUSE_PUBLIC_KEY="pk-lf-test", LANGFUSE_SECRET_KEY="sk-lf-test",
                          LANGFUSE_BASE_URL="http://127.0.0.1:9", LANGFUSE_TIMEOUT="2")
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "enabled"
    assert time.monotonic() - started < 60
