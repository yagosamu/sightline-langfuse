from types import SimpleNamespace

import pytest

from observability.evaluators import judge as judge_module
from observability.evaluators.judge import Verdict, grading_request, judge


class FakeMessages:
    def __init__(self, verdict: Verdict | None, stop_reason: str = "end_turn"):
        self.verdict, self.stop_reason, self.calls = verdict, stop_reason, []

    def parse(self, **request):
        self.calls.append(request)
        return SimpleNamespace(parsed_output=self.verdict, stop_reason=self.stop_reason,
                               usage=SimpleNamespace(input_tokens=900, output_tokens=120))


def use_fake(monkeypatch, verdict, stop_reason="end_turn") -> FakeMessages:
    messages = FakeMessages(verdict, stop_reason)
    monkeypatch.setattr(judge_module, "client", lambda: SimpleNamespace(messages=messages))
    return messages


ITEM = dict(question="Who approves refunds?", as_of="2025-12-01", category="temporal", answer="Finance (Priya).",
            expected_answer="Finance, Priya Raman.", expected_facts=["Finance approves refunds"],
            trap="Answering RevOps, which only took over on 2026-03-03.")


@pytest.mark.parametrize("verdict, score", [("correct", 1.0), ("partially_correct", 0.5), ("incorrect", 0.0)])
def test_verdicts_map_to_scores(monkeypatch, verdict, score):
    use_fake(monkeypatch, Verdict(reason="r", trap_hit=False, verdict=verdict))
    assert judge(**ITEM).score == score


def test_falling_into_the_trap_scores_zero(monkeypatch):
    use_fake(monkeypatch, Verdict(reason="names RevOps", trap_hit=True, verdict="partially_correct"))
    result = judge(**ITEM)
    assert (result.score, result.trap_hit) == (0.0, True)


def test_a_refusal_gives_no_score(monkeypatch):
    use_fake(monkeypatch, None, stop_reason="refusal")
    result = judge(**ITEM)
    assert (result.verdict, result.score) == (None, None)


def test_judge_uses_structured_output_with_the_as_of_date_and_trap(monkeypatch):
    messages = use_fake(monkeypatch, Verdict(reason="r", trap_hit=False, verdict="correct"))
    judge(**ITEM)
    request = messages.calls[0]
    assert request["output_format"] is Verdict
    assert request["model"] == "claude-haiku-5-5"
    prompt = request["messages"][0]["content"]
    assert "as of 2025-12-01" in prompt and "Trap:" in prompt and "<answer>\nFinance (Priya).\n</answer>" in prompt


def test_grading_request_lists_every_expected_fact():
    text = grading_request(**{**ITEM, "expected_facts": ["a fact", "another fact"]})
    assert "- a fact\n- another fact" in text
