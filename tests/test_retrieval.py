from datetime import datetime, timezone
from types import SimpleNamespace

from context.retrieval import (entity_view, episode_path, fact_status, list_sources, parse_as_of, read_source,
                               source_days, visible_until)


def dt(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)


def edge(valid_at=None, invalid_at=None):
    return SimpleNamespace(valid_at=valid_at, invalid_at=invalid_at)


def test_status_without_as_of():
    assert fact_status(edge(dt("2023-02-01")), None, None) == "current"
    assert fact_status(edge(dt("2023-02-01"), dt("2026-03-01")), None, None) == "superseded"


def test_status_with_as_of():
    as_of = parse_as_of("2025-12-01")
    finance_rule = edge(dt("2023-02-01"), dt("2026-03-03"))
    revops_rule = edge(dt("2026-03-03"))
    assert fact_status(finance_rule, as_of, dt("2023-02-01")) == "true on as_of"
    assert fact_status(revops_rule, as_of, dt("2026-03-03")) == "recorded after as_of"
    assert fact_status(finance_rule, parse_as_of("2026-10-15"), dt("2023-02-01")) == "no longer true on as_of"


def test_an_end_date_after_as_of_is_hidden():
    finance_rule = edge(dt("2023-02-01"), dt("2026-03-03"))
    assert visible_until(finance_rule, parse_as_of("2025-12-01")) is None
    assert visible_until(finance_rule, parse_as_of("2026-10-15")) == dt("2026-03-03")
    assert visible_until(finance_rule, None) == dt("2026-03-03")


def test_as_of_includes_the_whole_day():
    assert fact_status(edge(dt("2026-09-29")), parse_as_of("2026-09-29"), dt("2026-09-29")) == "true on as_of"


def test_episode_path_maps_zendesk_tickets_to_comment_threads():
    assert episode_path("zendesk/tickets.json#48233@2026-09-30") == "zendesk/comments/48233.json"
    assert episode_path("hubspot/companies.csv#18342001") == "hubspot/companies.csv"


def test_read_source_reads_pdf_and_blocks_escapes():
    msa = read_source("drive/Orion_Freight_Systems_MSA_2025-08-15.pdf")
    assert "7.3 SLA refund right" in msa["content"]
    for bad in ["../../.env", "/etc/passwd", "confluence/../../../pyproject.toml", "README.md", "truth/questions.jsonl"]:
        assert "error" in read_source(bad)


def test_list_sources_covers_all_systems():
    systems = list_sources()["systems"]
    assert set(systems) == {"confluence", "notion", "slack", "drive", "hubspot", "zendesk", "gong"}
    assert all(systems.values())


def test_list_sources_as_of_hides_later_documents():
    every = [d for docs in list_sources()["systems"].values() for d in docs]
    then = [d for docs in list_sources("2026-01-15")["systems"].values() for d in docs]
    assert then and len(then) < len(every)
    assert all(d["dated"] <= "2026-01-15" for d in then)
    assert {d["path"] for d in every if d["dated"] <= "2026-01-15"} == {d["path"] for d in then}


def test_read_source_as_of_refuses_later_documents():
    path = "drive/Orion_Freight_Systems_MSA_2025-08-15.pdf"
    day = source_days()[path]
    assert "error" in read_source(path, as_of="2000-01-01")
    assert "content" in read_source(path, as_of=day)
    assert "content" in read_source(path)


def brisklane():
    return SimpleNamespace(name="Brisklane", labels=["Entity", "Organization"], attributes={"city": "Lisbon"},
                           summary="Acquired on 2025-03-10. From 2026-01-01 the 24-month retention applies.")


def test_entity_summary_is_kept_only_when_nothing_later_mentions_it():
    mentions = [dt("2025-03-10"), dt("2026-01-01")]
    assert entity_view(brisklane(), mentions, None)["summary"].startswith("Acquired")
    assert entity_view(brisklane(), mentions, parse_as_of("2026-10-15"))["summary"].startswith("Acquired")
    then = entity_view(brisklane(), mentions, parse_as_of("2025-10-01"))
    assert then["name"] == "Brisklane" and then["types"] == ["Organization"]
    assert then["summary"] is None and then["attributes"] == {}


def test_an_entity_first_mentioned_after_as_of_is_hidden():
    assert entity_view(brisklane(), [dt("2025-03-10")], parse_as_of("2025-01-01")) is None
