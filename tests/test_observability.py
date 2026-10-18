import contextlib
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


def test_cli_otel_env_propagates_traceparent_through_openrouter(fake_langfuse, monkeypatch):
    monkeypatch.delenv("HELIX_AGENT_PROVIDER", raising=False)
    assert "CLAUDE_CODE_PROPAGATE_TRACEPARENT" not in cli_otel_env()
    monkeypatch.setenv("HELIX_AGENT_PROVIDER", "openrouter")
    assert cli_otel_env()["CLAUDE_CODE_PROPAGATE_TRACEPARENT"] == "1"


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


def current_trace_id() -> str | None:
    from opentelemetry import trace

    span_context = trace.get_current_span().get_span_context()
    return format(span_context.trace_id, "032x") if span_context.is_valid else None


def test_remote_parent_continues_the_callers_trace():
    trace_id = "7fc7a4d519014c178e3dfd2476a7c73c"
    with tracing.remote_parent({"Traceparent": f"00-{trace_id}-db0d00499cd5012b-01"}):
        assert current_trace_id() == trace_id
    assert current_trace_id() is None


@pytest.mark.parametrize("headers", [None, {}, {"traceparent": "not-a-traceparent"}])
def test_remote_parent_without_a_valid_header_starts_a_clean_root(headers):
    with tracing.remote_parent(headers):
        assert current_trace_id() is None


async def test_traced_never_records_the_mcp_context_as_input(no_langfuse, monkeypatch):
    seen = {}

    @contextlib.contextmanager
    def spy(name, as_type, **fields):
        seen.update(fields)
        yield None

    monkeypatch.setattr(tracing, "observation", spy)

    @tracing.traced("read-source", "tool")
    async def tool(path: str, ctx=None) -> str:
        return path

    assert await tool(path="slack/users.json", ctx=object()) == "slack/users.json"
    assert seen["input"] == {"path": "slack/users.json"}


def test_trace_attributes_survive_the_trip_through_mcp_headers():
    headers = tracing.attribute_headers("workshop", "run 1", ["anchor", "Q01", "as-of:2026-09-29", "a,b"])
    assert headers["x-orbisa-session-id"] == "run%201"
    sent = {k.title(): v for k, v in headers.items()}  # header names arrive in any case
    assert tracing.attributes_from_headers(sent) == {
        "user_id": "workshop", "session_id": "run 1", "tags": ["anchor", "Q01", "as-of:2026-09-29", "a,b"]}
    assert tracing.attribute_headers() == {}
    assert tracing.attributes_from_headers(None) == {}


def test_cli_resource_attributes_label_the_cli_spans(fake_langfuse, monkeypatch):
    from observability.tracing.agent import cli_resource_attributes

    monkeypatch.setenv("LANGFUSE_TRACING_ENVIRONMENT", "workshop")
    assert cli_resource_attributes("workshop", "run 1,2") == (
        "langfuse.user.id=workshop,langfuse.session.id=run%201%2C2,langfuse.environment=workshop")
    assert cli_otel_env("workshop")["OTEL_RESOURCE_ATTRIBUTES"].startswith("langfuse.user.id=workshop")


def test_agent_options_can_isolate_the_cli_config_dir(no_langfuse):
    from clients.agent.runner import build_options

    assert build_options(cli_config_dir="/tmp/claude-config-x").env["CLAUDE_CONFIG_DIR"] == "/tmp/claude-config-x"
    assert "CLAUDE_CONFIG_DIR" not in build_options().env


async def test_isolated_questions_each_get_a_fresh_config_dir_that_is_removed(no_langfuse, monkeypatch):
    import os

    from clients.agent import runner

    seen = []

    async def fake_ask(*_, cli_config_dir):
        seen.append(cli_config_dir)
        assert os.path.isdir(cli_config_dir)
        return cli_config_dir

    async def fake_shared(*_, cli_config_dir):
        return cli_config_dir

    monkeypatch.setattr(runner, "_ask", fake_ask)
    first = await runner.ask("q", "2026-09-29", isolate_cli_config=True)
    second = await runner.ask("q", "2026-09-29", isolate_cli_config=True)
    assert first != second and not os.path.exists(first) and not os.path.exists(second)
    monkeypatch.setattr(runner, "_ask", fake_shared)
    assert await runner.ask("q", "2026-09-29") is None


def test_agent_options_send_trace_attributes_to_helix(fake_langfuse):
    from clients.agent.runner import build_options

    helix = build_options(user_id="workshop", session_id="s1", tags=["Q01"]).mcp_servers["helix"]
    assert helix["headers"] == {"x-orbisa-user-id": "workshop", "x-orbisa-session-id": "s1", "x-orbisa-tags": "Q01"}
    assert "headers" not in build_options().mcp_servers["helix"]


def test_mask_replaces_emails_everywhere():
    from observability.tracing.masking import mask

    data = {"preview": '{"email": "jo.l@orbisa.example"}', "facts": [{"fact": "Mail dana.whitfield@orionfreight.example"}],
            "count": 3, "path": "slack/users.json"}
    assert mask(data=data) == {"preview": '{"email": "<email>"}', "facts": [{"fact": "Mail <email>"}],
                               "count": 3, "path": "slack/users.json"}


@pytest.mark.parametrize("text", ["clause 7.3 of the MSA", "@jo.l in #sales", "drive/Orion_MSA_2025-08-15.pdf",
                                  "USD 15,500 per month", "as-of:2026-09-29"])
def test_mask_leaves_non_emails_alone(text):
    from observability.tracing.masking import mask_value

    assert mask_value(text) == text


@pytest.mark.parametrize("pattern", ["slack/users.json", "zendesk/users.json", "hubspot/contacts.csv",
                                     "gong/calls.json", "notion/*/Team Directory *.md"])
def test_mask_leaves_no_address_in_corpus_files_with_emails(pattern):
    from context.config import DATA_DIR
    from observability.tracing.masking import mask_value

    [path] = DATA_DIR.glob(pattern)
    masked = mask_value(path.read_text(encoding="utf-8"))
    assert "<email>" in masked
    assert "@" not in masked.replace("<email>", "")


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
