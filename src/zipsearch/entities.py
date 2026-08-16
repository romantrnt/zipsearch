"""Deterministic local entities suitable for navigation, never enrichment."""

from __future__ import annotations

import ipaddress
import re
import uuid
from urllib.parse import urlparse

_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_IP = re.compile(r"(?<![\w:.])(?:[0-9A-Fa-f:.]{3,})(?![\w:.])")
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.I,
)
_HASH = re.compile(r"\b[0-9a-f]{32}(?:[0-9a-f]{8})?(?:[0-9a-f]{24})?\b", re.I)


def extract(text: str) -> tuple[tuple[str, str], ...]:
    """Return stable ``(kind, normalized value)`` pairs in source order."""
    found: list[tuple[int, str, str]] = []
    for match in _EMAIL.finditer(text):
        value = match.group().casefold()
        found.extend(
            ((match.start(), "email", value), (match.start(), "domain", value.rsplit("@", 1)[1]))
        )
    for match in _URL.finditer(text):
        value = match.group().rstrip(".,;:!?)]")
        parsed = urlparse(value)
        found.append((match.start(), "url", value))
        if parsed.hostname:
            found.append((match.start(), "domain", parsed.hostname.casefold()))
    for match in _IP.finditer(text):
        try:
            value = str(ipaddress.ip_address(match.group()))
        except ValueError:
            continue
        found.append((match.start(), "ipv6" if ":" in value else "ipv4", value))
    for match in _UUID.finditer(text):
        try:
            found.append((match.start(), "uuid", str(uuid.UUID(match.group()))))
        except ValueError:
            continue
    for match in _HASH.finditer(text):
        value = match.group().casefold()
        kind = {32: "md5", 40: "sha1", 64: "sha256"}.get(len(value))
        if kind:
            found.append((match.start(), kind, value))
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for _, kind, value in sorted(found, key=lambda item: item[0]):
        item = kind, value
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)
