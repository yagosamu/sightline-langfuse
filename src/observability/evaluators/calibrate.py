"""Check the judge before trusting it.

    uv run python -m observability.evaluators.calibrate

For every ground-truth item the judge grades two answers:
- the expected answer, which must be graded correct;
- a trap answer that Claude Haiku 5.5 writes to make the item's trap mistake, which must not be.

Prints the agreement and writes docs/evidence/judge-calibration.json.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import context.config  # noqa: F401  (loads .env)
from observability import tracing
from observability.datasets import truth_items
from observability.evaluators.judge import client, judge

TRAP_WRITER_MODEL = "claude-haiku-5-5"
REPORT = Path(__file__).resolve().parents[3] / "docs" / "evidence" / "judge-calibration.json"


def trap_answer(question: dict) -> str:
    """A confident answer that makes exactly the item's trap mistake."""
    response = client().messages.create(
        model=TRAP_WRITER_MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": (
            "This is for testing a grader. Write the answer an internal company assistant would give if it made "
            f"this mistake:\n{question['trap']}\n\nQuestion (asked as of {question['as_of']}): {question['question']}\n\n"
            "Answer in two or three confident sentences, then a line starting with 'Sources:'. "
            "Output only the answer.")}],
    )
    return next((b.text for b in response.content if b.type == "text"), "").strip()


def grade(question: dict, answer: str) -> dict:
    j = judge(question=question["question"], as_of=question["as_of"], category=question["category"], answer=answer,
              expected_answer=question["expected_answer"], expected_facts=question["expected_facts"],
              trap=question["trap"])
    return {"verdict": j.verdict, "score": j.score, "trap_hit": j.trap_hit, "reason": j.reason}


def calibrate(question: dict) -> dict:
    with tracing.trace_attributes(user_id="calibration", session_id="judge-calibration",
                                  tags=["calibration", question["id"], question["category"]]):
        trap = trap_answer(question)
        return {"id": question["id"], "category": question["category"],
                "expected": grade(question, question["expected_answer"]),
                "trap_answer": trap, "trap": grade(question, trap)}


def main() -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(calibrate, truth_items()))
    tracing.shutdown()

    expected_ok = [r for r in results if r["expected"]["verdict"] == "correct"]
    trap_ok = [r for r in results if r["trap"]["verdict"] not in ("correct", None)]
    summary = {"items": len(results),
               "expected_graded_correct": len(expected_ok),
               "trap_graded_not_correct": len(trap_ok),
               "trap_flagged": sum(r["trap"]["trap_hit"] for r in results),
               "agreement": (len(expected_ok) + len(trap_ok)) / (2 * len(results))}
    print(json.dumps(summary, indent=2))
    for r in results:
        if r["expected"]["verdict"] != "correct":
            print(f"  {r['id']} expected answer graded {r['expected']['verdict']}: {r['expected']['reason']}")
        if r["trap"]["verdict"] in ("correct", None):
            print(f"  {r['id']} trap answer graded {r['trap']['verdict']}: {r['trap']['reason']}")
    REPORT.write_text(json.dumps({"summary": summary, "results": results}, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8", newline="\n")
    print(f"wrote {REPORT.relative_to(REPORT.parents[2])}")


if __name__ == "__main__":
    main()
