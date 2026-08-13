"""Predictable normalization and token-aware scoring for human archive search."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_SPACE = re.compile(r"\s+")
_PHONE = re.compile(r"\D+")


def normalize(value: str, *, case_sensitive: bool = False) -> str:
    """Fold Unicode, case, ё/е, punctuation, and whitespace without fuzzy guesses."""
    value = unicodedata.normalize("NFKC", value)
    if not case_sensitive:
        value = value.casefold().replace("ё", "е")
    value = "".join(char if (char.isalnum() or char.isspace()) else " " for char in value)
    return _SPACE.sub(" ", value).strip()


def tokens(value: str, *, case_sensitive: bool = False) -> tuple[str, ...]:
    return tuple(_TOKEN.findall(normalize(value, case_sensitive=case_sensitive)))


def phone_digits(value: str) -> str:
    digits = _PHONE.sub("", value)
    return "7" + digits[1:] if len(digits) == 11 and digits.startswith("8") else digits


@dataclass(frozen=True)
class SmartPattern:
    source: str
    normalized: str
    tokens: tuple[str, ...]
    phone: str
    case_sensitive: bool


def compile_patterns(
    patterns: tuple[str, ...], *, case_sensitive: bool = False
) -> tuple[SmartPattern, ...]:
    compiled = tuple(
        SmartPattern(
            pattern,
            normalize(pattern, case_sensitive=case_sensitive),
            tokens(pattern, case_sensitive=case_sensitive),
            phone_digits(pattern),
            case_sensitive,
        )
        for pattern in patterns
    )
    if not compiled or any(not item.normalized for item in compiled):
        raise ValueError("smart-search patterns cannot be empty")
    return compiled


def score(text: str, patterns: tuple[SmartPattern, ...]) -> tuple[int, str, tuple[str, ...]] | None:
    """Rank exact phrases, token records, prefixes, then useful broad token hits.

    It deliberately performs no edit-distance matching. A multi-token query is a
    ranked union: all-token records lead, while one-token records stay available.
    """
    case_sensitive = patterns[0].case_sensitive if patterns else False
    normalized = normalize(text, case_sensitive=case_sensitive)
    record_tokens = tokens(text, case_sensitive=case_sensitive)
    record_set = set(record_tokens)
    text_phone = phone_digits(text)
    best: tuple[int, str, tuple[str, ...]] | None = None
    for pattern in patterns:
        full = sum(token in record_set for token in pattern.tokens)
        prefix = sum(
            token not in record_set and any(candidate.startswith(token) for candidate in record_set)
            for token in pattern.tokens
            if len(token) >= 2
        )
        if pattern.phone and len(pattern.phone) >= 7 and pattern.phone in text_phone:
            candidate = (520, "phone", (pattern.source,))
        elif pattern.normalized in normalized:
            candidate = (500 + min(len(pattern.tokens), 9), "phrase", (pattern.source,))
        elif pattern.tokens and full == len(pattern.tokens):
            candidate = (400 + full, "all_tokens", (pattern.source,))
        elif pattern.tokens and full + prefix == len(pattern.tokens) and full:
            candidate = (300 + full * 10 + prefix, "prefix_tokens", (pattern.source,))
        elif full:
            candidate = (100 + full * 10, "partial_tokens", (pattern.source,))
        elif prefix:
            candidate = (70 + prefix, "prefix_partial", (pattern.source,))
        else:
            continue
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best
