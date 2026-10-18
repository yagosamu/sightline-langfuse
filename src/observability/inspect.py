"""Print the latest Ask Orbisa trace as a tree, read back from the Langfuse API.

    uv run python -m observability.inspect              # latest answer-question trace
    uv run python -m observability.inspect --trace <id>

Tokens come from Langfuse usage when it is mapped; otherwise from the CLI's raw span attributes,
marked with "(attr)".
"""

import argparse
import sys
from collections import defaultdict

import context.config  # noqa: F401  (loads .env)
from observability.tracing import get_langfuse, missing_env

ROOT_NAME = "answer-question"
FIELDS = "core,basic,usage,model,metadata"
TOKEN_ATTRIBUTES = {"in": "attributes.input_tokens", "out": "attributes.output_tokens",
                    "cache_read": "attributes.cache_read_tokens", "cache_write": "attributes.cache_creation_tokens"}


def latest_trace_id(api) -> str | None:
    roots = api.observations.get_many(name=ROOT_NAME, fields="core", limit=50).data
    return max(roots, key=lambda o: o.start_time).trace_id if roots else None


def trace_observations(api, trace_id: str) -> list:
    rows, cursor = [], None
    while True:
        page = api.observations.get_many(trace_id=trace_id, fields=FIELDS, limit=100, cursor=cursor)
        rows += page.data
        cursor = getattr(page.meta, "cursor", None)
        if not cursor or not page.data:
            return rows


def tokens(o) -> str:
    usage = {k: v for k, v in (o.usage_details or {}).items() if v}
    if usage:
        return " ".join(f"{k}={v}" for k, v in usage.items())
    meta = o.metadata or {}
    found = {label: meta[key] for label, key in TOKEN_ATTRIBUTES.items() if meta.get(key) is not None}
    return " ".join(f"{k}={v}" for k, v in found.items()) + " (attr)" if found else ""


def line(o) -> str:
    parts = [f"{o.type:<10}", o.name, f"{o.latency:.2f}s" if o.latency is not None else ""]
    if o.model:
        parts.append(o.model)
    if t := tokens(o):
        parts.append(t)
    if o.total_cost:
        parts.append(f"${o.total_cost:.4f}")
    if o.name == ROOT_NAME and (cost := (o.metadata or {}).get("cost_usd")) is not None:
        parts.append(f"cost_usd={float(cost):.4f} (ResultMessage)")
    return "  ".join(p for p in parts if p)


def print_tree(rows: list) -> None:
    ids = {o.id for o in rows}
    children = defaultdict(list)
    for o in rows:
        children[o.parent_observation_id if o.parent_observation_id in ids else None].append(o)

    def walk(parent, depth):
        for o in sorted(children[parent], key=lambda o: o.start_time):
            print("  " * depth + line(o))
            walk(o.id, depth + 1)

    walk(None, 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trace", help="trace id; defaults to the latest answer-question trace")
    args = parser.parse_args()

    langfuse = get_langfuse()
    if langfuse is None:
        sys.exit(f"Langfuse is not configured (missing {', '.join(missing_env())})")
    trace_id = args.trace or latest_trace_id(langfuse.api)
    if trace_id is None:
        sys.exit(f"no {ROOT_NAME} trace found")
    rows = trace_observations(langfuse.api, trace_id)
    by_type = defaultdict(int)
    for o in rows:
        by_type[o.type] += 1
    print(f"trace {trace_id}: {len(rows)} observations ({', '.join(f'{n} {t}' for t, n in sorted(by_type.items()))})\n")
    print_tree(rows)


if __name__ == "__main__":
    main()
