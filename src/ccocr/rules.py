"""Text split rules ("logical expressions") evaluated on a page's OCR text.

A rule is a small boolean expression. It never splits on its own: a match
creates a split *proposal* that an operator confirms (spec 3.2).

Grammar (case-insensitive keywords)::

    expr    := or
    or      := and ("OR" and)*
    and     := not ("AND" not)*
    not     := "NOT" not | atom
    atom    := "(" expr ")" | test | flag
    test    := field ("contains" STRING | "matches" REGEX | "startswith" STRING)
             | "page" CMP NUMBER
             | "words" CMP NUMBER
             | "confidence" CMP NUMBER
    flag    := "prev_blank" | "first_page"
    field   := "text" | "top" | "bottom"
    CMP     := "==" | "!=" | "<" | "<=" | ">" | ">="
    STRING  := '"…"' | "'…'"
    REGEX   := /…/flags      (flags: i, m, s)

Examples::

    top matches /(RAČUN|INVOICE)\\s+(št\\.|no\\.)/i AND NOT text contains "nadaljevanje"
    text contains "Pogodba" AND page > 1
    prev_blank AND words > 20

``text`` is the whole page, ``top`` the upper zone and ``bottom`` the lower
zone of the page (zone size set per profile). ``contains`` and
``startswith`` are case-insensitive and diacritic-sensitive. The parser is a
hand-written recursive-descent parser: no ``eval`` is ever used.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

__all__ = ["PageContext", "Rule", "RuleError", "compile_rule"]


class RuleError(ValueError):
    """Raised when a rule expression cannot be parsed."""


@dataclass
class PageContext:
    """What a rule can see about one page."""

    text: str
    top: str
    bottom: str
    page: int  # 1-based position in the batch
    words: int
    confidence: float  # 0..100, mean word confidence
    prev_blank: bool = False
    first_page: bool = False


Predicate = Callable[[PageContext], bool]


@dataclass
class Rule:
    name: str
    expression: str
    predicate: Predicate = field(repr=False)
    # Metadata given to a document that starts on a matching page, e.g. {"doctype": "invoice"}.
    metadata: dict[str, str] = field(default_factory=dict)

    def matches(self, ctx: PageContext) -> bool:
        return bool(self.predicate(ctx))


_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<cmp>==|!=|<=|>=|<|>)
  | (?P<number>-?\d+(?:\.\d+)?)
  | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<regex>/(?:[^/\\]|\\.)+/[a-zA-Z]*)
  | (?P<word>[A-Za-z_][A-Za-z_]*)
    """,
    re.VERBOSE,
)

_FIELDS = {"text", "top", "bottom"}
_NUMERIC = {"page", "words", "confidence"}
_FLAGS = {"prev_blank", "first_page"}


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def _tokenize(src: str) -> list[tuple[str, str]]:
    pos = 0
    out: list[tuple[str, str]] = []
    while pos < len(src):
        m = _TOKEN_RE.match(src, pos)
        if not m:
            raise RuleError(f"unexpected character at position {pos}: {src[pos:pos + 15]!r}")
        kind = m.lastgroup
        assert kind is not None
        if kind != "ws":
            out.append((kind, m.group()))
        pos = m.end()
    return out


def _unquote(s: str) -> str:
    body = s[1:-1]
    return re.sub(r"\\(.)", r"\1", body)


def _regex(tok: str) -> re.Pattern[str]:
    end = tok.rfind("/")
    pattern, flags = tok[1:end], tok[end + 1:]
    f = 0
    for ch in flags.lower():
        if ch == "i":
            f |= re.IGNORECASE
        elif ch == "m":
            f |= re.MULTILINE
        elif ch == "s":
            f |= re.DOTALL
        else:
            raise RuleError(f"unknown regex flag {ch!r}")
    try:
        return re.compile(unicodedata.normalize("NFC", pattern.replace("\\/", "/")), f)
    except re.error as e:
        raise RuleError(f"invalid regular expression {pattern!r}: {e}") from e


_CMP: dict[str, Callable[[float, float], bool]] = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.toks = tokens
        self.i = 0

    def peek(self) -> tuple[str, str] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise RuleError("unexpected end of expression")
        self.i += 1
        return tok

    def kw(self, word: str) -> bool:
        tok = self.peek()
        if tok and tok[0] == "word" and tok[1].lower() == word:
            self.i += 1
            return True
        return False

    def parse(self) -> Predicate:
        p = self.or_()
        if self.peek() is not None:
            raise RuleError(f"unexpected token {self.peek()[1]!r}")  # type: ignore[index]
        return p

    def or_(self) -> Predicate:
        parts = [self.and_()]
        while self.kw("or"):
            parts.append(self.and_())
        if len(parts) == 1:
            return parts[0]
        return lambda c, ps=tuple(parts): any(p(c) for p in ps)

    def and_(self) -> Predicate:
        parts = [self.not_()]
        while self.kw("and"):
            parts.append(self.not_())
        if len(parts) == 1:
            return parts[0]
        return lambda c, ps=tuple(parts): all(p(c) for p in ps)

    def not_(self) -> Predicate:
        if self.kw("not"):
            inner = self.not_()
            return lambda c: not inner(c)
        return self.atom()

    def atom(self) -> Predicate:
        kind, val = self.take()
        if kind == "lparen":
            inner = self.or_()
            k, _ = self.take()
            if k != "rparen":
                raise RuleError("missing ')'")
            return inner
        if kind != "word":
            raise RuleError(f"expected a field or flag, got {val!r}")
        name = val.lower()
        if name in _FLAGS:
            return lambda c, n=name: bool(getattr(c, n))
        if name in _NUMERIC:
            k, op = self.take()
            if k != "cmp":
                raise RuleError(f"expected comparison after {name!r}")
            k, num = self.take()
            if k != "number":
                raise RuleError(f"expected a number after {name} {op}")
            fn, n = _CMP[op], float(num)
            return lambda c, nm=name: fn(float(getattr(c, nm)), n)
        if name in _FIELDS:
            k, op = self.take()
            op = op.lower()
            if k != "word" or op not in {"contains", "matches", "startswith"}:
                raise RuleError(f"expected contains/matches/startswith after {name!r}")
            k, arg = self.take()
            if op == "matches":
                if k != "regex":
                    raise RuleError("'matches' needs a /regex/")
                rx = _regex(arg)
                return lambda c, nm=name: rx.search(unicodedata.normalize("NFC", getattr(c, nm))) is not None
            if k != "string":
                raise RuleError(f"'{op}' needs a quoted string")
            needle = _norm(_unquote(arg))
            if op == "contains":
                return lambda c, nm=name: needle in _norm(getattr(c, nm))
            return lambda c, nm=name: _norm(getattr(c, nm)).lstrip().startswith(needle)
        raise RuleError(f"unknown name {val!r}")


def compile_rule(name: str, expression: str, metadata: dict[str, str] | None = None) -> Rule:
    """Parse ``expression`` and return a :class:`Rule`; raises :class:`RuleError`."""
    tokens = _tokenize(expression)
    if not tokens:
        raise RuleError("empty expression")
    return Rule(name=name, expression=expression, predicate=_Parser(tokens).parse(), metadata=dict(metadata or {}))
