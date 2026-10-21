"""The Ask Orbisa health queries, shared by the dashboard widgets and the `report` command.

Each query is a Langfuse Metrics API v2 query without its time range; `with_range` adds it. Our cost is on
the report-model-usage generations (one per model per question), so mean cost per question by model is
the average of their totalCost.
"""

from datetime import datetime, timedelta, timezone

from observability.monitoring import AGENT_OBSERVATION, FAITHFULNESS

MODEL_USAGE = "report-model-usage"
STEPS = [AGENT_OBSERVATION, "claude_code.llm_request", "search-context"]
TOOLS = ["search_context", "find_entities", "read_source", "list_sources"]  # the CLI's MCP tool spans
SCORES = [FAITHFULNESS, "correctness", "source_recall"]


def names(values: list[str]) -> dict:
    return {"column": "name", "operator": "any of", "value": values, "type": "stringOptions"}


QUERIES: dict[str, dict] = {
    "Questions per day": {
        "view": "observations", "dimensions": [], "filters": [names([AGENT_OBSERVATION])],
        "metrics": [{"measure": "count", "aggregation": "count"}], "timeDimension": {"granularity": "day"},
    },
    "p95 latency by step (ms)": {
        "view": "observations", "dimensions": [{"field": "name"}], "filters": [names(STEPS)],
        "metrics": [{"measure": "latency", "aggregation": "p95"}],
    },
    "Mean cost per question by model (USD)": {
        "view": "observations", "dimensions": [{"field": "providedModelName"}], "filters": [names([MODEL_USAGE])],
        "metrics": [{"measure": "totalCost", "aggregation": "avg"}],
    },
    "Tool calls by tool": {
        "view": "observations", "dimensions": [{"field": "name"}], "filters": [names(TOOLS)],
        "metrics": [{"measure": "count", "aggregation": "count"}],
    },
    "Mean scores over time": {
        "view": "scores-numeric", "dimensions": [{"field": "name"}], "filters": [names(SCORES)],
        "metrics": [{"measure": "value", "aggregation": "avg"}], "timeDimension": {"granularity": "day"},
    },
}


def with_range(query: dict, hours: int, now: datetime | None = None) -> dict:
    end = now or datetime.now(timezone.utc)
    return {**query, "fromTimestamp": (end - timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
            "toTimestamp": end.isoformat().replace("+00:00", "Z")}
