import json

import httpx
import pytest

from observability.monitoring.investigate import classify
from observability.monitoring.langfuse_mcp import LangfuseMCP, ToolNotAllowed

TOOLS = [{"name": "listScores", "annotations": {"readOnlyHint": True}},
         {"name": "updatePromptLabels", "annotations": {"readOnlyHint": False}},
         {"name": "listPrompts", "annotations": {}}]  # on our allowlist, but not marked read-only by the server


def fake_server(calls: list):
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["method"] if body["method"] == "tools/list" else body["params"]["name"])
        if body["method"] == "tools/list":
            return httpx.Response(200, json={"result": {"tools": TOOLS}})
        return httpx.Response(200, json={"result": {"content": [{"type": "text", "text": '{"data": []}'}]}})
    return httpx.MockTransport(handle)


def client(calls):
    return LangfuseMCP("http://langfuse", "pk", "sk", transport=fake_server(calls))


def test_read_only_tools_on_the_allowlist_are_called():
    calls = []
    assert client(calls).call("listScores", limit=1) == {"data": []}
    assert calls == ["tools/list", "listScores"]


@pytest.mark.parametrize("tool", ["updatePromptLabels", "deleteDataset", "listPrompts"])
def test_write_tools_and_unmarked_tools_are_refused_before_any_call(tool):
    calls = []
    with pytest.raises(ToolNotAllowed):
        client(calls).call(tool)
    assert tool not in calls


def item(answer, as_of, **scores):
    return {"answer": answer, "input": {"as_of": as_of}, "metadata": {"category": "temporal"},
            "scores": {name: {"value": value} for name, value in scores.items()}}


def test_a_later_date_from_a_fact_validity_is_blamed_on_helix():
    fact = {"fact": "Finance approves refunds", "status": "true on as_of", "valid_from": "2023-02-01",
            "valid_until": "2026-03-03"}
    [finding] = classify(item("Finance until 2026-03-03.", "2025-12-01"), [fact], [], {})
    assert finding["class"] == "temporal" and "validity" in finding["cause"]


def test_a_later_date_seen_in_list_sources_is_blamed_on_list_sources():
    [finding] = classify(item("A newer page is dated 2026-02-20.", "2026-01-15"), [], [], {}, listed_sources=True)
    assert "list_sources" in finding["cause"]


def test_a_later_date_nothing_returned_is_a_likely_false_positive():
    [finding] = classify(item("Request it by 30 September 2026.", "2026-09-15"), [], [], {})
    assert "false positive" in finding["cause"]


def test_missing_citations_split_into_retrieval_and_reasoning():
    comments = {"source_recall": "3/4 expected sources; missing: x"}
    [cited] = classify(item("No dates.", "2026-10-15", source_recall=0.75, retrieved_source_recall=1.0), [], [], comments)
    [missing] = classify(item("No dates.", "2026-10-15", source_recall=0.75, retrieved_source_recall=0.5), [], [], comments)
    assert (cited["class"], cited["cause"]) == ("reasoning", "retrieved but not cited")
    assert (missing["class"], missing["cause"]) == ("retrieval", "expected source not retrieved")
    assert cited["evidence"] == "3/4 expected sources; missing: x"
