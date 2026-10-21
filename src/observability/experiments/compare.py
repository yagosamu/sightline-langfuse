"""Compare experiment runs, and apply the team's release rule to a candidate.

    uv run python -m observability.experiments.compare diff production-full production-targets-2   # noise
    uv run python -m observability.experiments.compare diff production-targets-2 staging-targets  # change
    uv run python -m observability.experiments.compare release \\
        --baseline production-full production-targets-2 \\
        --candidate sources-candidate-full sources-candidate-targets+sources-candidate-targets-rest
    uv run python -m observability.experiments.compare release --goal leaks         --baseline production-full production-targets-2 --candidate helix-as-of-full helix-as-of-targets

Reads .helix/experiments/<name>.json; '+' joins runs of one configuration (a rerun of failed items).
Compare a change against the spread between two runs of the same configuration before calling it
better or worse. The release rule passes a candidate only if all five checks pass, and even then it
ships only after a human review (observability.prompts.promote and the git diff of system_prompt.md).

The goal names what the candidate promises. A prompt change aims at source recall: recall must gain beyond
the noise and leaks must not get worse. A Helix fix for as_of aims at leaks: leaks must drop and recall
must not lose more than the noise. The goal is chosen before the candidate runs.
"""

import argparse
import json
from pathlib import Path
from statistics import mean

from context.config import STATE_DIR

SCORES = ("correctness", "trap_hit", "source_recall", "retrieved_source_recall", "has_sources_line", "abstained",
          "future_date_leak")
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


def load_merged(spec: str) -> dict:
    """A run, or several runs of one configuration joined with '+' (for example a rerun of failed items)."""
    runs = [load(name) for name in spec.split("+")]
    items = {row["id"]: row for run in runs for row in run["items"]}
    return {"run": spec, "items": sorted(items.values(), key=lambda row: row["id"])}


def leak_count(run: dict) -> int:
    """Answers that cite a date unknown on their as-of date. Runs made before the future_date_leak
    evaluator existed carry no score; their count comes from the investigation report, if there is one."""
    if any("future_date_leak" in row for row in run["items"]):
        return sum(bool(row.get("future_date_leak")) for row in run["items"])
    report = Path(__file__).resolve().parents[3] / "docs" / "evidence" / f"investigation-{run['run']}.json"
    if report.exists():
        return len({f["id"] for f in json.loads(report.read_text(encoding="utf-8"))["findings"] if f["class"] == "temporal"})
    return 0


def mean_of(run: dict, key: str, ids: set[str] | None = None) -> float | None:
    values = [row[key] for row in run["items"] if row.get(key) is not None and (ids is None or row["id"] in ids)]
    return mean(values) if values else None


def release(baseline_full: dict, baseline_targets: dict, candidate_full: dict, candidate_targets: dict,
            max_cost_increase: float = 0.10, goal: str = "recall") -> dict:
    """The team's release rule. The candidate ships only if every check passes, and then only after review."""
    targets = {row["id"] for row in baseline_targets["items"]} & {row["id"] for row in candidate_targets["items"]}

    def target_recall(run):
        return mean_of(run, "source_recall", targets)

    noise = max(abs(target_recall(baseline_full) - target_recall(baseline_targets)),
                abs(target_recall(candidate_full) - target_recall(candidate_targets)))
    gain = (mean([target_recall(candidate_full), target_recall(candidate_targets)])
            - mean([target_recall(baseline_full), target_recall(baseline_targets)]))
    full_gain = mean_of(candidate_full, "source_recall") - mean_of(baseline_full, "source_recall")

    def worse_items(candidate, baseline):
        base = {row["id"]: row for row in baseline["items"]}
        return [row["id"] for row in candidate["items"] if row["id"] in base
                and (row.get("correctness") or 0) < (base[row["id"]].get("correctness") or 0)]

    cost_change = mean_of(candidate_full, "cost_usd") / mean_of(baseline_full, "cost_usd") - 1
    leaks = {"baseline": leak_count(baseline_full) + leak_count(baseline_targets),
             "candidate": leak_count(candidate_full) + leak_count(candidate_targets)}
    recall = {"gain_on_targets": round(gain, 4), "noise": round(noise, 4), "gain_on_all_22": round(full_gain, 4)}
    if goal == "leaks":
        recall_check = ("1 source_recall no loss beyond noise", {"pass": gain >= -noise and full_gain >= -noise, **recall})
        leak_check = ("4 future_date_leak fewer", {"pass": leaks["candidate"] < leaks["baseline"], **leaks})
    else:
        recall_check = ("1 source_recall gain beyond noise", {"pass": gain > noise and full_gain >= 0, **recall})
        leak_check = ("4 future_date_leak not worse", {"pass": leaks["candidate"] <= leaks["baseline"], **leaks})
    checks = {
        recall_check[0]: recall_check[1],
        "2 no item loses correctness": {"pass": not (worse_items(candidate_full, baseline_full)
                                                     + worse_items(candidate_targets, baseline_targets)),
                                        "items": worse_items(candidate_full, baseline_full)
                                        + worse_items(candidate_targets, baseline_targets)},
        "3 every candidate trace complete": {"pass": all(row.get("trace_complete") for run in (candidate_full, candidate_targets)
                                                         for row in run["items"])},
        leak_check[0]: leak_check[1],
        "5 cost increase at most 10%": {"pass": cost_change <= max_cost_increase, "change": round(cost_change, 4)},
    }
    return {"ship_after_review": all(check["pass"] for check in checks.values()), "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    diff = sub.add_parser("diff", help="item-by-item comparison of two runs (default)")
    diff.add_argument("a")
    diff.add_argument("b")
    rule = sub.add_parser("release", help="apply the release rule: baseline full and targets, candidate full and targets")
    rule.add_argument("--baseline", nargs=2, required=True, metavar=("FULL", "TARGETS"))
    rule.add_argument("--candidate", nargs=2, required=True, metavar=("FULL", "TARGETS"))
    rule.add_argument("--goal", choices=("recall", "leaks"), default="recall",
                      help="what the candidate promises: more source recall (a prompt) or fewer leaks (a Helix fix)")
    args = parser.parse_args()
    if args.command == "release":
        verdict = release(*(load_merged(name) for name in args.baseline + args.candidate), goal=args.goal)
        print(json.dumps(verdict, indent=2))
        print("\nverdict:", "candidate passes every check; ship only after human review (promote + git diff)"
              if verdict["ship_after_review"] else "candidate does not pass the release rule")
        return
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
