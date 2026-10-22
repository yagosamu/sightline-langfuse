"""Investigate an experiment run through the Langfuse MCP server, read-only.

    uv run python -m observability.monitoring.investigate --run production-full

1. Lists the items scored incorrect or partially correct, with the judge's reason. When none are, it
   investigates the weakest scores instead: source recall below 1 and answers that leak a date after
   their as-of date.
2. For each, reads the trace's search-context observations and classifies where it failed:
   - temporal: the answer names a date after as_of on which a source was recorded; the evidence says
     whether Helix returned that date (in a fact's validity or an entity summary) or the agent brought it in;
   - retrieval: an expected source never came back from Helix;
   - reasoning: Helix returned the expected source, the agent did not cite it, or the judge marked the
     answer wrong;
   - alias: an alias item answered wrongly.
3. Writes the findings to docs/evidence/investigation-<run>.json. Proposing a change is left to people,
   with these findings as evidence, and any change goes through an experiment and a human review.
"""

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import context.config  # noqa: F401  (loads .env)
from observability.evaluators import future_date_leak, mentioned_dates, source_dates
from observability.monitoring.langfuse_mcp import LangfuseMCP, parse

REPORT_DIR = Path(__file__).resolve().parents[3] / "docs" / "evidence"
LOOKBACK = timedelta(days=30)


def window() -> dict:
    now = datetime.now(timezone.utc)
    return {"fromStartTime": (now - LOOKBACK).isoformat().replace("+00:00", "Z"),
            "toStartTime": now.isoformat().replace("+00:00", "Z")}


def run_items(mcp: LangfuseMCP, run: str) -> list[dict]:
    page = mcp.call("listExperimentItems", fields=["core", "io", "itemMetadata", "scores"], limit=100,
                    scoreLimit=50, experimentName=[run], fromStartTime=window()["fromStartTime"])
    items = []
    for raw in page["data"]:
        output = parse(raw["output"])
        if raw.get("level") == "ERROR" or not isinstance(output, dict):
            continue  # the task failed (for example an API limit): there is no answer to investigate
        scores = {s["name"]: s for s in parse(raw.get("scores")) or []}
        items.append({"id": raw["experimentItemId"], "trace_id": raw["traceId"], "url": raw.get("url"),
                      "start": raw["startTime"],
                      "input": parse(raw["input"]), "answer": output.get("answer", ""),
                      "retrieved_sources": output.get("retrieved_sources", []),
                      "expected": parse(raw.get("expectedOutput")) or {},
                      "metadata": parse(raw.get("experimentItemMetadata")) or {},
                      "scores": {name: {"value": s.get("value"), "comment": s.get("comment")}
                                 for name, s in scores.items()}})
    return sorted(items, key=lambda item: item["id"])


def score(item: dict, name: str):
    return item["scores"].get(name, {}).get("value")


def trace_outputs(mcp: LangfuseMCP, item: dict, name: str) -> list:
    """Outputs of the observations with this name in the item's trace. The MCP observation tools take
    field names (not the SDK's field groups), at most 50 rows with output, and a range of at most 14 days,
    so the range is the item's own hour. find-entities records a list of entities, the others a dict."""
    start = datetime.fromisoformat(item["start"].replace("Z", "+00:00"))
    stamp = lambda t: t.isoformat().replace("+00:00", "Z")  # noqa: E731
    rows = mcp.call("listObservations", traceId=item["trace_id"], name=name, fields=["id", "name", "output"],
                    limit=50, fromStartTime=stamp(start - timedelta(minutes=5)),
                    toStartTime=stamp(start + timedelta(hours=1)))["data"]
    return [out for out in (parse(row.get("output")) for row in rows) if isinstance(out, (dict, list))]


def score_comments(mcp: LangfuseMCP, trace_id: str) -> dict[str, str]:
    """Score comments, which listExperimentItems leaves out."""
    return {s["name"]: s.get("comment") for s in mcp.call("listScores", limit=50, traceId=[trace_id])["data"]}


def classify(item: dict, facts: list[dict], documents: list[dict], comments: dict[str, str],
             listed_sources: bool = False, entities: list[dict] = ()) -> list[dict]:
    as_of = item["input"]["as_of"]
    dates = source_dates()
    findings = []
    if future_date_leak(item["answer"], as_of)[0]:
        recorded = set(dates.values())
        for day in sorted(d for d in mentioned_dates(item["answer"]) if d > as_of and d in recorded):
            via_facts = [f for f in facts if day in (f.get("valid_from"), f.get("valid_until"))]
            via_entities = [e["name"] for e in entities if day in (e.get("summary") or "")]
            via_documents = [d["path"] for d in documents if day in d.get("preview", "")]
            later_documents = [d["path"] for d in documents if (dates.get(d.get("path")) or "") > as_of]
            if via_facts:
                cause = "Helix returned it as a fact's validity date"
            elif via_entities:
                cause = "Helix returned it in an entity summary (find_entities)"
            elif via_documents or later_documents:
                cause = "a document read with read_source contains it or was recorded after as_of"
            elif listed_sources:
                # list_sources has no as_of: it returns every document with the date it was recorded.
                cause = "list_sources showed a document recorded on that date, after as_of"
            else:
                cause = "nothing retrieved carries it: probably a date the agent computed (likely false positive)"
            findings.append({"class": "temporal", "date": day, "cause": cause, "evidence": {
                "facts": [{"fact": f["fact"], "status": f["status"], "valid_from": f.get("valid_from"),
                           "valid_until": f.get("valid_until")} for f in via_facts][:3],
                "entities": via_entities, "documents": via_documents or later_documents}})
    recall, retrieved = score(item, "source_recall"), score(item, "retrieved_source_recall")
    if recall is not None and recall < 1:
        kind = "retrieval" if retrieved is not None and retrieved < 1 else "reasoning"
        findings.append({"class": kind, "cause": "expected source not retrieved" if kind == "retrieval"
                         else "retrieved but not cited", "evidence": comments.get("source_recall")})
    correctness = score(item, "correctness")
    if correctness is not None and correctness < 1:
        kind = "alias" if item["metadata"].get("category") == "alias" else "reasoning"
        findings.append({"class": kind, "cause": "judge marked the answer wrong", "evidence": comments.get("correctness")})
    return findings


def investigate(mcp: LangfuseMCP, run: str) -> dict:
    items = run_items(mcp, run)
    wrong = [i for i in items if (score(i, "correctness") or 1) < 1]
    weak = wrong or [i for i in items if (score(i, "source_recall") or 1) < 1
                     or future_date_leak(i["answer"], i["input"]["as_of"])[0]]
    findings = []
    for item in weak:
        facts = [f for out in trace_outputs(mcp, item, "search-context") for f in out.get("facts", [])]
        documents = trace_outputs(mcp, item, "read-source")
        listed = bool(trace_outputs(mcp, item, "list-sources"))
        entities = [e for out in trace_outputs(mcp, item, "find-entities") for e in out]
        for finding in classify(item, facts, documents, score_comments(mcp, item["trace_id"]), listed, entities):
            findings.append({"id": item["id"], "category": item["metadata"].get("category"),
                             "as_of": item["input"]["as_of"], "url": item["url"], **finding})
    return {"run": run, "items": len(items), "incorrect_or_partial": len(wrong),
            "investigated": "incorrect items" if wrong else "weakest scores (no item was graded below correct)",
            "groups": dict(Counter(f["class"] for f in findings)), "findings": findings}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", default="production-full")
    args = parser.parse_args()
    report = investigate(LangfuseMCP(), args.run)
    out = REPORT_DIR / f"investigation-{args.run}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"{report['run']}: {report['items']} items, {report['incorrect_or_partial']} incorrect or partial; "
          f"investigated {report['investigated']}")
    print("groups:", report["groups"])
    for f in report["findings"]:
        print(f"\n{f['id']} ({f['category']}, as of {f['as_of']}) -> {f['class']}: {f['cause']}")
        print(f"   {json.dumps(f.get('evidence'), ensure_ascii=False)[:400]}")
    print(f"\nwrote {out.relative_to(out.parents[2])}")


if __name__ == "__main__":
    main()
