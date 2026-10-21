"""The Ask Orbisa dashboard in Langfuse, and a terminal health report from the Metrics API.

    uv run python -m observability.monitoring.dashboard create        # safe to re-run
    uv run python -m observability.monitoring.dashboard report        # last 24 hours
    uv run python -m observability.monitoring.dashboard report --hours 168

Widgets and report read the same queries (monitoring/queries.py). The dashboards API is marked unstable
in the Langfuse SDK, so this file is the only place that depends on it.
"""

import argparse
import json
import os
import sys

import context.config  # noqa: F401  (loads .env)
from langfuse.api.unstable.dashboard_widgets.types.dashboard_widget_dimension import DashboardWidgetDimension
from langfuse.api.unstable.dashboard_widgets.types.dashboard_widget_filter import DashboardWidgetFilter
from langfuse.api.unstable.dashboard_widgets.types.dashboard_widget_metric import DashboardWidgetMetric
from langfuse.api.unstable.dashboards.types.create_dashboard_placement_request import (
    CreateDashboardPlacementRequest_Widget,
)

from observability.monitoring import AGENT_OBSERVATION
from observability.monitoring.queries import MODEL_USAGE, QUERIES, SCORES, STEPS, TOOLS, names, with_range
from observability.tracing import get_langfuse, missing_env

DASHBOARD = "Ask Orbisa"
CHARTS = {
    "Questions per day": "LINE_TIME_SERIES",
    "p95 latency by step (ms)": "HORIZONTAL_BAR",
    "Mean cost per question by model (USD)": "VERTICAL_BAR",
    "Tool calls by tool": "HORIZONTAL_BAR",
    "Mean scores over time": "LINE_TIME_SERIES",
}
WIDTH, HEIGHT = 6, 5  # two widgets per row on the 12-column grid


def client():
    langfuse = get_langfuse()
    if langfuse is None:
        sys.exit(f"Langfuse is not configured (missing {', '.join(missing_env())})")
    return langfuse


def widget_fields(title: str) -> dict:
    query = QUERIES[title]
    return {
        "name": title,
        "description": "Ask Orbisa health, from observability/monitoring/queries.py",
        "view": query["view"],
        "dimensions": [DashboardWidgetDimension(field=d["field"]) for d in query["dimensions"]],
        "metrics": [DashboardWidgetMetric(measure=m["measure"], agg=m["aggregation"]) for m in query["metrics"]],
        "filters": [DashboardWidgetFilter(**f) for f in query["filters"]],
        "chart_type": CHARTS[title],
    }


def create(langfuse) -> None:
    api = langfuse.api.unstable
    widgets = {w.name: w.id for w in api.dashboard_widgets.list(limit=100).data}
    for title in CHARTS:
        if title not in widgets:
            widgets[title] = api.dashboard_widgets.create(**widget_fields(title)).id
            print(f"widget created: {title}")
    dashboards = {d.name: d.id for d in api.dashboards.list(limit=100).data}
    dashboard_id = dashboards.get(DASHBOARD) or api.dashboards.create(
        name=DASHBOARD, description="Is Ask Orbisa healthy? Volume, latency, cost, tool use and quality scores").id
    placed = placed_widget_ids(api.dashboards.get(dashboard_id))
    for i, title in enumerate(CHARTS):
        if widgets[title] not in placed:
            api.dashboards.add_placement(dashboard_id, request=CreateDashboardPlacementRequest_Widget(
                widget_id=widgets[title], x=(i % 2) * WIDTH, y=(i // 2) * HEIGHT, width=WIDTH, height=HEIGHT))
            print(f"placed: {title}")
    base = os.environ["LANGFUSE_BASE_URL"].rstrip("/")
    project = langfuse.api.projects.get().data[0].id
    print(f"dashboard {DASHBOARD!r}: {base}/project/{project}/dashboards/{dashboard_id}")


def placed_widget_ids(dashboard) -> set[str]:
    """Widget ids already on the dashboard, wherever this SDK version keeps the placements."""
    definition = getattr(dashboard, "definition", None)
    placements = getattr(definition, "widgets", None) or getattr(dashboard, "placements", None) or []
    return {getattr(p, "widget_id", None) or getattr(p, "id", None) for p in placements}


def run(langfuse, query: dict, hours: int) -> list[dict]:
    return langfuse.api.metrics.metrics(query=json.dumps(with_range(query, hours))).data


def total(langfuse, filters: list[dict], measure: str, aggregation: str, hours: int) -> float:
    rows = run(langfuse, {"view": "observations", "dimensions": [], "filters": filters,
                          "metrics": [{"measure": measure, "aggregation": aggregation}]}, hours)
    return float(next(iter(rows[0].values()), 0) or 0) if rows else 0.0


def report(langfuse, hours: int) -> dict:
    questions = int(total(langfuse, [names([AGENT_OBSERVATION])], "count", "count", hours))
    cost = total(langfuse, [names([MODEL_USAGE])], "totalCost", "sum", hours)
    tool_calls = int(total(langfuse, [names(TOOLS)], "count", "count", hours))
    latency = {r["name"]: r["p95_latency"] / 1000 for r in run(langfuse, QUERIES["p95 latency by step (ms)"], hours)}
    by_model = {r["providedModelName"]: r["avg_totalCost"]
                for r in run(langfuse, QUERIES["Mean cost per question by model (USD)"], hours)}
    score_query = {**QUERIES["Mean scores over time"], "timeDimension": None}
    scores = {r["name"]: r["avg_value"] for r in run(langfuse, {k: v for k, v in score_query.items() if v}, hours)
              if r["name"] in SCORES}
    return {"hours": hours, "questions": questions,
            "cost_usd_per_question": round(cost / questions, 4) if questions else None,
            "cost_usd_per_question_by_model": {m: round(v, 4) for m, v in by_model.items()},
            "tool_calls_per_question": round(tool_calls / questions, 1) if questions else None,
            "p95_latency_s": {step: round(latency[step], 1) for step in STEPS if step in latency},
            "mean_scores": {name: round(value, 3) for name, value in scores.items()}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create")
    r = sub.add_parser("report")
    r.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    langfuse = client()
    if args.command == "create":
        create(langfuse)
    else:
        print(json.dumps(report(langfuse, args.hours), indent=2))


if __name__ == "__main__":
    main()
