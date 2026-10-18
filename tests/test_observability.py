import pytest

from observability import tracing
from observability.tracing import helix


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
