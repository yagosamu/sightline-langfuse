"""Run Ask Orbisa on the orbisa-truth dataset as a Langfuse experiment.

    make experiment ARGS="--name production-full --prompt-label production"
    make experiment ARGS="--name staging-targets --prompt-label staging --targets"

Runs inside Docker (the agent image), so the Claude Code CLI does not pick up a developer's own
settings, hooks or plugins. Each item gets correctness (LLM judge), trap_hit, source_recall,
retrieved_source_recall, has_sources_line and, for false_premise items, abstained. The run gets
correctness overall and per category, source recall, mean cost, cost per correct answer and p95
latency. After the run, trace_complete (C1) is scored from the Langfuse API. A JSON summary is
written to .helix/experiments/<name>.json.
"""

import argparse
import json
import sys
import time
from datetime import date

from langfuse import Evaluation

from clients.agent.runner import ask
from context.config import STATE_DIR
from observability import tracing
from observability.datasets import DATASET_NAME, TARGET_CATEGORIES
from observability.evaluators import (abstained, future_date_leak, has_sources_line, retrieved_source_recall,
                                      source_recall)
from observability.evaluators.judge import JUDGE_MODEL, judge
from observability.experiments import summarize, targets_met
from observability.prompts import prompt_label

HELIX_OBSERVATIONS = {"search-context", "find-entities", "read-source", "list-sources"}


def make_task(run_name: str, label: str, model: str | None):
    async def task(*, item, **_):
        question, meta = item.input, item.metadata
        run = await ask(question["question"], question["as_of"], model=model, prompt_label=label,
                        user_id="experiment", session_id=run_name, isolate_cli_config=True,
                        tags=["experiment", meta["category"], meta["id"], f"as-of:{question['as_of']}"])
        return {"answer": run.answer, "retrieved_sources": sorted(run.retrieved_sources() | run.read_sources()),
                "tool_calls": len(run.tool_calls), "cost_usd": run.cost_usd, "duration_ms": run.duration_ms,
                "num_turns": run.num_turns, "is_error": run.is_error}
    return task


def evaluate_item(*, input, output, expected_output, metadata, **_) -> list[Evaluation]:
    answer, as_of, expected = output["answer"], input["as_of"], expected_output["sources"]
    verdict = judge(question=input["question"], as_of=as_of, category=metadata["category"], answer=answer,
                    expected_answer=expected_output["answer"], expected_facts=expected_output["facts"],
                    trap=metadata["trap"])
    leak, leak_comment = future_date_leak(answer, as_of)
    evaluations = [Evaluation(name="has_sources_line", value=has_sources_line(answer), data_type="BOOLEAN"),
                   Evaluation(name="future_date_leak", value=leak, comment=leak_comment, data_type="BOOLEAN")]
    if verdict.score is not None:
        evaluations += [Evaluation(name="correctness", value=verdict.score, comment=f"{verdict.verdict}: {verdict.reason}"),
                        Evaluation(name="trap_hit", value=verdict.trap_hit, data_type="BOOLEAN")]
    for name, (value, comment) in {"source_recall": source_recall(answer, expected, as_of),
                                   "retrieved_source_recall": retrieved_source_recall(
                                       set(output["retrieved_sources"]), expected, as_of)}.items():
        if value is not None:
            evaluations.append(Evaluation(name=name, value=value, comment=comment))
    if metadata["category"] == "false_premise":
        evaluations.append(Evaluation(name="abstained", value=abstained(answer), data_type="BOOLEAN"))
    return evaluations


def item_row(result) -> dict:
    scores = {e.name: e.value for e in result.evaluations}
    output = result.output or {}
    return {"id": result.item.metadata["id"], "category": result.item.metadata["category"],
            "trace_id": result.trace_id, **{k: output.get(k) for k in ("cost_usd", "duration_ms", "num_turns", "is_error",
                                                                      "tool_calls")},
            **{k: scores.get(k) for k in ("correctness", "trap_hit", "source_recall", "retrieved_source_recall",
                                          "has_sources_line", "abstained", "future_date_leak")}}


def evaluate_run(*, item_results, **_) -> list[Evaluation]:
    summary = summarize([item_row(r) for r in item_results])
    evaluations = [Evaluation(name=f"run_{key}", value=summary[key]) for key in
                   ("correctness", "correctness_targets", "correctness_target_floor", "source_recall",
                    "future_date_leak_rate",
                    "retrieved_source_recall", "cost_usd_mean", "cost_usd_per_correct_answer", "latency_s_p95")
                   if summary[key] is not None]
    evaluations += [Evaluation(name=f"run_correctness_{category}", value=value)
                    for category, value in summary["correctness_by_category"].items() if value is not None]
    return evaluations


def trace_complete(langfuse, row: dict) -> bool:
    """C1: the question's trace has the agent root, its answer, and one Helix observation per tool call."""
    rows = langfuse.api.observations.get_many(trace_id=row["trace_id"], fields="core,basic", limit=100).data
    names = [o.name for o in rows]
    helix = sum(name in HELIX_OBSERVATIONS for name in names)
    return "answer-question" in names and helix == (row["tool_calls"] or 0) and helix > 0


def score_trace_complete(langfuse, rows: list[dict], wait_s: int = 60) -> None:
    """Score each trace once it is complete, or as incomplete after `wait_s` (ingestion is asynchronous)."""
    deadline = time.monotonic() + wait_s
    pending = [row for row in rows if row["trace_id"]]
    while pending:
        for row in list(pending):
            complete = trace_complete(langfuse, row)
            if complete or time.monotonic() > deadline:
                row["trace_complete"] = complete
                langfuse.create_score(trace_id=row["trace_id"], name="trace_complete", value=1 if complete else 0,
                                      data_type="BOOLEAN")
                pending.remove(row)
        if pending:
            time.sleep(5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True, help="run name, e.g. production-full")
    parser.add_argument("--prompt-label", help="system prompt label (default: HELIX_PROMPT_LABEL or production)")
    parser.add_argument("--ids", help="comma-separated item ids, e.g. Q01,Q03")
    parser.add_argument("--targets", action="store_true", help=f"only the target categories: {', '.join(TARGET_CATEGORIES)}")
    parser.add_argument("--max-concurrency", type=int, default=4)
    parser.add_argument("--model", help="override HELIX_AGENT_MODEL")
    args = parser.parse_args()

    langfuse = tracing.get_langfuse()
    if langfuse is None:
        sys.exit(f"Langfuse is not configured (missing {', '.join(tracing.missing_env())})")
    label = prompt_label(args.prompt_label)
    items = langfuse.get_dataset(DATASET_NAME).items
    if args.ids:
        wanted = {i.strip().upper() for i in args.ids.split(",")}
        items = [item for item in items if item.id in wanted]
    if args.targets:
        items = [item for item in items if item.metadata["category"] in TARGET_CATEGORIES]
    items = sorted(items, key=lambda item: item.id)
    print(f"{args.name}: {len(items)} items, prompt label {label}, judge {JUDGE_MODEL}")

    result = langfuse.run_experiment(
        name=DATASET_NAME, run_name=args.name, data=items, task=make_task(args.name, label, args.model),
        evaluators=[evaluate_item], run_evaluators=[evaluate_run], max_concurrency=args.max_concurrency,
        metadata={"prompt_label": label, "judge_model": JUDGE_MODEL, "agent_model": args.model or "default"},
    )
    langfuse.flush()
    rows = sorted((item_row(r) for r in result.item_results), key=lambda row: row["id"])
    score_trace_complete(langfuse, rows)
    langfuse.flush()

    summary = summarize(rows)
    summary["trace_complete"] = sum(bool(row.get("trace_complete")) for row in rows) / len(rows) if rows else None
    report = {"run": args.name, "date": date.today().isoformat(), "prompt_label": label, "judge_model": JUDGE_MODEL,
              "dataset_run_url": result.dataset_run_url, "summary": summary, "targets": targets_met(summary),
              "items": rows}
    out = STATE_DIR / "experiments" / f"{args.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({k: report[k] for k in ("summary", "targets")}, indent=2))
    print(f"wrote {out}")
    tracing.shutdown()


if __name__ == "__main__":
    main()
