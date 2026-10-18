from observability.datasets import dataset_item, truth_items


def test_every_question_becomes_one_item_keyed_by_its_id():
    items = [dataset_item(q) for q in truth_items()]
    assert len(items) == 22
    assert len({item["id"] for item in items}) == 22
    q01 = next(item for item in items if item["id"] == "Q01")
    assert q01["input"] == {"question": q01["input"]["question"], "as_of": "2026-09-29"}
    assert set(q01["expected_output"]) == {"answer", "facts", "sources"}
    assert q01["metadata"]["category"] == "anchor" and q01["metadata"]["trap"]
