"""The ground truth (data/orbisa/truth/questions.jsonl) as a Langfuse dataset."""

import json

from context.config import DATA_DIR

DATASET_NAME = "orbisa-truth"
TRUTH_FILE = DATA_DIR / "truth" / "questions.jsonl"
TARGET_CATEGORIES = ("temporal", "conflict", "false_premise")  # the categories with targets (decision 01)


def truth_items() -> list[dict]:
    return [json.loads(line) for line in TRUTH_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]


def dataset_item(question: dict) -> dict:
    """One ground-truth line as Langfuse dataset item fields. The ground truth itself is never edited."""
    return {
        "id": question["id"],
        "input": {"question": question["question"], "as_of": question["as_of"]},
        "expected_output": {"answer": question["expected_answer"], "facts": question["expected_facts"],
                            "sources": question["expected_sources"]},
        "metadata": {"id": question["id"], "category": question["category"], "trap": question["trap"]},
    }
