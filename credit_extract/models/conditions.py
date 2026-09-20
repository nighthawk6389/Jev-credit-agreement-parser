"""A restricted expression grammar for variant conditions.

Conditions are *parsed*, never ``eval``-ed. The grammar admits comparisons over
named state variables and boolean combinators, and nothing else -- no
attribute access, no calls, no indexing. An expression that cannot be parsed is
a finding, not an exception swallowed at resolution time.

    ipo_completed == true
    junior_secured_debt > 25_000_000
    leverage <= 4.50 and date >= 2021-03-31
    not (revolver_utilisation < 35%)
    facility in ["revolver", "delayed_draw"]

Evaluation is three-valued. A comparison over a variable the caller did not
supply returns ``None`` -- *undeterminable* -- rather than ``False``. The
distinction matters for the same reason ``absent_from_document`` matters:
"this condition does not hold" and "we cannot tell whether it holds" are
different answers, and collapsing them makes a springing covenant look
unconditionally inactive.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel

__all__ = [
    "ConditionSyntaxError", "Expr", "parse_condition", "evaluate",
    "referenced_variables",
]


class ConditionSyntaxError(ValueError):
    """The expression is not in the grammar."""


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<date>\d{4}-\d{2}-\d{2})
    | (?P<number>-?\d[\d_]*(?:\.\d+)?%?)
    | (?P<string>"[^"]*"|'[^']*')
    | (?P<op><=|>=|==|!=|<|>)
    | (?P<punct>[()\[\],])
    | (?P<word>[A-Za-z_][A-Za-z0-9_.]*)
    """,
    re.VERBOSE,
)

_KEYWORDS = {"and", "or", "not", "in", "true", "false", "null", "none"}


class _Token(BaseModel):
    kind: str
    text: str
    position: int


def _tokenize(source: str) -> list[_Token]:
    tokens: list[_Token] = []
    index = 0
    while index < len(source):
        match = _TOKEN_RE.match(source, index)
        if match is None:
            raise ConditionSyntaxError(
                f"unexpected character {source[index]!r} at position {index} "
                f"in {source!r}"
            )
        index = match.end()
        kind = match.lastgroup or ""
        if kind == "ws":
            continue
        text = match.group()
        if kind == "word" and text.lower() in _KEYWORDS:
            kind = text.lower()
        tokens.append(_Token(kind=kind, text=text, position=match.start()))
    return tokens


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------

Tri = bool | None


class Expr(BaseModel):
    """Base node. Evaluation is three-valued."""

    def evaluate(self, state: dict[str, Any]) -> Tri:  # pragma: no cover - abstract
        raise NotImplementedError

    def variables(self) -> set[str]:  # pragma: no cover - abstract
        raise NotImplementedError


class Literal_(Expr):
    kind: Literal["literal"] = "literal"
    value: Any = None

    def evaluate(self, state: dict[str, Any]) -> Tri:
        return bool(self.value) if isinstance(self.value, bool) else None

    def variables(self) -> set[str]:
        return set()


class Var(Expr):
    kind: Literal["var"] = "var"
    name: str

    def evaluate(self, state: dict[str, Any]) -> Tri:
        if self.name not in state:
            return None
        value = state[self.name]
        return bool(value)

    def variables(self) -> set[str]:
        return {self.name}


class Compare(Expr):
    kind: Literal["compare"] = "compare"
    name: str
    op: str
    rhs: Any = None

    def evaluate(self, state: dict[str, Any]) -> Tri:
        if self.name not in state:
            return None                      # undeterminable, not false
        left = state[self.name]
        if left is None:
            return None
        right = self.rhs
        if self.op == "in":
            if not isinstance(right, (list, tuple, set)):
                return None
            return any(_equal(left, item) for item in right)
        if self.op == "==":
            return _equal(left, right)
        if self.op == "!=":
            equal = _equal(left, right)
            return None if equal is None else not equal
        return _order(left, right, self.op)

    def variables(self) -> set[str]:
        return {self.name}


class Not(Expr):
    kind: Literal["not"] = "not"
    operand: Expr

    def evaluate(self, state: dict[str, Any]) -> Tri:
        inner = self.operand.evaluate(state)
        return None if inner is None else not inner

    def variables(self) -> set[str]:
        return self.operand.variables()


class BoolOp(Expr):
    kind: Literal["bool"] = "bool"
    op: Literal["and", "or"]
    operands: list[Expr]

    def evaluate(self, state: dict[str, Any]) -> Tri:
        results = [operand.evaluate(state) for operand in self.operands]
        if self.op == "and":
            # A single definite False settles it even if others are unknown.
            if any(r is False for r in results):
                return False
            return None if any(r is None for r in results) else True
        if any(r is True for r in results):
            return True
        return None if any(r is None for r in results) else False

    def variables(self) -> set[str]:
        return set().union(*(operand.variables() for operand in self.operands))


Literal_.model_rebuild()
Not.model_rebuild()
BoolOp.model_rebuild()


def _coerce_number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _equal(left: Any, right: Any) -> Tri:
    if isinstance(left, bool) or isinstance(right, bool):
        return bool(left) == bool(right)
    if isinstance(left, date) or isinstance(right, date):
        return _as_date(left) == _as_date(right)
    a, b = _coerce_number(left), _coerce_number(right)
    if a is not None and b is not None:
        return a == b
    return str(left).strip().casefold() == str(right).strip().casefold()


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _order(left: Any, right: Any, op: str) -> Tri:
    if isinstance(left, date) or isinstance(right, date):
        a_date, b_date = _as_date(left), _as_date(right)
        if a_date is None or b_date is None:
            return None
        return _apply_order(a_date, b_date, op)
    a, b = _coerce_number(left), _coerce_number(right)
    if a is None or b is None:
        return None
    return _apply_order(a, b, op)


def _apply_order(a: Any, b: Any, op: str) -> bool:
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    return a >= b


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class _Parser:
    def __init__(self, tokens: list[_Token], source: str) -> None:
        self.tokens = tokens
        self.source = source
        self.index = 0

    def _peek(self) -> _Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _next(self) -> _Token:
        token = self._peek()
        if token is None:
            raise ConditionSyntaxError(f"unexpected end of {self.source!r}")
        self.index += 1
        return token

    def _accept(self, kind: str) -> _Token | None:
        token = self._peek()
        if token is not None and token.kind == kind:
            self.index += 1
            return token
        return None

    def _expect(self, kind: str) -> _Token:
        token = self._accept(kind)
        if token is None:
            found = self._peek()
            raise ConditionSyntaxError(
                f"expected {kind} at position "
                f"{found.position if found else len(self.source)} in "
                f"{self.source!r}"
            )
        return token

    def parse(self) -> Expr:
        expr = self._or()
        if self.index != len(self.tokens):
            token = self.tokens[self.index]
            raise ConditionSyntaxError(
                f"unexpected {token.text!r} at position {token.position} in "
                f"{self.source!r}"
            )
        return expr

    def _or(self) -> Expr:
        operands = [self._and()]
        while self._accept("or"):
            operands.append(self._and())
        return operands[0] if len(operands) == 1 else BoolOp(op="or", operands=operands)

    def _and(self) -> Expr:
        operands = [self._unary()]
        while self._accept("and"):
            operands.append(self._unary())
        return operands[0] if len(operands) == 1 else BoolOp(op="and", operands=operands)

    def _unary(self) -> Expr:
        if self._accept("not"):
            return Not(operand=self._unary())
        return self._primary()

    def _primary(self) -> Expr:
        token = self._peek()
        if token is not None and token.kind == "punct" and token.text == "(":
            self._next()
            expr = self._or()
            closing = self._expect("punct")
            if closing.text != ")":
                raise ConditionSyntaxError(f"expected ')' in {self.source!r}")
            return expr
        if token is not None and token.kind in ("true", "false"):
            self._next()
            return Literal_(value=token.kind == "true")
        name_token = self._expect("word")
        operator = self._peek()
        if operator is not None and operator.kind in ("op", "in"):
            self._next()
            rhs = self._literal()
            return Compare(name=name_token.text, op=operator.text.lower(), rhs=rhs)
        return Var(name=name_token.text)

    def _literal(self) -> Any:
        token = self._peek()
        if token is None:
            raise ConditionSyntaxError(f"expected a value in {self.source!r}")
        if token.kind == "punct" and token.text == "[":
            self._next()
            items: list[Any] = []
            while True:
                closing = self._peek()
                if closing is not None and closing.text == "]":
                    self._next()
                    break
                items.append(self._literal())
                separator = self._peek()
                if separator is not None and separator.text == ",":
                    self._next()
            return items
        self._next()
        if token.kind == "number":
            text = token.text.replace("_", "")
            if text.endswith("%"):
                return Decimal(text[:-1])
            return Decimal(text)
        if token.kind == "string":
            return token.text[1:-1]
        if token.kind == "date":
            return date.fromisoformat(token.text)
        if token.kind in ("true", "false"):
            return token.kind == "true"
        if token.kind in ("null", "none"):
            return None
        if token.kind == "word":
            return token.text
        raise ConditionSyntaxError(
            f"unexpected {token.text!r} at position {token.position} in "
            f"{self.source!r}"
        )


def parse_condition(source: str) -> Expr:
    """Parse a condition expression. Raises :class:`ConditionSyntaxError`."""
    if not source or not source.strip():
        raise ConditionSyntaxError("empty condition")
    return _Parser(_tokenize(source), source).parse()


def evaluate(source: str, state: dict[str, Any]) -> Tri:
    """Parse and evaluate in one step. ``None`` means undeterminable."""
    return parse_condition(source).evaluate(state)


def referenced_variables(source: str) -> set[str]:
    """State variables an expression depends on, for reporting what is needed."""
    return parse_condition(source).variables()
