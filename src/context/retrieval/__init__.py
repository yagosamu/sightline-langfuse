"""Read-side of Helix: temporal fact search, entity lookup and source documents."""

from datetime import datetime, time, timezone
from functools import cache

from graphiti_core import Graphiti
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodicNode
from graphiti_core.search.search_config_recipes import NODE_HYBRID_SEARCH_RRF
from pypdf import PdfReader

from context.config import DATA_DIR, settings
from context.ingest.sources import PARSERS
from observability.tracing import observation, record

MAX_SOURCE_CHARS = 15_000


def parse_as_of(as_of: str | None) -> datetime | None:
    if not as_of:
        return None
    day = datetime.strptime(as_of[:10], "%Y-%m-%d").date()
    return datetime.combine(day, time.max, tzinfo=timezone.utc)


def episode_path(name: str) -> str:
    if name.startswith("zendesk/tickets.json#"):
        return f"zendesk/comments/{name.split('#', 1)[1].split('@', 1)[0]}.json"
    return name.split("#", 1)[0]


def _day(value: datetime | None) -> str | None:
    return value.date().isoformat() if value else None


def fact_status(edge: EntityEdge, as_of: datetime | None, known_since: datetime | None) -> str:
    if as_of is None:
        return "superseded" if edge.invalid_at else "current"
    if known_since and known_since > as_of:
        return "recorded after as_of"
    if edge.valid_at and edge.valid_at > as_of:
        return "not yet true on as_of"
    if edge.invalid_at and edge.invalid_at <= as_of:
        return "no longer true on as_of"
    return "true on as_of"


def visible_until(edge: EntityEdge, as_of: datetime | None) -> datetime | None:
    """A fact's end date, unless it lies after as_of: on as_of nobody knew yet that the fact would end."""
    if as_of is not None and edge.invalid_at and edge.invalid_at > as_of:
        return None
    return edge.invalid_at


async def search_facts(graphiti: Graphiti, query: str, as_of: str | None = None, limit: int = 10) -> dict:
    as_of_dt = parse_as_of(as_of)
    with observation("search-graph", "retriever", input={"query": query}) as obs:
        edges = await graphiti.search(query, group_ids=[settings().group_id], num_results=max(limit * 2, 20))
        episode_ids = sorted({uuid for edge in edges for uuid in edge.episodes})
        episodes = {ep.uuid: ep for ep in await EpisodicNode.get_by_uuids(graphiti.driver, episode_ids)} if episode_ids else {}
        record(obs, output={"edges": len(edges), "episodes": len(episodes)})

    facts, hidden = [], 0
    with observation("filter-facts-as-of", input={"as_of": as_of, "limit": limit}) as obs:
        for edge in edges:
            sources = [episodes[u] for u in edge.episodes if u in episodes]
            known_since = min((ep.valid_at for ep in sources if ep.valid_at), default=None)
            status = fact_status(edge, as_of_dt, known_since)
            if status in {"recorded after as_of", "not yet true on as_of"}:
                hidden += 1
                continue
            facts.append({
                "fact": edge.fact,
                "status": status,
                "valid_from": _day(edge.valid_at),
                "valid_until": _day(visible_until(edge, as_of_dt)),
                "sources": [{"path": episode_path(ep.name), "description": ep.source_description, "dated": _day(ep.valid_at)}
                            for ep in sources if as_of_dt is None or ep.valid_at is None or ep.valid_at <= as_of_dt],
            })
            if len(facts) == limit:
                break
        record(obs, output={"kept": len(facts), "hidden_as_future": hidden})
    return {"query": query, "as_of": as_of, "facts": facts, "hidden_as_future": hidden}


def entity_view(node: EntityNode, mentioned_on: list[datetime], as_of: datetime | None) -> dict | None:
    """An entity as it was known on as_of, or None if nothing mentioned it yet.

    Graphiti writes one summary from every episode that mentions the entity, later ones included, and a
    summary cannot be cut back to a date. So with as_of the summary and attributes are kept only when no
    later episode mentions the entity; otherwise only the name and types are left.
    """
    view = {"name": node.name, "types": [label for label in node.labels if label != "Entity"],
            "summary": node.summary, "attributes": node.attributes}
    if as_of is None:
        return view
    if mentioned_on and min(mentioned_on) > as_of:
        return None
    if any(day > as_of for day in mentioned_on):
        view |= {"summary": None, "attributes": {}, "note": "mentioned again after as_of: summary withheld"}
    return view


async def find_entities(graphiti: Graphiti, query: str, as_of: str | None = None, limit: int = 8) -> dict:
    as_of_dt = parse_as_of(as_of)
    config = NODE_HYBRID_SEARCH_RRF.model_copy(deep=True)
    config.limit = limit
    results = await graphiti.search_(query, config=config, group_ids=[settings().group_id])
    entities, hidden = [], 0
    for node in results.nodes:
        mentioned_on = []
        if as_of_dt is not None:
            episodes = await EpisodicNode.get_by_entity_node_uuid(graphiti.driver, node.uuid)
            mentioned_on = [ep.valid_at for ep in episodes if ep.valid_at]
        view = entity_view(node, mentioned_on, as_of_dt)
        if view is None:
            hidden += 1
        else:
            entities.append(view)
    return {"query": query, "as_of": as_of, "entities": entities, "hidden_as_future": hidden}


def read_source(path: str, as_of: str | None = None) -> dict:
    relative = episode_path(path.strip().lstrip("/"))
    if relative not in source_paths():
        return {"path": relative, "error": "not a source document; use list_sources to see valid paths"}
    if as_of and source_days()[relative] > as_of[:10]:
        return {"path": relative, "error": "recorded after as_of: this document did not exist yet on that date"}
    target = DATA_DIR / relative
    if target.suffix == ".pdf":
        text = "\n".join(page.extract_text() for page in PdfReader(target).pages)
    else:
        text = target.read_text()
    return {"path": relative, "truncated": len(text) > MAX_SOURCE_CHARS, "content": text[:MAX_SOURCE_CHARS]}


@cache
def source_paths() -> frozenset[str]:
    """Only parsed source documents are readable: never the story bible or the ground truth."""
    return frozenset(ep.path for parse in PARSERS.values() for ep in parse())


@cache
def source_days_by_system() -> dict[str, dict[str, str]]:
    """The date of each source document, per system: the date of its first parsed episode."""
    systems: dict[str, dict[str, str]] = {}
    for system, parse in PARSERS.items():
        seen: dict[str, str] = {}
        for ep in parse():
            seen.setdefault(ep.path, ep.reference_time.date().isoformat())
        systems[system] = seen
    return systems


def source_days() -> dict[str, str]:
    return {path: day for seen in source_days_by_system().values() for path, day in seen.items()}


def list_sources(as_of: str | None = None) -> dict:
    """Every source document with its date. With as_of, only the documents that existed on that date."""
    return {"systems": {system: [{"path": path, "dated": day} for path, day in sorted(seen.items())
                                 if not as_of or day <= as_of[:10]]
                        for system, seen in source_days_by_system().items()}}
