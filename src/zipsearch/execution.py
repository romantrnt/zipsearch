"""Shared, explainable direct-vs-index search execution planning."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, replace
from itertools import islice
from pathlib import Path
from threading import Event

from .engine import (
    _append_matches,
    _match_sort_key,
    discover_archives,
    scan_archives,
    scan_member_locator,
)
from .index import (
    IndexError,
    IndexStatus,
    candidates,
    default_index_path,
    fresh_indexed_paths,
    status,
)
from .models import ArchiveScan, SearchOptions

Locator = tuple[str, tuple[str, ...], tuple[int, ...], int]


@dataclass(frozen=True)
class SearchPlan:
    execution: str
    reason: str
    index: IndexStatus
    candidate_records: int = 0
    candidate_archives: int = 0
    candidate_members: int = 0
    planner_seed: str = "scan"
    locations: dict[Path, set[Locator]] | None = None
    scan_paths: tuple[Path, ...] = ()

    @property
    def indexed(self) -> bool:
        return self.execution in {"index", "hybrid"}


def plan_search(
    root: Path,
    options: SearchOptions,
    *,
    index_path: Path | None = None,
    recursive: bool = True,
    force_scan: bool = False,
) -> SearchPlan:
    """Choose a safe execution path from transparent coverage heuristics.

    Candidate retrieval is only an acceleration.  A planner never changes match
    semantics; it can only decline to use an otherwise valid index.
    """
    index_path = index_path or default_index_path(root)
    if force_scan:
        # A user explicitly choosing direct scan should not pay for SQLite
        # opening or central-directory freshness fingerprints merely to report
        # an index state they asked us not to use.
        return SearchPlan(
            "scan", "forced by --no-index", IndexStatus(index_path, "BYPASSED", False)
        )
    # ``status`` management commands run a full SQLite integrity pass.  Query
    # planning uses the faster guard (schema/state/source fingerprints); any
    # subsequent SQLite read failure degrades to direct scan below.
    current = status(root, index_path, verify_integrity=False)
    if not recursive:
        return SearchPlan("scan", "non-recursive search requires direct discovery", current)
    if options.regex:
        return SearchPlan("scan", "regex has no semantics-preserving index plan", current)
    if options.advanced_query:
        return SearchPlan("scan", "advanced query is source-verified", current)
    if options.case_sensitive:
        return SearchPlan("scan", "case-sensitive search requires source verification", current)
    if not current.usable and current.state not in {"STALE", "BUILDING"}:
        return SearchPlan("scan", f"index is {current.state.lower()}", current)
    try:
        locations, details = candidates(
            root,
            index_path,
            options.patterns,
            smart=options.smart,
            case_sensitive=options.case_sensitive,
        )
    except (OSError, sqlite3.Error, IndexError):
        corrupt = IndexStatus(index_path, "CORRUPT", False, bytes=current.bytes)
        return SearchPlan("scan", "index candidate lookup failed", corrupt)
    candidate_archives = len(locations)
    candidate_members = sum(
        len({route for _, _, route, _ in records}) for records in locations.values()
    )
    common = dict(
        candidate_records=int(details["candidate_records"]),
        candidate_archives=candidate_archives,
        candidate_members=candidate_members,
        planner_seed=str(details["planner_seed"]),
        locations=locations,
    )
    if not details.get("indexable", True):
        return SearchPlan("scan", "query has no safe indexable evidence", current, **common)
    try:
        covered = fresh_indexed_paths(root, index_path)
        all_paths = tuple(discover_archives(root, recursive=recursive))
        scan_paths = tuple(path for path in all_paths if path not in covered)
    except (OSError, sqlite3.Error, IndexError):
        return SearchPlan("scan", "index coverage lookup failed", current, **common)
    hybrid = bool(scan_paths)
    # Empty candidates are a conclusive fast negative for indexable evidence.
    if not candidate_members:
        return SearchPlan(
            "hybrid" if hybrid else "index",
            "indexed negative plus unindexed archive scan" if hybrid else "no candidate records",
            current,
            scan_paths=scan_paths,
            **common,
        )
    # The planner declines indexed verification only when *both* dimensions
    # approach full coverage. This preserves the useful all-archive / one-member
    # case while avoiding broad SMART work that would reopen every member anyway.
    archive_broad = candidate_archives * 100 >= max(1, current.archives) * 80
    member_broad = candidate_members * 100 >= max(1, current.members) * 80
    if archive_broad and member_broad:
        return SearchPlan("scan", "indexed candidate coverage is too broad", current, **common)
    return SearchPlan(
        "hybrid" if hybrid else "index",
        (
            "selective indexed candidates plus changed/new archive scan"
            if hybrid
            else "selective indexed candidates"
        ),
        current,
        scan_paths=scan_paths,
        **common,
    )


def execute_plan(
    root: Path,
    options: SearchOptions,
    plan: SearchPlan,
    *,
    recursive: bool = True,
    cancelled: Event | None = None,
) -> Iterator[ArchiveScan]:
    """Execute a plan while preserving source verification and bounded workers."""
    if not plan.indexed:
        yield from scan_archives(discover_archives(root, recursive=recursive), options, cancelled)
        return
    for archive in sorted(plan.locations or {}):
        by_route: dict[tuple[int, ...], set[tuple[str, tuple[str, ...], int]]] = {}
        for member, nested, route, line in (plan.locations or {})[archive]:
            by_route.setdefault(route, set()).add((member, nested, line))
        combined = ArchiveScan(archive)
        for route, records in sorted(by_route.items()):
            if cancelled is not None and cancelled.is_set():
                return
            targeted = scan_member_locator(archive, route, options, cancelled)
            combined.members_scanned += targeted.members_scanned
            combined.members_seen += targeted.members_scanned
            combined.bytes_declared += targeted.bytes_declared
            combined.issues.extend(targeted.issues)
            verified = (
                match
                for match in targeted.matches
                if (match.member, match.nested_path, match.line) in records
            )
            # A target route is only a slice of an archive.  The public cap is
            # per archive, exactly as in direct scanning, rather than once per
            # selected member.  SMART uses its established bounded ranking;
            # literal/regex preserve archive/member order up to the same cap.
            if options.smart:
                _append_matches(combined.matches, verified, options)
            else:
                remaining = max(0, options.max_matches - len(combined.matches))
                combined.matches.extend(islice(verified, remaining))
        if options.smart:
            combined.matches.sort(key=_match_sort_key)
            del combined.matches[options.max_matches :]
        if plan.execution == "hybrid":
            combined.matches = [
                replace(match, execution_path="index") for match in combined.matches
            ]
        yield combined
    if plan.execution == "hybrid" and plan.scan_paths:
        yield from scan_archives(iter(plan.scan_paths), options, cancelled)
