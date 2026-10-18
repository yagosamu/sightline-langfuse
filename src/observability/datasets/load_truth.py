"""Create the orbisa-truth dataset and upsert one item per ground-truth question.

    uv run python -m observability.datasets.load_truth

The question id (Q01...) is the item id, so re-running updates the items instead of duplicating them.
"""

import sys

import context.config  # noqa: F401  (loads .env)
from observability.datasets import DATASET_NAME, TRUTH_FILE, dataset_item, truth_items
from observability.tracing import get_langfuse, missing_env


def main() -> None:
    langfuse = get_langfuse()
    if langfuse is None:
        sys.exit(f"Langfuse is not configured (missing {', '.join(missing_env())})")
    try:
        langfuse.get_dataset(DATASET_NAME)
    except Exception:
        langfuse.create_dataset(name=DATASET_NAME, description=f"Ask Orbisa ground truth, from {TRUTH_FILE.name}")
    items = truth_items()
    for question in items:
        langfuse.create_dataset_item(dataset_name=DATASET_NAME, **dataset_item(question))
    langfuse.flush()
    stored = langfuse.get_dataset(DATASET_NAME).items
    print(f"{DATASET_NAME}: upserted {len(items)} items; the dataset now holds {len(stored)}")


if __name__ == "__main__":
    main()
