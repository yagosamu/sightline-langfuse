import json

from clients.agent.runner import AgentRun, compact_result


def mcp_text(payload: dict) -> list[dict]:
    return [{"type": "text", "text": json.dumps(payload)}]


SEARCH = {"query": "who approves refunds", "as_of": "2026-09-29", "hidden_as_future": 2, "facts": [
    {"fact": "Tomás approves refunds", "status": "true on as_of", "valid_from": "2026-03-03", "valid_until": None,
     "sources": [{"path": "slack/revops/2026-03-03.json", "description": "Slack", "dated": "2026-03-03"}]}]}


def test_search_results_keep_facts_status_and_source_paths():
    assert compact_result("search_context", mcp_text(SEARCH)) == {
        "facts": [{"fact": "Tomás approves refunds", "status": "true on as_of",
                   "sources": ["slack/revops/2026-03-03.json"]}],
        "hidden_as_future": 2}


def test_read_source_keeps_the_path_not_the_document():
    result = compact_result("read_source", mcp_text({"path": "drive/msa.pdf", "truncated": False, "content": "x" * 9000}))
    assert result == {"path": "drive/msa.pdf", "error": None}


def test_unreadable_results_are_marked_not_raised():
    assert compact_result("search_context", "not json") == {"error": "unreadable tool result"}


def test_run_lists_retrieved_and_read_sources():
    run = AgentRun(question="q", as_of="2026-09-29", answer="a", tool_calls=[
        {"id": "1", "tool": "search_context", "input": {}, "result": compact_result("search_context", mcp_text(SEARCH))},
        {"id": "2", "tool": "read_source", "input": {}, "result": {"path": "drive/msa.pdf", "error": None}},
        {"id": "3", "tool": "read_source", "input": {}, "result": {"path": "nope", "error": "not a source document"}},
        {"id": "4", "tool": "read_source", "input": {}},  # no result came back
    ])
    assert run.retrieved_sources() == {"slack/revops/2026-03-03.json"}
    assert run.read_sources() == {"drive/msa.pdf"}


def test_helix_facts_are_deduplicated_in_first_seen_order():
    search = {"id": "1", "tool": "search_context", "input": {}, "result": compact_result("search_context", mcp_text(SEARCH))}
    run = AgentRun(question="q", as_of="2026-09-29", answer="a", tool_calls=[search, {**search, "id": "2"}])
    assert run.helix_facts() == [{"fact": "Tomás approves refunds", "status": "true on as_of",
                                  "sources": ["slack/revops/2026-03-03.json"]}]
