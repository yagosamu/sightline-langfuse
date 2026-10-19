import pytest

from observability import evaluators as ev
from observability.datasets import truth_items

TRUTH = {q["id"]: q for q in truth_items()}

Q01_ANSWER = """Yes. Orion is eligible under clause 7.3.

- Renewal 2026-08-15, request on day 45.

Sources: drive/Orion_Freight_Systems_MSA_2025-08-15.pdf, drive/Orion_Freight_Systems_Order_Form_OF-2025-0815.pdf,
confluence/INC-2304-Postmortem_98311452.html, confluence/Refund-and-Credits-Policy_98309871.html,
slack/revops/2026-03-03.json, zendesk/comments/48233.json"""


def test_q01_cites_every_expected_source_once_paths_are_mapped():
    q = TRUTH["Q01"]
    value, comment = ev.source_recall(Q01_ANSWER, q["expected_sources"], q["as_of"])
    assert value == 1.0, comment  # zendesk/tickets.json is served as zendesk/comments/<id>.json


def test_missing_sources_lower_recall_and_are_named():
    q = TRUTH["Q01"]
    answer = Q01_ANSWER.replace("slack/revops/2026-03-03.json, ", "")
    value, comment = ev.source_recall(answer, q["expected_sources"], q["as_of"])
    assert value == pytest.approx(5 / 6)
    assert "missing: slack/revops/2026-03-03.json" in comment


@pytest.mark.parametrize("qid, later", [("Q09", "confluence/Data-Retention-Policy_98307120.html"),
                                        ("Q21", "confluence/Refund-and-Credits-Policy_98309871.html")])
def test_sources_dated_after_as_of_are_left_out(qid, later):
    q = TRUTH[qid]
    citable, left_out = ev.answerable(q["expected_sources"], q["as_of"])
    assert left_out == [later]
    assert later not in citable


def test_every_expected_source_maps_to_something_helix_serves():
    served = set(ev.source_dates())
    for q in TRUTH.values():
        for source in q["expected_sources"]:
            assert any(ev.serves(path, source) for path in served), (q["id"], source)


def test_sources_line_only_counts_the_section_after_it():
    q = TRUTH["Q01"]
    answer = "The MSA drive/Orion_Freight_Systems_MSA_2025-08-15.pdf says so.\n\nSources: slack/revops/2026-03-03.json"
    value, _ = ev.source_recall(answer, q["expected_sources"], q["as_of"])
    assert value == pytest.approx(1 / 6)
    assert ev.has_sources_line(answer)
    assert not ev.has_sources_line("No sources line here.")
    assert ev.source_recall("No sources line.", q["expected_sources"], q["as_of"])[0] == 0.0


def test_retrieved_recall_uses_paths_helix_returned():
    q = TRUTH["Q01"]
    retrieved = {"drive/Orion_Freight_Systems_MSA_2025-08-15.pdf", "zendesk/comments/48233.json"}
    value, _ = ev.retrieved_source_recall(retrieved, q["expected_sources"], q["as_of"])
    assert value == pytest.approx(2 / 6)


@pytest.mark.parametrize("answer, expected", [
    ("Orbisa has no Platinum plan. It sells Starter, Growth and Enterprise.", True),
    ("There is no Platinum plan at Orbisa.", True),
    ("The refund has not been approved yet.", True),
    ("Our sources do not cover a 2025 SOC 2 audit.", True),
    ("The refund window is 60 days.", False),
])
def test_abstained_is_a_keyword_heuristic(answer, expected):
    assert ev.abstained(answer) is expected


def test_mentioned_dates_reads_iso_and_written_dates():
    text = "Since March 3, 2026 and 15 June 2026, plus 2026-08-15, during October 2026."
    assert ev.mentioned_dates(text) == {"2026-03-03", "2026-06-15", "2026-08-15"}


def test_a_later_recording_date_is_a_leak_but_a_scheduled_date_is_not():
    # 2026-03-03 is the day the Slack message moving refunds to RevOps was recorded.
    assert ev.future_date_leak("Finance approves; RevOps took over on 2026-03-03.", "2025-12-01")[0]
    assert not ev.future_date_leak("Finance approves.", "2025-12-01")[0]
    # Kodiak's renewal date is in the future but known in advance, and no source was recorded on it.
    assert not ev.future_date_leak("Kodiak renews on 2026-11-30.", "2026-10-15")[0]
    assert not ev.future_date_leak("RevOps took over on 2026-03-03.", "2026-10-15")[0]


def test_two_reference_answers_mention_what_was_not_known_on_their_as_of_date():
    # Finding, not a fix: the ground truth stays as it is.
    flagged = {q["id"] for q in TRUTH.values() if ev.future_date_leak(q["expected_answer"], q["as_of"])[0]}
    assert flagged == {"Q03", "Q09"}


def test_abstained_accepts_every_expected_false_premise_answer_and_no_other():
    for q in TRUTH.values():
        assert ev.abstained(q["expected_answer"]) is (q["category"] == "false_premise"), q["id"]
