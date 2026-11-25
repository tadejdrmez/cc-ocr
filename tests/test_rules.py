import pytest

from ccocr.rules import PageContext, RuleError, compile_rule


def ctx(text="", top=None, page=2, words=50, conf=90.0, prev_blank=False):
    return PageContext(text=text, top=top if top is not None else text, bottom="", page=page, words=words,
                       confidence=conf, prev_blank=prev_blank, first_page=page == 1)


def test_contains_is_case_insensitive_and_keeps_diacritics():
    r = compile_rule("r", 'text contains "račun"')
    assert r.matches(ctx("RAČUN št. 5"))
    assert not r.matches(ctx("RACUN st. 5"))


def test_regex_and_boolean_logic():
    r = compile_rule("r", 'top matches /(RAČUN|INVOICE)\\s+(št|no)\\.?/i AND NOT top contains "nadaljevanje"')
    assert r.matches(ctx(top="Invoice No. 7"))
    assert not r.matches(ctx(top="Nadaljevanje: račun št. 7"))


def test_numeric_flags_and_parentheses():
    r = compile_rule("r", '(page > 1 AND words >= 20) OR prev_blank')
    assert r.matches(ctx(page=3, words=20))
    assert not r.matches(ctx(page=1, words=100))
    assert r.matches(ctx(page=1, words=0, prev_blank=True))


def test_startswith_and_cyrillic():
    r = compile_rule("r", "top startswith 'уговор'")
    assert r.matches(ctx(top="  УГОВОР О КУПОПРОДАЈИ"))


@pytest.mark.parametrize("bad", ["", "text", "text contains", "page > x", "foo contains 'a'", "text matches 'a'", "(text contains 'a'", "text contains 'a' b"])
def test_bad_rules_are_rejected(bad):
    with pytest.raises(RuleError):
        compile_rule("bad", bad)


def test_no_eval_possible():
    with pytest.raises(RuleError):
        compile_rule("x", "__import__('os')")
