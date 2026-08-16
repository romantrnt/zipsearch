"""Measure direct scans against locator-verified indexed searches.

This is deliberately a generated workload, not a product-performance claim.
It includes rare, medium, common, phone, and multi-component SMART queries.
"""

from __future__ import annotations

import json
import tempfile
import time
import zipfile
from collections import defaultdict
from pathlib import Path

from zipsearch.engine import rank_matches, scan_archives, scan_member_locator
from zipsearch.index import build_or_update, candidates, default_index_path
from zipsearch.models import Match, SafetyLimits, SearchOptions

ARCHIVES = 48
MEMBERS_PER_ARCHIVE = 5
LINES_PER_MEMBER = 160


def _write_corpus(root: Path) -> tuple[int, int]:
    expanded = 0
    for archive_number in range(ARCHIVES):
        with zipfile.ZipFile(
            root / f"{archive_number:03}.zip", "w", zipfile.ZIP_DEFLATED
        ) as archive:
            for member_number in range(MEMBERS_PER_ARCHIVE):
                lines = [
                    f"ordinary record archive={archive_number} member={member_number} "
                    f"row={row} Иван\n"
                    for row in range(LINES_PER_MEMBER)
                ]
                if member_number == 0 and archive_number == 0:
                    lines.append("rare-needle unique value\n")
                if member_number == 1 and archive_number % 10 == 0:
                    lines.append("medium-needle sampled value\n")
                if member_number == 2:
                    lines.append("common-needle everywhere\n")
                if member_number == 3 and archive_number in {7, 41}:
                    lines.append("phone +7 (999) 555-01-23\n")
                archive.writestr(f"records/{member_number}.txt", "".join(lines))
                expanded += sum(len(line.encode()) for line in lines)
    return sum(path.stat().st_size for path in root.glob("*.zip")), expanded


def _key(match: Match) -> tuple[object, ...]:
    return match.archive, match.member, match.nested_path, match.line, match.text


def _direct(root: Path, options: SearchOptions) -> list[Match]:
    matches = [
        match
        for scan in scan_archives(iter(sorted(root.glob("*.zip"))), options)
        for match in scan.matches
    ]
    return rank_matches(iter(matches), options.max_matches) if options.smart else matches


def _verify(root: Path, options: SearchOptions) -> tuple[list[Match], int, int, int]:
    located, _ = candidates(
        root,
        default_index_path(root),
        options.patterns,
        smart=options.smart,
        case_sensitive=options.case_sensitive,
    )
    results: list[Match] = []
    members = bytes_processed = 0
    for archive, locations in located.items():
        routes: dict[tuple[int, ...], set[tuple[str, tuple[str, ...], int]]] = defaultdict(set)
        for member, nested, route, line in locations:
            routes[route].add((member, nested, line))
        for route, wanted in routes.items():
            scan = scan_member_locator(archive, route, options)
            members += scan.members_scanned
            bytes_processed += scan.bytes_declared
            results.extend(
                match
                for match in scan.matches
                if (match.member, match.nested_path, match.line) in wanted
            )
    return (
        rank_matches(iter(results), options.max_matches) if options.smart else results,
        len(located),
        members,
        bytes_processed,
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="zipsearch-index-bench-") as directory:
        root = Path(directory)
        compressed, expanded = _write_corpus(root)
        started = time.perf_counter()
        build_or_update(root, default_index_path(root), SafetyLimits(), rebuild=True)
        build_seconds = time.perf_counter() - started
        index_bytes = default_index_path(root).stat().st_size
        scenarios = {
            "rare": SearchOptions(("rare-needle",), workers=4),
            "medium": SearchOptions(("medium-needle",), workers=4),
            "common": SearchOptions(("common-needle",), workers=4),
            "phone": SearchOptions(("+79995550123",), smart=True, workers=4),
            "smart_multi": SearchOptions(("Иван +79995550123",), smart=True, workers=4),
        }
        report: dict[str, object] = {
            "corpus": {
                "archives": ARCHIVES,
                "compressed_bytes": compressed,
                "expanded_bytes": expanded,
                "index_bytes": index_bytes,
                "index_to_compressed": round(index_bytes / compressed, 3),
                "index_to_expanded": round(index_bytes / expanded, 3),
                "build_seconds": round(build_seconds, 4),
            },
            "scenarios": {},
        }
        for name, options in scenarios.items():
            started = time.perf_counter()
            direct = _direct(root, options)
            direct_seconds = time.perf_counter() - started
            started = time.perf_counter()
            candidates(
                root,
                default_index_path(root),
                options.patterns,
                smart=options.smart,
                case_sensitive=options.case_sensitive,
            )
            lookup_seconds = time.perf_counter() - started
            started = time.perf_counter()
            indexed, archives_opened, members, verification_bytes = _verify(root, options)
            verification_seconds = time.perf_counter() - started
            report["scenarios"][name] = {
                "direct_seconds": round(direct_seconds, 4),
                "candidate_lookup_seconds": round(lookup_seconds, 4),
                "verification_seconds": round(verification_seconds, 4),
                "indexed_total_seconds": round(lookup_seconds + verification_seconds, 4),
                "archives_opened": archives_opened,
                "members_decompressed": members,
                "verification_expanded_bytes": verification_bytes,
                "direct_results": len(direct),
                "indexed_results": len(indexed),
                "parity": sorted(_key(item) for item in direct)
                == sorted(_key(item) for item in indexed),
            }
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
