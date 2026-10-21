import json
from datetime import datetime, timezone
from types import SimpleNamespace

from observability.monitoring import dashboard
from observability.monitoring.queries import QUERIES, with_range


def test_every_query_has_a_widget_and_the_widget_metric_uses_agg():
    assert set(dashboard.CHARTS) == set(QUERIES)
    fields = dashboard.widget_fields("p95 latency by step (ms)")
    assert fields["metrics"][0].agg == "p95" and fields["metrics"][0].measure == "latency"
    assert fields["chart_type"] == "HORIZONTAL_BAR"


def test_with_range_adds_an_iso_time_window():
    query = with_range(QUERIES["Questions per day"], hours=24, now=datetime(2026, 10, 21, 12, tzinfo=timezone.utc))
    assert (query["fromTimestamp"], query["toTimestamp"]) == ("2026-10-20T12:00:00Z", "2026-10-21T12:00:00Z")


class FakeMetrics:
    """Answers each Metrics API query by what it filters on."""

    def metrics(self, *, query):
        q = json.loads(query)
        wanted = q["filters"][0]["value"]
        if q["view"] == "scores-numeric":
            rows = [{"name": "faithfulness", "avg_value": 0.84}, {"name": "unrelated", "avg_value": 0.1}]
        elif q["dimensions"] == [{"field": "name"}] and "answer-question" in wanted:
            rows = [{"name": "answer-question", "p95_latency": 30_000.0}, {"name": "search-context", "p95_latency": 1_200.0}]
        elif q["dimensions"] == [{"field": "providedModelName"}]:
            rows = [{"providedModelName": "claude-opus-5-5", "avg_totalCost": 0.09}]
        elif wanted == ["answer-question"]:
            rows = [{"count_count": 10}]
        elif wanted == ["report-model-usage"]:
            rows = [{"sum_totalCost": 0.9}]
        else:
            rows = [{"count_count": 65}]
        return SimpleNamespace(data=rows)


def test_report_divides_totals_by_the_number_of_questions():
    langfuse = SimpleNamespace(api=SimpleNamespace(metrics=FakeMetrics()))
    result = dashboard.report(langfuse, hours=24)
    assert result["questions"] == 10
    assert result["cost_usd_per_question"] == 0.09
    assert result["tool_calls_per_question"] == 6.5
    assert result["p95_latency_s"] == {"answer-question": 30.0, "search-context": 1.2}
    assert result["mean_scores"] == {"faithfulness": 0.84}
