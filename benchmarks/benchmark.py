"""Small reproducible smoke benchmark; not a performance claim or test gate."""

from __future__ import annotations

import tempfile
import time
import zipfile
from pathlib import Path

from zipsearch.engine import discover_archives, scan_archives
from zipsearch.models import SearchOptions


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="zipsearch-bench-") as directory:
        root = Path(directory)
        payload = "".join(f"ordinary record {number:08x}\n" for number in range(1_000))
        payload += "benchmark-needle\n"
        for number in range(64):
            with zipfile.ZipFile(root / f"{number:03}.zip", "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("records.txt", payload)
        started = time.perf_counter()
        scans = list(scan_archives(discover_archives(root), SearchOptions(("benchmark-needle",))))
        elapsed = time.perf_counter() - started
        matches = sum(len(scan.matches) for scan in scans)
        print(f"archives={len(scans)} matches={matches} seconds={elapsed:.3f}")


if __name__ == "__main__":
    main()
