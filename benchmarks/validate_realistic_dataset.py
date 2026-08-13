"""Generate the realistic dataset and validate it through the public CLI."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from generate_realistic_dataset import NEEDLE, ROOT, generate


def run(*arguments: str) -> tuple[int, list[dict[str, object]], float]:
    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-m", "zipsearch", "search", str(ROOT), *arguments, "--jsonl"],
        check=False,
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - started
    objects = [json.loads(line) for line in completed.stdout.splitlines()]
    return completed.returncode, [item for item in objects if item["type"] == "match"], elapsed


def main() -> None:
    statistics = generate()
    checks = {
        "literal_all_formats": (NEEDLE["email"], 9),
        "regex_contract": (r"KPD-2024-NEEDLE-[0-9]{3}", 9, "--regex"),
        "csv_only": (NEEDLE["email"], 2, "--extension", "csv", "--encoding", "cp1251"),
        "tsv_only": (NEEDLE["email"], 1, "--extension", "tsv"),
        "json_metadata": ("Департамент Бесполезной Аналитики", 1, "--extension", "json"),
        "jsonl_only": (NEEDLE["email"], 2, "--extension", "jsonl"),
        "xml_only": (NEEDLE["email"], 1, "--extension", "xml"),
        "sqlite_only": (NEEDLE["email"], 2, "--extension", "sqlite"),
        "xlsx_only": (NEEDLE["email"], 1, "--extension", "xlsx"),
        "nested_only": (NEEDLE["email"], 1, "--include", "вложения/*.jsonl"),
        "include_exports": (NEEDLE["email"], 2, "--include", "exports/*"),
        "exclude_exports": (NEEDLE["email"], 7, "--exclude", "exports/*"),
        "unicode_casefold": ("аркадий плющеватый-задорнов", 9),
        "multi_pattern": (NEEDLE["email"], 9, NEEDLE["contract"], "--workers", "4"),
        "absent": ("never-present-synthetic-value-0000", 0),
    }
    report: dict[str, object] = {"dataset": statistics, "checks": {}}
    for label, details in checks.items():
        pattern, expected, *flags = details
        status, matches, elapsed = run(pattern, *flags)
        actual = len(matches)
        if status != 0 or actual != expected:
            raise SystemExit(
                f"{label}: expected {expected} matches, got {actual}; stderr/status: {status}"
            )
        report["checks"][label] = {
            "expected": expected,
            "actual": actual,
            "seconds": round(elapsed, 4),
        }
    smart_status, smart_matches, smart_elapsed = run(
        "Глеб Скрепкин", "--smart", "--max-matches", "20", "--workers", "4"
    )
    if (
        smart_status != 0
        or len(smart_matches) != 20
        or any(int(match["score"]) < 400 for match in smart_matches[:5])
    ):
        raise SystemExit("smart_order: strong all-token matches were not retained at the top")
    report["checks"]["smart_order"] = {
        "expected": "20 top-ranked; first five score >= 400",
        "actual": len(smart_matches),
        "seconds": round(smart_elapsed, 4),
    }
    temp_artifacts = list(Path("/tmp").glob("zipsearch-*"))
    report["temporary_artifacts"] = [str(path) for path in temp_artifacts]
    if temp_artifacts:
        raise SystemExit("temporary ZipSearch artifacts remain")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
