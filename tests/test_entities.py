from __future__ import annotations

from zipsearch.entities import extract


def test_deterministic_local_entity_extraction() -> None:
    text = (
        "mail User@Example.Invalid https://example.invalid/path 192.0.2.4 "
        "2001:db8::1 550e8400-e29b-41d4-a716-446655440000 "
        "d41d8cd98f00b204e9800998ecf8427e"
    )
    assert extract(text) == (
        ("email", "user@example.invalid"),
        ("domain", "example.invalid"),
        ("url", "https://example.invalid/path"),
        ("ipv4", "192.0.2.4"),
        ("ipv6", "2001:db8::1"),
        ("uuid", "550e8400-e29b-41d4-a716-446655440000"),
        ("md5", "d41d8cd98f00b204e9800998ecf8427e"),
    )
