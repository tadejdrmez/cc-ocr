from ccocr.rules import PageContext, compile_rule
from ccocr.separators import PageFacts, parse_payload, plan_split
from ccocr.sheets import payload


def page(i, text="", blank=False, sep=None):
    ctx = None if blank or sep else PageContext(text=text, top=text, bottom="", page=i + 1, words=30, confidence=90)
    return PageFacts(index=i, is_blank=blank, ink_ratio=0.0 if blank else 0.05, separator=sep, context=ctx)


CFG = {"proposals": ["qr-separator", "blank-page", "text-rule"], "auto_accept": True, "drop_blank_pages": True}


def test_payload_roundtrip():
    code = parse_payload(payload("invoice", "4.2.1", "inv"))
    assert code.valid and code.doctype == "INVOICE" and code.classification == "4.2.1" and code.fields["PROFILE"] == "inv"
    assert parse_payload("https://example.com") is None
    assert not parse_payload("CCOCR:9;SEP;DT=X").valid


def test_qr_blank_and_rule_splits():
    rule = compile_rule("inv", 'top contains "račun št"', {"doctype": "invoice"})
    sep = parse_payload(payload("contract"))
    facts = [
        page(0, "RAČUN št. 1"), page(1, "nadaljevanje"), page(2, "RAČUN št. 2"),
        page(3, blank=True), page(4, sep=sep), page(5, "Pogodba"), page(6, "člen 2"),
    ]
    plan = plan_split(facts, CFG, [rule])
    assert [d.pages for d in plan.documents] == [[0, 1], [2], [5, 6]]
    assert [d.metadata.get("doctype") for d in plan.documents] == ["invoice", "invoice", "contract"]
    assert {p["page"] for p in plan.removed_pages} == {4, 5}
    assert [p.source for p in plan.proposals] == ["text-rule", "blank-page", "qr-separator"]


def test_blank_pages_dropped_without_splitting_when_blank_proposals_off():
    cfg = {**CFG, "proposals": ["text-rule"]}
    plan = plan_split([page(0, "a"), page(1, blank=True), page(2, "b")], cfg, [])
    assert [d.pages for d in plan.documents] == [[0, 2]]


def test_invalid_or_unknown_separator_flags_review():
    plan = plan_split([page(0, sep=parse_payload("CCOCR:1;SEP;DT=ALIEN")), page(1, "x")], CFG, [], known_doctypes={"invoice"})
    assert plan.documents[0].review_flags and "unknown document type" in plan.documents[0].review_flags[0]


def test_fixed_count():
    cfg = {**CFG, "proposals": ["fixed-count"], "fixed_count": 2}
    plan = plan_split([page(i, "x") for i in range(5)], cfg, [])
    assert [d.pages for d in plan.documents] == [[0, 1], [2, 3], [4]]
