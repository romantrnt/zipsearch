"""Exact, on-demand result grouping; it never changes the raw search set."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from .entities import extract
from .models import Match
from .smart import _PHONE_COMPONENT, phone_digits


@dataclass(frozen=True)
class MatchGroup:
    mode: str
    key: str
    occurrences: tuple[Match, ...]


def _key(match: Match, mode: str) -> str:
    if mode == "exact":
        return match.text
    if mode == "phone":
        return next(
            (
                phone_digits(found.group())
                for found in _PHONE_COMPONENT.finditer(match.text)
                if len(phone_digits(found.group())) >= 7
            ),
            match.text,
        )
    entities = extract(match.text)
    if mode == "email":
        return next((value for kind, value in entities if kind == "email"), match.text)
    return entities[0][1] if entities else match.text


def group(matches: list[Match], mode: str) -> tuple[MatchGroup, ...]:
    """Group only equal deterministic values, retaining every occurrence."""
    if mode not in {"exact", "phone", "email", "entity"}:
        raise ValueError(f"unknown grouping mode: {mode}")
    buckets: OrderedDict[str, list[Match]] = OrderedDict()
    for match in matches:
        buckets.setdefault(_key(match, mode), []).append(match)
    return tuple(MatchGroup(mode, key, tuple(items)) for key, items in buckets.items())
