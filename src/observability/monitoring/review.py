"""Human review through the orbisa-review annotation queue.

    uv run python -m observability.monitoring.review enqueue-live --session live-2026-10-19 --lowest 2
    uv run python -m observability.monitoring.review enqueue-sample --run production-targets-2 --per-category 2
    uv run python -m observability.monitoring.review show
    uv run python -m observability.monitoring.review verdict <trace_id> correct|partially_correct|incorrect --comment "..."
    uv run python -m observability.monitoring.review to-dataset <trace_id>
    uv run python -m observability.monitoring.review agreement

- enqueue-live queues the live answers with the lowest faithfulness (an answer with no Sources line first).
- enqueue-sample queues experiment answers per target category, to check the judge against people (C5).
- verdict records the reviewer's human_correctness score and marks the queue item completed.
- to-dataset turns a reviewed trace into an item of orbisa-reviewed, kept apart from the 22-question
  ground truth so experiments on it stay comparable.
- agreement compares human_correctness with the judge's correctness on the traces that have both.
"""

import argparse
import json
import os
import sys

import httpx

import context.config  # noqa: F401  (loads .env)
from observability.datasets import TARGET_CATEGORIES
from observability.experiments.compare import load
from observability.monitoring import (AGENT_OBSERVATION, FAITHFULNESS, HUMAN_CORRECTNESS, HUMAN_VERDICTS,
                                      REVIEW_QUEUE, REVIEWED_DATASET)
from observability.tracing import get_langfuse, missing_env


def client():
    langfuse = get_langfuse()
    if langfuse is None:
        sys.exit(f"Langfuse is not configured (missing {', '.join(missing_env())})")
    return langfuse


def scores(langfuse, trace_id: str) -> dict[str, dict]:
    """Scores on a trace, by name. The SDK's scores client calls the v1 endpoint, which v4 events_only
    deployments do not serve, so this reads /api/public/v3/scores with the same credentials."""
    response = httpx.get(f"{os.environ['LANGFUSE_BASE_URL'].rstrip('/')}/api/public/v3/scores",
                         params={"traceId": trace_id, "fields": "core,details", "limit": 100},
                         auth=(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]), timeout=10)
    response.raise_for_status()
    return {s["name"]: s for s in response.json()["data"]}


def agent_observation(langfuse, trace_id: str):
    rows = langfuse.api.observations.get_many(trace_id=trace_id, name=AGENT_OBSERVATION,
                                              fields="core,basic,io,metadata", limit=1).data
    if not rows:
        sys.exit(f"no {AGENT_OBSERVATION} observation in trace {trace_id}")
    return rows[0]


def as_json(value):
    return json.loads(value) if isinstance(value, str) and value[:1] in "{[\"" else value


def queue(langfuse):
    return next(q for q in langfuse.api.annotation_queues.list_queues(limit=100).data if q.name == REVIEW_QUEUE)


def queue_items(langfuse, queue_id: str) -> list:
    return list(langfuse.api.annotation_queues.list_queue_items(queue_id, limit=100).data)


def enqueue(langfuse, trace_ids: list[str]) -> list[str]:
    q = queue(langfuse)
    queued = {item.object_id for item in queue_items(langfuse, q.id)}
    added = [t for t in trace_ids if t not in queued]
    for trace_id in added:
        langfuse.api.annotation_queues.create_queue_item(q.id, object_id=trace_id, object_type="TRACE")
    return added


def lowest_live(langfuse, session: str, n: int) -> list[str]:
    roots = langfuse.api.observations.get_many(name=AGENT_OBSERVATION, session_id=session, fields="core", limit=100).data

    def rank(trace_id):
        s = scores(langfuse, trace_id)
        has_line = s.get("has_sources_line", {}).get("value", True)
        return (bool(has_line), s.get(FAITHFULNESS, {}).get("value", 1.0))

    return sorted({o.trace_id for o in roots}, key=rank)[:n]


def sample_run(run: str, per_category: int) -> list[str]:
    rows = load(run)["items"]
    return [row["trace_id"] for category in TARGET_CATEGORIES
            for row in [r for r in rows if r["category"] == category][:per_category]]


def show(langfuse) -> None:
    q = queue(langfuse)
    for item in queue_items(langfuse, q.id):
        obs = agent_observation(langfuse, item.object_id)
        s = scores(langfuse, item.object_id)
        question = as_json(obs.input)
        print(f"[{item.status}] trace {item.object_id}  (as of {question['as_of']})\n  Q: {question['question']}")
        print(f"  A: {str(as_json(obs.output))[:400]}")
        for name in ("correctness", FAITHFULNESS, "has_sources_line", HUMAN_CORRECTNESS):
            if name in s:
                print(f"  {name}: {s[name].get('value')}  {(s[name].get('comment') or '')[:300]}")
        print()


def verdict(langfuse, trace_id: str, label: str, comment: str) -> None:
    config = next(c for c in langfuse.api.score_configs.get(limit=100).data if c.name == HUMAN_CORRECTNESS)
    langfuse.create_score(trace_id=trace_id, name=HUMAN_CORRECTNESS, value=label, data_type="CATEGORICAL",
                          config_id=config.id, comment=comment)
    q = queue(langfuse)
    for item in queue_items(langfuse, q.id):
        if item.object_id == trace_id:
            langfuse.api.annotation_queues.update_queue_item(q.id, item.id, status="COMPLETED")
    langfuse.flush()
    print(f"{HUMAN_CORRECTNESS}={label} on {trace_id}; queue item completed")


def to_dataset(langfuse, trace_id: str) -> None:
    obs = agent_observation(langfuse, trace_id)
    review = scores(langfuse, trace_id).get(HUMAN_CORRECTNESS)
    if review is None:
        sys.exit(f"trace {trace_id} has no {HUMAN_CORRECTNESS} yet: review it first")
    try:
        langfuse.get_dataset(REVIEWED_DATASET)
    except Exception:
        langfuse.create_dataset(name=REVIEWED_DATASET,
                                description="Live answers reviewed by people; kept apart from orbisa-truth")
    question = as_json(obs.input)
    langfuse.create_dataset_item(
        dataset_name=REVIEWED_DATASET, id=f"reviewed-{trace_id[:12]}", input=question,
        expected_output={"reviewer_verdict": review.get("value"), "reviewer_note": review.get("comment"),
                         "reviewed_answer": as_json(obs.output)},
        metadata={"source": "live review", "session_id": obs.session_id},
        source_trace_id=trace_id, source_observation_id=obs.id)
    langfuse.flush()
    print(f"{REVIEWED_DATASET}: item reviewed-{trace_id[:12]} from trace {trace_id}")


def agreement(langfuse) -> None:
    q = queue(langfuse)
    pairs = []
    for item in queue_items(langfuse, q.id):
        s = scores(langfuse, item.object_id)
        if HUMAN_CORRECTNESS in s and "correctness" in s:
            human = HUMAN_VERDICTS[s[HUMAN_CORRECTNESS]["value"]]  # v3 returns the category label as value
            pairs.append((item.object_id, human, s["correctness"]["value"]))
    agree = sum(human == judge for _, human, judge in pairs)
    for trace_id, human, judge in pairs:
        print(f"  {trace_id}: human {human} vs judge {judge}{'' if human == judge else '  <- disagree'}")
    print(f"C5 agreement: {agree}/{len(pairs)} (target: at least 5 of 6)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    live = sub.add_parser("enqueue-live")
    live.add_argument("--session", required=True)
    live.add_argument("--lowest", type=int, default=2)
    sample = sub.add_parser("enqueue-sample")
    sample.add_argument("--run", required=True)
    sample.add_argument("--per-category", type=int, default=2)
    sub.add_parser("show")
    v = sub.add_parser("verdict")
    v.add_argument("trace_id")
    v.add_argument("label", choices=list(HUMAN_VERDICTS))
    v.add_argument("--comment", default="")
    d = sub.add_parser("to-dataset")
    d.add_argument("trace_id")
    sub.add_parser("agreement")
    args = parser.parse_args()

    langfuse = client()
    if args.command == "enqueue-live":
        print("queued:", enqueue(langfuse, lowest_live(langfuse, args.session, args.lowest)))
    elif args.command == "enqueue-sample":
        print("queued:", enqueue(langfuse, sample_run(args.run, args.per_category)))
    elif args.command == "show":
        show(langfuse)
    elif args.command == "verdict":
        verdict(langfuse, args.trace_id, args.label, args.comment)
    elif args.command == "to-dataset":
        to_dataset(langfuse, args.trace_id)
    elif args.command == "agreement":
        agreement(langfuse)


if __name__ == "__main__":
    main()
