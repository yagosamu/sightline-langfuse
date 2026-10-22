"""What each Helix MCP tool records in its observation: (output, metadata).

Names and keys here are what evaluators and dashboards filter on, so they stay stable.
"""

from collections import Counter
from typing import Any

PREVIEW_CHARS = 3_000  # read-source keeps a preview, not whole customer documents


def system_of(path: str) -> str:
    return path.split("/", 1)[0]


def search_context(result: dict) -> tuple[Any, dict[str, Any]]:
    facts = result.get("facts", [])
    paths = sorted({src["path"] for fact in facts for src in fact.get("sources", [])})
    output = {
        "facts": [
            {"fact": f["fact"], "status": f["status"], "valid_from": f["valid_from"],
             "valid_until": f["valid_until"], "sources": [s["path"] for s in f.get("sources", [])]}
            for f in facts
        ],
        "hidden_as_future": result.get("hidden_as_future", 0),
    }
    metadata = {
        "as_of": result.get("as_of"),
        "fact_count": len(facts),
        "hidden_as_future": result.get("hidden_as_future", 0),
        "status_counts": dict(Counter(f["status"] for f in facts)),
        "source_systems": sorted({system_of(p) for p in paths}),
        "source_paths": paths,
    }
    return output, metadata


def find_entities(result: dict) -> tuple[Any, dict[str, Any]]:
    entities = result.get("entities", [])
    output = [{"name": e["name"], "types": e["types"], "summary": e.get("summary")} for e in entities]
    return output, {"entity_count": len(entities), "hidden_as_future": result.get("hidden_as_future", 0)}


def read_source(result: dict) -> tuple[Any, dict[str, Any]]:
    path = result.get("path", "")
    if "error" in result:
        return result, {"path": path, "error": result["error"]}
    content = result.get("content", "")
    output = {"path": path, "length": len(content), "truncated": result.get("truncated", False),
              "preview": content[:PREVIEW_CHARS]}
    return output, {"path": path, "system": system_of(path), "length": len(content)}


def list_sources(result: dict) -> tuple[Any, dict[str, Any]]:
    counts = {system: len(docs) for system, docs in result.get("systems", {}).items()}
    return counts, {"document_count": sum(counts.values())}
