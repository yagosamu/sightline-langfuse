"""Code evaluators (no LLM) for Ask Orbisa answers, mapped to decision 01.

- source_recall (C3): expected sources cited in the answer's "Sources:" line.
- retrieved_source_recall: expected sources that Helix returned or the agent read. Diagnostic: it tells
  "Helix did not find it" apart from "the agent did not cite it".
- has_sources_line: the answer format asks for a final "Sources:" line.
- abstained: for false_premise items, whether the answer rejects the premise. A keyword heuristic; the
  LLM judge's correctness is the authoritative score.

The ground truth is never edited. Two mismatches are handled here instead:
- Helix serves some systems under other paths (the CSVs and Gong calls inside one export, Zendesk
  tickets as one thread per ticket), so expected paths map to the paths Helix can serve.
- Some expected sources are dated after the item's as-of date. Helix hides them, so the agent cannot
  cite them; they are left out of the denominator and reported.
"""

import re
from functools import cache

# Expected ground-truth path -> path prefixes that serve the same content in Helix.
SOURCE_ALIASES = {
    "gong/calls.json": ("gong/transcripts.json",),
    "hubspot/contacts.csv": ("hubspot/companies.csv",),
    "hubspot/deals.csv": ("hubspot/companies.csv",),
    "zendesk/tickets.json": ("zendesk/comments/",),
}
SOURCES_LINE = re.compile(r"^\W*sources\W*:", re.IGNORECASE | re.MULTILINE)
ABSTAIN = re.compile(
    r"\b(there is no|there's no|has no|have no|no such|does not exist|doesn't exist|has not been|hasn't been|was not|wasn't|"
    r"not been approved|no record|not (?:in|covered by) (?:the|our) sources|sources do not|cannot find|"
    r"could not find|couldn't find|no information|not found|false premise|premise)\b",
    re.IGNORECASE)


@cache
def source_dates() -> dict[str, str]:
    """Every path Helix serves, with the date it was recorded."""
    from context import retrieval

    return {doc["path"]: doc["dated"] for docs in retrieval.list_sources()["systems"].values() for doc in docs}


def served_forms(expected: str) -> tuple[str, ...]:
    return SOURCE_ALIASES.get(expected, (expected,))


def serves(path: str, expected: str) -> bool:
    """Whether a Helix path serves an expected ground-truth source (a form ending in / is a prefix)."""
    return any(path == form or (form.endswith("/") and path.startswith(form)) for form in served_forms(expected))


def answerable(expected: list[str], as_of: str) -> tuple[list[str], list[str]]:
    """Split expected sources into (citable on as_of, dated after as_of)."""
    citable, later = [], []
    for source in expected:
        days = [day for path, day in source_dates().items() if serves(path, source)]
        (later if days and min(days) > as_of else citable).append(source)
    return citable, later


def sources_section(answer: str) -> str:
    """The text after the last "Sources:" line; empty when there is none."""
    matches = list(SOURCES_LINE.finditer(answer or ""))
    return answer[matches[-1].end():] if matches else ""


def found(expected: str, where: str | set[str]) -> bool:
    """Whether an expected source appears in an answer's text or in a set of retrieved paths."""
    if isinstance(where, str):
        return any(form in where for form in served_forms(expected))
    return any(serves(path, expected) for path in where)


def recall(expected: list[str], as_of: str, where: str | set[str]) -> tuple[float | None, str]:
    citable, later = answerable(expected, as_of)
    if not citable:
        return None, f"no expected source is citable on {as_of}" + (f" (later: {', '.join(later)})" if later else "")
    hits = [path for path in citable if found(path, where)]
    comment = f"{len(hits)}/{len(citable)} expected sources"
    if missing := [path for path in citable if path not in hits]:
        comment += f"; missing: {', '.join(missing)}"
    if later:
        comment += f"; left out, dated after {as_of}: {', '.join(later)}"
    return len(hits) / len(citable), comment


def source_recall(answer: str, expected: list[str], as_of: str) -> tuple[float | None, str]:
    return recall(expected, as_of, sources_section(answer))


def retrieved_source_recall(retrieved: set[str], expected: list[str], as_of: str) -> tuple[float | None, str]:
    return recall(expected, as_of, retrieved)


def has_sources_line(answer: str) -> bool:
    return bool(sources_section(answer).strip())


def abstained(answer: str) -> bool:
    return bool(ABSTAIN.search(answer or ""))
