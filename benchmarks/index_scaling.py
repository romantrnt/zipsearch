"""Deterministic SQLite index scaling measurements; not a performance claim."""

from __future__ import annotations

import json
import tempfile
import time
import zipfile
from pathlib import Path

from zipsearch.execution import execute_plan, plan_search
from zipsearch.index import build_or_update, default_index_path
from zipsearch.models import SafetyLimits, SearchOptions


def build_corpus(root: Path, archives: int) -> tuple[int, int]:
    expanded = 0
    for number in range(archives):
        with zipfile.ZipFile(root / f"{number:04}.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for member in range(4):
                lines = [
                    f"record archive={number} member={member} row={row} ordinary-token\n"
                    for row in range(120)
                ]
                if number == archives // 2 and member == 0:
                    lines.append("rare-scale-needle +7 (999) 555-01-23\n")
                data = "".join(lines)
                expanded += len(data.encode())
                archive.writestr(f"records/{member}.txt", data)
    return sum(path.stat().st_size for path in root.glob("*.zip")), expanded


def measure(archives: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="zipsearch-scale-") as directory:
        root = Path(directory)
        compressed, expanded = build_corpus(root, archives)
        index = default_index_path(root)
        started = time.perf_counter()
        build_or_update(root, index, SafetyLimits(), rebuild=True)
        build_seconds = time.perf_counter() - started
        started = time.perf_counter()
        build_or_update(root, index, SafetyLimits())
        update_seconds = time.perf_counter() - started
        options = SearchOptions(("rare-scale-needle",))
        started = time.perf_counter()
        direct = list(execute_plan(root, options, plan_search(root, options, force_scan=True)))
        direct_seconds = time.perf_counter() - started
        started = time.perf_counter()
        indexed = list(execute_plan(root, options, plan_search(root, options)))
        indexed_seconds = time.perf_counter() - started
        index_bytes = index.stat().st_size
        return {
            "archives": archives,
            "source_compressed_bytes": compressed,
            "source_expanded_bytes": expanded,
            "index_bytes": index_bytes,
            "index_to_compressed": round(index_bytes / compressed, 3),
            "index_to_expanded": round(index_bytes / expanded, 3),
            "build_seconds": round(build_seconds, 4),
            "unchanged_update_seconds": round(update_seconds, 4),
            "direct_seconds": round(direct_seconds, 4),
            "indexed_seconds": round(indexed_seconds, 4),
            "parity": sum(len(item.matches) for item in direct)
            == sum(len(item.matches) for item in indexed),
        }


def main() -> None:
    print(json.dumps([measure(size) for size in (12, 36, 72)], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
