import re

from observability.monitoring import setup


def test_every_faithfulness_prompt_variable_is_mapped_exactly_once():
    used = set(re.findall(r"\{\{(\w+)\}\}", setup.FAITHFULNESS_SYSTEM + setup.FAITHFULNESS_USER))
    mapped = [m.variable for m in setup.faithfulness_mapping()]
    assert sorted(mapped) == sorted(used) == ["answer", "as_of", "documents", "facts", "question"]


def test_faithfulness_reads_the_document_excerpts_the_agent_records():
    documents = next(m for m in setup.faithfulness_mapping() if m.variable == "documents")
    assert (documents.source, documents.json_path) == ("metadata", "$.helix_documents")


def test_faithfulness_reads_the_facts_the_agent_records():
    facts = next(m for m in setup.faithfulness_mapping() if m.variable == "facts")
    assert (facts.source, facts.json_path) == ("metadata", "$.helix_facts")


def test_code_evaluator_defines_the_documented_entry_point():
    assert setup.HAS_SOURCES_LINE_TS.startswith("function evaluate({")
    assert 'name: "has_sources_line"' in setup.HAS_SOURCES_LINE_TS
