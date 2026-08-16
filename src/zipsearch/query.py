"""Small deterministic Boolean query language for explicit advanced searches."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .smart import normalize, phone_digits, tokens

_LEX = re.compile(r'\s*(?:("(?:[^"\\]|\\.)*")|([()]|\bAND\b|\bOR\b|\bNOT\b)|([^\s()]+))', re.I)
_FIELDS = frozenset({"archive", "member", "format", "table", "sheet", "fields", "phone", "email"})


class QuerySyntaxError(ValueError):
    pass


@dataclass(frozen=True)
class Term:
    value: str
    phrase: bool = False
    field: str | None = None


@dataclass(frozen=True)
class Not:
    child: object


@dataclass(frozen=True)
class Binary:
    operator: str
    left: object
    right: object


def _lex(source: str) -> list[str]:
    source = re.sub(r"([A-Za-z]+):(?=\")", r"\1: ", source)
    result: list[str] = []
    position = 0
    while position < len(source):
        match = _LEX.match(source, position)
        if not match:
            excerpt = source[position : position + 20]
            raise QuerySyntaxError(f"unexpected query syntax near {excerpt!r}")
        position = match.end()
        result.append(next(value for value in match.groups() if value is not None))
    return result


class _Parser:
    def __init__(self, source: str) -> None:
        self.items = _lex(source)
        self.position = 0

    def take(self) -> str | None:
        if self.position >= len(self.items):
            return None
        item = self.items[self.position]
        self.position += 1
        return item

    def parse(self) -> object:
        if not self.items:
            raise QuerySyntaxError("advanced query is empty")
        value = self.or_()
        if self.take() is not None:
            raise QuerySyntaxError("missing Boolean operator")
        return value

    def or_(self) -> object:
        value = self.and_()
        while self.position < len(self.items) and self.items[self.position].upper() == "OR":
            self.position += 1
            value = Binary("OR", value, self.and_())
        return value

    def and_(self) -> object:
        value = self.not_()
        while self.position < len(self.items):
            item = self.items[self.position].upper()
            if item == "AND":
                self.position += 1
                value = Binary("AND", value, self.not_())
            elif item not in {"OR", ")"}:
                value = Binary("AND", value, self.not_())
            else:
                break
        return value

    def not_(self) -> object:
        if self.position < len(self.items) and self.items[self.position].upper() == "NOT":
            self.position += 1
            return Not(self.not_())
        return self.atom()

    def atom(self) -> object:
        item = self.take()
        if item is None:
            raise QuerySyntaxError("expected a term")
        if item == "(":
            value = self.or_()
            if self.take() != ")":
                raise QuerySyntaxError("missing closing parenthesis")
            return value
        if item in {")", "AND", "OR", "NOT"}:
            raise QuerySyntaxError(f"expected a term, got {item}")
        field, separator, value = item.partition(":")
        if separator and field.casefold() in _FIELDS:
            if not value:
                value = self.take() or ""
                if not value or value in {"(", ")"}:
                    raise QuerySyntaxError(f"expected a value for {field}:")
            return Term(_unquote(value), value.startswith('"'), field.casefold())
        return Term(_unquote(item), item.startswith('"'))


def _unquote(value: str) -> str:
    if value.startswith('"'):
        if not value.endswith('"') or len(value) == 1:
            raise QuerySyntaxError("unterminated quoted phrase")
        return value[1:-1].replace(r"\"", '"').replace(r"\\", "\\")
    return value


def parse(source: str) -> object:
    return _Parser(source).parse()


def _contains(value: str, needle: str) -> bool:
    if needle.endswith("*"):
        prefix = normalize(needle[:-1])
        return bool(prefix) and any(token.startswith(prefix) for token in tokens(value))
    return normalize(needle) in normalize(value)


def evaluate(
    node: object, *, text: str, archive: str, member: str, provenance: dict[str, str]
) -> bool:
    if isinstance(node, Binary):
        left = evaluate(node.left, text=text, archive=archive, member=member, provenance=provenance)
        right = evaluate(
            node.right, text=text, archive=archive, member=member, provenance=provenance
        )
        return left and right if node.operator == "AND" else left or right
    if isinstance(node, Not):
        return not evaluate(
            node.child, text=text, archive=archive, member=member, provenance=provenance
        )
    assert isinstance(node, Term)
    if node.field == "archive":
        return _contains(archive, node.value)
    if node.field == "member":
        return _contains(member, node.value)
    if node.field == "format":
        return PurePosixPath(member).suffix.lstrip(".").casefold() == node.value.casefold()
    if node.field in {"table", "sheet", "fields"}:
        return _contains(provenance.get(node.field, ""), node.value)
    if node.field == "phone":
        value = phone_digits(node.value)
        return len(value) >= 7 and value in phone_digits(text)
    if node.field == "email":
        return node.value.casefold() in text.casefold()
    return _contains(text, node.value)


def positive_terms(node: object) -> tuple[str, ...]:
    if isinstance(node, Term):
        return () if node.field else (node.value,)
    if isinstance(node, Not):
        return ()
    assert isinstance(node, Binary)
    return positive_terms(node.left) + positive_terms(node.right)
