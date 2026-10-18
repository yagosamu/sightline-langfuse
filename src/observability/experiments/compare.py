"""Compare two experiment runs item by item, on the items both runs share.

    uv run python -m observability.experiments.compare production-full production-targets-2   # noise
    uv run python -m observability.experiments.compare production-targets-2 staging-targets  # change

Reads .helix/experiments/<name>.json. Compare a change against the spread between two runs of the
same configuration before calling it better or worse.
"""

import argparse
import json
from statistics import mean

from context.config import STATE_DIR

SCORES = ("correctness", "trap_hit", "source_recall", "retrieved_source_recall", "has_sources_line", "abstained")
NUMBERS = ("cost_usd", "duration_ms", "num_turns", "tool_calls")


def load(name: str) -> dict:
    return json.loads((STATE_DIR / "experiments" / f"{name}.json").read_text(encoding="utf-8"))


def compare(a: dict, b: dict) -> dict:
    rows_a, rows_b = ({row["id"]: row for row in run["items"]} for run in (a, b))
    shared = sorted(rows_a.keys() & rows_b.keys())
    changed = [{"id": i, "score": s, "a": rows_a[i].get(s), "b": rows_b[i].get(s)}
               for i in shared for s in SCORES if rows_a[i].get(s) != rows_b[i].get(s)]

    def avg(rows, key, ids):
        values = [rows[i][key] for i in ids if rows[i].get(key) is not None]
        return mean(values) if values else None

    means = {key: (avg(rows_a, key, shared), avg(rows_b, key, shared)) for key in SCORES + NUMBERS}
    per_item_cost = [abs((rows_b[i]["cost_usd"] or 0) - (rows_a[i]["cost_usd"] or 0)) for i in shared]
    return {"a": a["run"], "b": b["run"], "shared_items": len(shared), "changed_scores": changed,
            "means": means, "mean_abs_cost_change_per_item": round(mean(per_item_cost), 4) if per_item_cost else None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("a")
    parser.add_argument("b")
    args = parser.parse_args()
    result = compare(load(args.a), load(args.b))
    print(f"{result['a']} vs {result['b']}: {result['shared_items']} shared items")
    for key, (va, vb) in result["means"].items():
        if va is None and vb is None:
            continue
        delta = f"{vb - va:+.4f}" if va is not None and vb is not None else "n/a"
        print(f"  {key:<24} {va if va is None else round(va, 4)!s:>10} -> {vb if vb is None else round(vb, 4)!s:>10}  ({delta})")
    print(f"  mean |cost change| per item: {result['mean_abs_cost_change_per_item']}")
    print(f"  scores that changed: {len(result['changed_scores'])}")
    for change in result["changed_scores"]:
        print(f"    {change['id']} {change['score']}: {change['a']} -> {change['b']}")


if __name__ == "__main__":
    main()
