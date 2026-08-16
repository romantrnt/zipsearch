"""Larger deterministic heterogeneous corpus for index-size investigation."""

from __future__ import annotations

import argparse
import json
import random
import tempfile
import time
import zipfile
from pathlib import Path

from zipsearch.execution import execute_plan, plan_search
from zipsearch.index import build_or_update, default_index_path, storage_breakdown
from zipsearch.models import SafetyLimits, SearchOptions


def make_corpus(root: Path, archives: int = 30, records: int = 180) -> tuple[int, int]:
    rng = random.Random(6000)
    expanded = 0
    names = ["Глеб Скрепкин", "Иван Петров", "Alice Smith", "Мария Орлова"]
    for number in range(archives):
        rows = []
        for row in range(records):
            record = {
                "id": f"REC-{number:03}-{row:05}",
                "name": rng.choice(names),
                "email": f"user{number}_{row}@example.invalid",
                "phone": f"+7 (9{number % 10}{row % 10}) 555-{row % 100:02}-23",
                "city": rng.choice(["Москва", "Тверь", "Kazan", "Omsk"]),
                "status": rng.choice(["active", "pending", "archived"]),
            }
            rows.append(record)
        if number == archives // 2:
            rows[records // 2].update(
                name="Глеб Скрепкин",
                email="rare.person@example.invalid",
                phone="+7 (999) 555-01-23",
            )
        csv = "id,name,email,phone,city,status\n" + "".join(
            ",".join(item.values()) + "\n" for item in rows
        )
        jsonl = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows)
        notes = "\n".join(
            f"audit archive={number} row={row} common operational token"
            for row in range(records * 3)
        )
        with zipfile.ZipFile(
            root / f"corpus-{number:03}.zip", "w", zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("exports/people.csv", csv)
            archive.writestr("exports/people.jsonl", jsonl)
            archive.writestr("logs/audit.log", notes)
            archive.writestr(
                "docs/readme.md", "heterogeneous generated corpus\n" * (number % 7 + 1)
            )
        expanded += sum(len(value.encode()) for value in (csv, jsonl, notes))
    return sum(path.stat().st_size for path in root.glob("*.zip")), expanded


def measure(
    root: Path, options: SearchOptions, *, force_scan: bool = False
) -> tuple[float, float, int, str]:
    """Measure planning separately from execution.

    Candidate lookup is meaningful work for an indexed query, but keeping it
    separate makes small selective-query crossovers explainable instead of
    accidentally attributing SQLite startup and freshness checks to member
    verification.
    """
    started = time.perf_counter()
    plan = plan_search(root, options, force_scan=force_scan)
    planning = time.perf_counter() - started
    started = time.perf_counter()
    scans = list(execute_plan(root, options, plan))
    execution = time.perf_counter() - started
    return planning, execution, sum(len(scan.matches) for scan in scans), plan.execution


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archives", type=int, default=30)
    parser.add_argument("--records", type=int, default=180)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="zipsearch-realistic-index-") as directory:
        root = Path(directory)
        compressed, expanded = make_corpus(root, args.archives, args.records)
        index = default_index_path(root)
        started = time.perf_counter()
        build_or_update(root, index, SafetyLimits(), rebuild=True)
        build = time.perf_counter() - started
        started = time.perf_counter()
        build_or_update(root, index, SafetyLimits())
        update = time.perf_counter() - started
        queries = {
            "selective_phone": SearchOptions(("+79995550123",), smart=True),
            "selective_email": SearchOptions(("rare.person@example.invalid",)),
            "broad_smart": SearchOptions(("Глеб",), smart=True),
        }
        report: dict[str, object] = {
            "corpus": {
                "archives": args.archives,
                "records_per_archive": args.records,
                "compressed_bytes": compressed,
                "expanded_bytes": expanded,
                "index_bytes": index.stat().st_size,
                "index_to_compressed": round(index.stat().st_size / compressed, 3),
                "index_to_expanded": round(index.stat().st_size / expanded, 3),
                "build_seconds": round(build, 4),
                "unchanged_update_seconds": round(update, 4),
                "storage_breakdown": storage_breakdown(index),
            },
            "queries": {},
        }
        for name, options in queries.items():
            direct_plan, direct_execution, direct_count, _ = measure(
                root, options, force_scan=True
            )
            planned_plan, planned_execution, planned_count, execution = measure(
                root, options
            )
            report["queries"][name] = {
                "direct_planning_seconds": round(direct_plan, 4),
                "direct_execution_seconds": round(direct_execution, 4),
                "direct_total_seconds": round(direct_plan + direct_execution, 4),
                "planned_planning_seconds": round(planned_plan, 4),
                "planned_execution_seconds": round(planned_execution, 4),
                "planned_total_seconds": round(planned_plan + planned_execution, 4),
                "direct_results": direct_count,
                "planned_results": planned_count,
                "execution": execution,
            }
        # Measure the promised archive-local update separately from the
        # unchanged freshness pass. Appending a tiny member changes exactly one
        # outer ZIP while keeping the generated corpus representative.
        with zipfile.ZipFile(root / "corpus-000.zip", "a", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("updates/change.txt", "changed archive benchmark marker\n")
        started = time.perf_counter()
        build_or_update(root, index, SafetyLimits())
        report["corpus"]["changed_archive_update_seconds"] = round(
            time.perf_counter() - started, 4
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
