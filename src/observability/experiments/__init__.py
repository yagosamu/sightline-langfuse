"""Experiment scoring: one row per item, aggregated per run against decision 01's targets."""

import math
from collections import defaultdict
from statistics import mean

from observability.datasets import TARGET_CATEGORIES

CORRECTNESS_TARGET = 0.8  # C2: mean on the target questions
CATEGORY_FLOOR = 0.67  # C2: no target category below this
SOURCE_RECALL_TARGET = 0.8  # C3


def _mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(mean(present), 3) if present else None


def p95(values: list[float]) -> float | None:
    """95th percentile, nearest rank."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def summarize(rows: list[dict]) -> dict:
    """Run-level scores from item rows (id, category, correctness, source_recall, cost_usd, duration_ms...)."""
    by_category: dict[str, list[float | None]] = defaultdict(list)
    for row in rows:
        by_category[row["category"]].append(row.get("correctness"))
    per_category = {category: _mean(values) for category, values in sorted(by_category.items())}
    target_rows = [row for row in rows if row["category"] in TARGET_CATEGORIES]
    target_categories = {c: per_category[c] for c in TARGET_CATEGORIES if c in per_category}
    total_cost = sum(row.get("cost_usd") or 0.0 for row in rows)
    correct = sum(row.get("correctness") or 0.0 for row in rows)
    latencies = [row["duration_ms"] / 1000 for row in rows if row.get("duration_ms")]
    return {
        "items": len(rows),
        "correctness": _mean([row.get("correctness") for row in rows]),
        "correctness_by_category": per_category,
        "correctness_targets": _mean([row.get("correctness") for row in target_rows]),
        "correctness_target_floor": min((v for v in target_categories.values() if v is not None), default=None),
        "source_recall": _mean([row.get("source_recall") for row in rows]),
        "future_date_leak_rate": _mean([float(row["future_date_leak"]) for row in rows
                                        if row.get("future_date_leak") is not None]),
        "retrieved_source_recall": _mean([row.get("retrieved_source_recall") for row in rows]),
        "cost_usd_mean": round(total_cost / len(rows), 4) if rows else None,
        "cost_usd_per_correct_answer": round(total_cost / correct, 4) if correct else None,
        "latency_s_p95": round(p95(latencies), 1) if latencies else None,
        "errors": sum(1 for row in rows if row.get("is_error")),
    }


def targets_met(summary: dict) -> dict[str, bool | None]:
    """Decision 01's C2 and C3 targets for one run; None when the score is missing."""
    def at_least(value, target):
        return None if value is None else value >= target

    return {
        "C2_mean_target_questions>=0.8": at_least(summary["correctness_targets"], CORRECTNESS_TARGET),
        "C2_no_target_category<0.67": at_least(summary["correctness_target_floor"], CATEGORY_FLOOR),
        "C3_source_recall>=0.8": at_least(summary["source_recall"], SOURCE_RECALL_TARGET),
    }
