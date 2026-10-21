from types import SimpleNamespace

import pytest

from observability.evaluators.judge import Judgement
from observability.experiments import p95, summarize, targets_met
from observability.experiments import run as experiment


def row(id, category, correctness, cost=0.1, duration_ms=20_000, source_recall=1.0):
    return {"id": id, "category": category, "correctness": correctness, "cost_usd": cost,
            "duration_ms": duration_ms, "source_recall": source_recall}


ROWS = [row("Q02", "temporal", 1.0), row("Q03", "temporal", 0.5), row("Q04", "conflict", 1.0),
        row("Q13", "false_premise", 0.0, source_recall=None), row("Q16", "lookup", 1.0, duration_ms=60_000)]


def test_summary_reports_per_category_and_target_scores():
    s = summarize(ROWS)
    assert s["correctness"] == pytest.approx(0.7)
    assert s["correctness_by_category"] == {"conflict": 1.0, "false_premise": 0.0, "lookup": 1.0, "temporal": 0.75}
    assert s["correctness_targets"] == pytest.approx(0.625)  # Q02, Q03, Q04, Q13
    assert s["correctness_target_floor"] == 0.0
    assert s["source_recall"] == 1.0  # Q13 has no citable source and is left out
    assert s["cost_usd_per_correct_answer"] == pytest.approx(0.5 / 3.5, abs=1e-4)
    assert s["latency_s_p95"] == 60.0


def test_targets_follow_decision_01():
    met = targets_met(summarize(ROWS))
    assert met == {"C2_mean_target_questions>=0.8": False, "C2_no_target_category<0.67": False,
                   "C3_source_recall>=0.8": True}


def test_p95_uses_nearest_rank():
    assert p95(list(range(1, 21))) == 19
    assert p95([]) is None


def test_item_evaluator_scores_a_false_premise_answer(monkeypatch):
    monkeypatch.setattr(experiment, "judge", lambda **_: Judgement("correct", 1.0, False, "rejects the premise"))
    scores = {e.name: e.value for e in experiment.evaluate_item(
        input={"question": "Platinum refund window?", "as_of": "2026-10-15"},
        output={"answer": "Orbisa has no Platinum plan.\n\nSources: confluence/Plans-and-Pricing_98301876.html",
                "retrieved_sources": ["confluence/Plans-and-Pricing_98301876.html"]},
        expected_output={"answer": "No Platinum plan.", "facts": ["No Platinum plan"],
                         "sources": ["confluence/Plans-and-Pricing_98301876.html"]},
        metadata={"category": "false_premise", "trap": "Inventing a window."})}
    assert scores == {"has_sources_line": True, "future_date_leak": False, "correctness": 1.0, "trap_hit": False,
                      "source_recall": 1.0, "retrieved_source_recall": 1.0, "abstained": True}


def test_item_evaluator_skips_correctness_when_the_judge_declines(monkeypatch):
    monkeypatch.setattr(experiment, "judge", lambda **_: Judgement(None, None, False, "no verdict"))
    names = [e.name for e in experiment.evaluate_item(
        input={"question": "q", "as_of": "2026-10-15"}, output={"answer": "a", "retrieved_sources": []},
        expected_output={"answer": "a", "facts": [], "sources": []}, metadata={"category": "lookup", "trap": "t"})]
    assert "correctness" not in names and "abstained" not in names


def test_failed_items_are_the_ones_without_a_result():
    items = [SimpleNamespace(id=i) for i in ("Q02", "Q14", "Q15", "Q21")]
    assert experiment.failed_items(items, [{"id": "Q02"}]) == ["Q14", "Q15", "Q21"]


def test_item_row_survives_a_failed_task():
    result = SimpleNamespace(item=SimpleNamespace(metadata={"id": "Q01", "category": "anchor"}), output=None,
                             evaluations=[], trace_id="t1")
    assert experiment.item_row(result)["correctness"] is None
