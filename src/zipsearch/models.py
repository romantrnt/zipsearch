from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SafetyLimits:
    """Limits applied before decompressing ZIP members."""

    max_members: int = 100_000
    max_member_bytes: int = 512 * 1024 * 1024
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_compression_ratio: float = 200.0
    max_nested_depth: int = 2


@dataclass(frozen=True)
class SearchOptions:
    patterns: tuple[str, ...]
    regex: bool = False
    smart: bool = False
    case_sensitive: bool = False
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    max_matches: int = 1_000
    context: int = 0
    encoding: str = "auto"
    workers: int = 4
    limits: SafetyLimits = field(default_factory=SafetyLimits)


@dataclass(frozen=True)
class Match:
    archive: str
    member: str
    line: int
    text: str
    patterns: tuple[str, ...]
    context_before: tuple[str, ...] = ()
    context_after: tuple[str, ...] = ()
    nested_path: tuple[str, ...] = ()
    score: int = 0
    match_type: str = "literal"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Issue:
    archive: str
    message: str
    member: str | None = None
    kind: str = "warning"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArchiveScan:
    archive: Path
    members_seen: int = 0
    members_scanned: int = 0
    bytes_declared: int = 0
    matches: list[Match] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


@dataclass
class Summary:
    archives_seen: int = 0
    archives_completed: int = 0
    members_seen: int = 0
    members_scanned: int = 0
    matches: int = 0
    issues: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)
