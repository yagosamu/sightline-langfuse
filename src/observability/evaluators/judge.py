"""LLM-as-a-judge for Ask Orbisa answers: correctness against the ground truth (C2).

The judge sees the question, the as-of date, the expected answer and facts, and the item's trap. It
returns correct / partially_correct / incorrect (1 / 0.5 / 0) with a reason. An answer that falls
into the trap scores 0 whatever the verdict. Each call is traced as an evaluator observation with
the model call inside it.
"""

import os
from dataclasses import dataclass
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from observability.tracing import observation, record

JUDGE_MODEL = os.environ.get("HELIX_JUDGE_MODEL", "claude-haiku-5-5")
SCORES = {"correct": 1.0, "partially_correct": 0.5, "incorrect": 0.0}

SYSTEM = """You grade answers from Ask Orbisa, an internal assistant for a logistics software company, against a
reference answer written by people who know the company's records.

Grade substance only. Wording, length, formatting and extra correct detail do not matter.

- correct: the answer reaches the reference's conclusion and states the expected facts that the conclusion
  depends on, without contradicting any of them.
- partially_correct: the conclusion is right, but the answer misses or misstates a fact the conclusion depends
  on, or it answers only part of the question.
- incorrect: the conclusion is wrong, the answer contradicts an expected fact, or it makes the mistake
  described as the trap.

Rules by question type:
- Facts change over time. The answer must use what was true on the as-of date. Using a rule or a person that
  was replaced before that date is incorrect, even if it was true earlier.
- When sources disagree, the expected answer names the one that wins (for example a signed contract over a
  general policy). Picking the losing source is incorrect.
- When the question rests on something false or unknown, a correct answer says so. Inventing a value, a
  date or an approval is incorrect.

Set trap_hit to true only when the answer makes the specific mistake described in the trap."""


class Verdict(BaseModel):
    reason: str = Field(description="Two to four sentences: which expected facts the answer states, misses or contradicts")
    trap_hit: bool = Field(description="True only if the answer makes the mistake described in the trap")
    verdict: Literal["correct", "partially_correct", "incorrect"]


@dataclass(frozen=True)
class Judgement:
    verdict: str | None  # None when the judge declined or failed
    score: float | None
    trap_hit: bool
    reason: str


_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def grading_request(*, question: str, as_of: str, category: str, answer: str, expected_answer: str,
                    expected_facts: list[str], trap: str) -> str:
    facts = "\n".join(f"- {fact}" for fact in expected_facts)
    return (f"Question (asked as of {as_of}, type {category}):\n{question}\n\n"
            f"Reference answer:\n{expected_answer}\n\nExpected facts:\n{facts}\n\nTrap:\n{trap}\n\n"
            f"Answer to grade:\n<answer>\n{answer}\n</answer>")


def judge(*, question: str, as_of: str, category: str, answer: str, expected_answer: str,
          expected_facts: list[str], trap: str, model: str = JUDGE_MODEL) -> Judgement:
    request = grading_request(question=question, as_of=as_of, category=category, answer=answer,
                              expected_answer=expected_answer, expected_facts=expected_facts, trap=trap)
    with observation("judge-answer", "evaluator", input={"category": category, "as_of": as_of, "answer": answer},
                     metadata={"judge_model": model}) as obs:
        judgement = _grade(request, model)
        record(obs, output={"verdict": judgement.verdict, "score": judgement.score, "trap_hit": judgement.trap_hit,
                            "reason": judgement.reason})
    return judgement


def _grade(request: str, model: str) -> Judgement:
    with observation("call-judge-model", "generation", model=model, input=request) as gen:
        try:
            response = client().messages.parse(
                model=model,
                max_tokens=8000,
                system=SYSTEM,
                messages=[{"role": "user", "content": request}],
                output_format=Verdict,
            )
        except anthropic.APIError as exc:
            record(gen, level="ERROR", status_message=str(exc))
            return Judgement(verdict=None, score=None, trap_hit=False, reason=f"judge call failed: {exc}")
        record(gen, output=response.parsed_output.model_dump() if response.parsed_output else None,
               usage_details={"input": response.usage.input_tokens, "output": response.usage.output_tokens})
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return Judgement(verdict=None, score=None, trap_hit=False, reason=f"no verdict (stop_reason {response.stop_reason})")
    verdict = response.parsed_output
    score = 0.0 if verdict.trap_hit else SCORES[verdict.verdict]
    return Judgement(verdict=verdict.verdict, score=score, trap_hit=verdict.trap_hit, reason=verdict.reason)
