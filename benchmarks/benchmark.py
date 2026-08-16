"""Small reproducible smoke benchmark; not a performance claim or test gate."""

from __future__ import annotations

import tempfile
import time
import zipfile
from collections import defaultdict
from pathlib import Path

from zipsearch.engine import discover_archives, scan_archives, scan_member_locator
from zipsearch.index import build_or_update, default_index_path
from zipsearch.models import SafetyLimits, SearchOptions


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="zipsearch-bench-") as directory:
        root = Path(directory)
        payload = "".join(f"ordinary record {number:08x}\n" for number in range(1_000))
        payload += "benchmark-needle\n"
        for number in range(64):
            with zipfile.ZipFile(root / f"{number:03}.zip", "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("records.txt", payload)
        source_compressed = sum(path.stat().st_size for path in root.glob("*.zip"))
        source_expanded = len(payload.encode()) * 64
        started = time.perf_counter()
        scans = list(scan_archives(discover_archives(root), SearchOptions(("benchmark-needle",))))
        direct_elapsed = time.perf_counter() - started
        direct_matches = sum(len(scan.matches) for scan in scans)
        started = time.perf_counter()
        build_or_update(root, default_index_path(root), SafetyLimits(), rebuild=True)
        build_elapsed = time.perf_counter() - started
        # Exercise the public CLI-equivalent indexed path through its candidate index.
        from zipsearch.index import candidates

        started = time.perf_counter()
        candidate_map, _ = candidates(
            root, default_index_path(root), ("benchmark-needle",), smart=False, case_sensitive=False
        )
        indexed_matches = 0
        members_decompressed = 0
        for archive, locations in candidate_map.items():
            routes: dict[tuple[int, ...], set[tuple[str, tuple[str, ...], int]]] = defaultdict(set)
            for member, nested, route, line in locations:
                routes[route].add((member, nested, line))
            for route, wanted in routes.items():
                scan = scan_member_locator(archive, route, SearchOptions(("benchmark-needle",)))
                members_decompressed += scan.members_scanned
                indexed_matches += sum(
                    (match.member, match.nested_path, match.line) in wanted
                    for match in scan.matches
                )
        indexed_elapsed = time.perf_counter() - started
        index_bytes = default_index_path(root).stat().st_size
        print(
            f"archives={len(scans)} source_compressed={source_compressed} "
            f"source_expanded={source_expanded} index_bytes={index_bytes} "
            f"direct_seconds={direct_elapsed:.3f} build_seconds={build_elapsed:.3f} "
            f"indexed_seconds={indexed_elapsed:.3f} direct_matches={direct_matches} "
            f"indexed_matches={indexed_matches} members_decompressed={members_decompressed} "
            f"parity={direct_matches == indexed_matches}"
        )


if __name__ == "__main__":
    main()
