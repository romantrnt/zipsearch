"""Predictable normalization and token-aware scoring for human archive search."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_SPACE = re.compile(r"\s+")
_PHONE = re.compile(r"\D+")
_WORD_COMPONENT = re.compile(r"[^\W_]+", re.UNICODE)
_PHONE_COMPONENT = re.compile(r"\+?\d(?:[\d(). -]*\d)?")


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


@dataclass(frozen=True)
class SmartMatch:
    score: int
    match_type: str
    patterns: tuple[str, ...]
    text_spans: tuple[tuple[int, int], ...]
    query_spans: tuple[tuple[int, int], ...]


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


def _merge_spans(spans: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return tuple(merged)


def _components(value: str) -> tuple[tuple[str, int, int, bool], ...]:
    """Return word components plus phone-shaped components, preserving source offsets."""
    phones = [
        match for match in _PHONE_COMPONENT.finditer(value) if len(phone_digits(match.group())) >= 7
    ]
    result = [(match.group(), match.start(), match.end(), True) for match in phones]
    result.extend(
        (match.group(), match.start(), match.end(), False)
        for match in _WORD_COMPONENT.finditer(value)
        if not any(phone.start() <= match.start() < phone.end() for phone in phones)
    )
    return tuple(sorted(result, key=lambda item: item[1]))


def _word_spans(
    text: str, wanted: set[str], *, case_sensitive: bool, prefixes: bool = False
) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    for match in _WORD_COMPONENT.finditer(text):
        token = normalize(match.group(), case_sensitive=case_sensitive)
        if token in wanted or (prefixes and any(token.startswith(value) for value in wanted)):
            found.append((match.start(), match.end()))
    return found


def match(text: str, patterns: tuple[SmartPattern, ...]) -> SmartMatch | None:
    """Rank exact phrases, token records, prefixes, then useful broad token hits.

    It deliberately performs no edit-distance matching. A multi-token query is a
    ranked union: all-token records lead, while one-token records stay available.
    """
    case_sensitive = patterns[0].case_sensitive if patterns else False
    normalized = normalize(text, case_sensitive=case_sensitive)
    record_tokens = tokens(text, case_sensitive=case_sensitive)
    record_set = set(record_tokens)
    text_phone = phone_digits(text)
    best: SmartMatch | None = None
    for pattern in patterns:
        full = sum(token in record_set for token in pattern.tokens)
        prefix = sum(
            token not in record_set and any(candidate.startswith(token) for candidate in record_set)
            for token in pattern.tokens
            if len(token) >= 2
        )
        if pattern.phone and len(pattern.phone) >= 7 and pattern.phone in text_phone:
            type_, value = "phone", 520
        elif pattern.normalized in normalized:
            type_, value = "phrase", 500 + min(len(pattern.tokens), 9)
        elif pattern.tokens and full == len(pattern.tokens):
            type_, value = "all_tokens", 400 + full
        elif pattern.tokens and full + prefix == len(pattern.tokens) and full:
            type_, value = "prefix_tokens", 300 + full * 10 + prefix
        elif full:
            type_, value = "partial_tokens", 100 + full * 10
        elif prefix:
            type_, value = "prefix_partial", 70 + prefix
        else:
            continue
        components = _components(pattern.source)
        word_components = [component for component in components if not component[3]]
        matched_words: set[str] = set()
        for component, *_ in word_components:
            word = normalize(component, case_sensitive=pattern.case_sensitive)
            if word in record_set or (
                len(word) >= 2 and any(candidate.startswith(word) for candidate in record_set)
            ):
                matched_words.add(word)
        if type_ == "phrase":
            matched_words = {
                normalize(component[0], case_sensitive=pattern.case_sensitive)
                for component in word_components
            }
        query_spans = [
            (start, end)
            for value_, start, end, is_phone in components
            if (is_phone and type_ == "phone")
            or (
                not is_phone
                and normalize(value_, case_sensitive=pattern.case_sensitive) in matched_words
            )
        ]
        text_spans = _word_spans(
            text,
            matched_words,
            case_sensitive=pattern.case_sensitive,
            prefixes=True,
        )
        if type_ == "phone":
            # Preserve separators in the record; its digit range is the evidence.
            digits = [(index, char) for index, char in enumerate(text) if char.isdigit()]
            digit_string = "".join(char for _, char in digits)
            comparable = (
                "7" + digit_string[1:]
                if len(digit_string) == 11 and digit_string.startswith("8")
                else digit_string
            )
            start = comparable.find(pattern.phone)
            text_spans += (
                [(digits[start][0], digits[start + len(pattern.phone) - 1][0] + 1)]
                if start >= 0
                else []
            )
        candidate = SmartMatch(
            value, type_, (pattern.source,), _merge_spans(text_spans), _merge_spans(query_spans)
        )
        if best is None or candidate.score > best.score:
            best = candidate
    return best


def score(text: str, patterns: tuple[SmartPattern, ...]) -> tuple[int, str, tuple[str, ...]] | None:
    """Backward-compatible score-only view of :func:`match`."""
    evidence = match(text, patterns)
    return (evidence.score, evidence.match_type, evidence.patterns) if evidence else None
