from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from pathlib import Path
from threading import Event

import pytest

from zipsearch.cli import main
from zipsearch.engine import scan_member_locator
from zipsearch.index import (
    IndexError,
    build_or_update,
    compact,
    decode_posting,
    default_index_path,
    encode_posting,
    status,
)
from zipsearch.models import SafetyLimits, SearchOptions


def write_zip(path: Path, text: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("records.txt", text)


def match_objects(capsys) -> list[dict[str, object]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"match"' in line]


def test_compact_posting_envelope_is_deterministic_and_rejects_corruption() -> None:
    pairs = [(2, 10), (1, 2), (2, 3), (2, 10)]
    encoded = encode_posting(pairs)
    assert encoded == encode_posting(reversed(pairs))
    assert decode_posting(encoded) == ((1, 2), (2, 3), (2, 10))
    damaged = bytearray(encoded)
    damaged[-1] ^= 1
    with pytest.raises(IndexError):
        decode_posting(bytes(damaged))


def test_explicit_index_verify_detects_corrupt_compact_payload(tmp_path: Path) -> None:
    write_zip(tmp_path / "records.zip", "needle\n")
    index = default_index_path(tmp_path)
    build_or_update(tmp_path, index, SafetyLimits(), rebuild=True)
    with sqlite3.connect(index) as connection:
        term_id, payload = connection.execute(
            "SELECT id, single_payload FROM terms WHERE single_payload IS NOT NULL LIMIT 1"
        ).fetchone()
        connection.execute(
            "UPDATE terms SET single_payload=? WHERE id=?",
            (payload[:-1] + bytes([payload[-1] ^ 1]), term_id),
        )
    assert status(tmp_path, index).state == "READY"
    assert status(tmp_path, index, verify_integrity=True).state == "CORRUPT"


def test_index_build_indexed_search_and_direct_parity(tmp_path: Path, capsys) -> None:
    with zipfile.ZipFile(tmp_path / "records.zip", "w") as archive:
        archive.writestr("records.txt", "Скрепкин Глеб\nИгорь Скрепкин +7 (908) 756-23-42\n")
        archive.writestr("unrelated-a.txt", "ordinary\n")
        archive.writestr("unrelated-b.txt", "ordinary\n")
    assert main(["index", "build", str(tmp_path)]) == 0
    assert status(tmp_path, default_index_path(tmp_path)).usable
    assert main(["search", str(tmp_path), "Глеб Скрепкин +79087562342", "--smart", "--jsonl"]) == 0
    indexed = match_objects(capsys)
    assert all(item["execution_path"] == "index" for item in indexed)
    assert (
        main(
            [
                "search",
                str(tmp_path),
                "Глеб Скрепкин +79087562342",
                "--smart",
                "--no-index",
                "--jsonl",
            ]
        )
        == 0
    )
    direct = match_objects(capsys)
    assert [(item["member"], item["line"], item["text"]) for item in indexed] == [
        (item["member"], item["line"], item["text"]) for item in direct
    ]


def test_index_candidate_is_a_superset_for_literal_substrings(tmp_path: Path, capsys) -> None:
    """Dictionary token boundaries must not change Python literal semantics."""
    write_zip(tmp_path / "records.zip", "needles are still searchable\n")
    assert main(["index", "build", str(tmp_path)]) == 0
    assert main(["search", str(tmp_path), "needle", "--jsonl"]) == 0
    indexed = match_objects(capsys)
    assert main(["search", str(tmp_path), "needle", "--no-index", "--jsonl"]) == 0
    direct = match_objects(capsys)
    assert [item["text"] for item in indexed] == [item["text"] for item in direct]


def test_incremental_update_stale_removed_and_clean(tmp_path: Path, capsys) -> None:
    first = tmp_path / "first.zip"
    write_zip(first, "first needle\n")
    assert main(["index", "build", str(tmp_path)]) == 0
    second = tmp_path / "second.zip"
    write_zip(second, "second needle\n")
    assert status(tmp_path, default_index_path(tmp_path)).state == "STALE"
    assert main(["index", "update", str(tmp_path)]) == 0
    assert status(tmp_path, default_index_path(tmp_path)).archives == 2
    first.unlink()
    assert main(["index", "update", str(tmp_path)]) == 0
    assert status(tmp_path, default_index_path(tmp_path)).archives == 1
    assert main(["index", "clean", str(tmp_path)]) == 0
    assert not default_index_path(tmp_path).exists()
    assert main(["search", str(tmp_path), "needle", "--jsonl"]) == 0
    assert len(match_objects(capsys)) == 1


def test_index_compact_reclaims_incremental_free_pages(tmp_path: Path) -> None:
    write_zip(tmp_path / "records.zip", "needle\n" * 100)
    index = default_index_path(tmp_path)
    build_or_update(tmp_path, index, SafetyLimits(), rebuild=True)
    write_zip(tmp_path / "records.zip", "needle\n")
    build_or_update(tmp_path, index, SafetyLimits())
    before = index.stat().st_size
    compact(index)
    assert index.stat().st_size <= before
    assert status(tmp_path, index).usable


def test_corrupt_and_building_indexes_never_become_authoritative(tmp_path: Path, capsys) -> None:
    write_zip(tmp_path / "records.zip", "needle\n")
    index = default_index_path(tmp_path)
    index.write_bytes(b"not sqlite")
    assert status(tmp_path, index).state == "CORRUPT"
    assert main(["search", str(tmp_path), "needle", "--jsonl"]) == 0
    assert match_objects(capsys)[0]["execution_path"] == "scan"
    assert main(["index", "build", str(tmp_path)]) == 0
    with sqlite3.connect(index) as connection:
        connection.execute("UPDATE meta SET value='BUILDING' WHERE key='state'")
    assert not status(tmp_path, index).usable


def test_nested_records_are_indexed_and_verified_from_the_outer_archive(
    tmp_path: Path, capsys
) -> None:
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as archive:
        archive.writestr("inside.txt", "nested needle\n")
    with zipfile.ZipFile(tmp_path / "outer.zip", "w") as archive:
        archive.writestr("inner.zip", inner.getvalue())
        archive.writestr("unrelated-a.txt", "ordinary\n")
        archive.writestr("unrelated-b.txt", "ordinary\n")
    assert main(["index", "build", str(tmp_path)]) == 0
    assert main(["search", str(tmp_path), "needle", "--jsonl"]) == 0
    result = match_objects(capsys)[0]
    assert result["execution_path"] == "index"
    assert result["nested_path"] == ["inner.zip"]


def test_targeted_verification_opens_only_the_indexed_member(tmp_path: Path) -> None:
    archive = tmp_path / "members.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("target.txt", "needle\n")
        output.writestr("unrelated.txt", "needle\n")
    scan = scan_member_locator(archive, (0,), SearchOptions(("needle",)))
    assert scan.members_scanned == 1
    assert [match.member for match in scan.matches] == ["target.txt"]


def test_index_locator_distinguishes_duplicate_member_names(tmp_path: Path, capsys) -> None:
    archive = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("same.txt", "first needle\n")
        output.writestr("same.txt", "second needle\n")
    assert main(["index", "build", str(tmp_path)]) == 0
    assert main(["search", str(tmp_path), "needle", "--jsonl"]) == 0
    assert [item["text"] for item in match_objects(capsys)] == ["first needle", "second needle"]


def test_cancelled_build_remains_building_and_update_resumes(tmp_path: Path) -> None:
    write_zip(tmp_path / "records.zip", "needle\n")
    cancelled = Event()
    cancelled.set()
    index = default_index_path(tmp_path)
    build_or_update(tmp_path, index, SafetyLimits(), rebuild=True, cancelled=cancelled)
    assert status(tmp_path, index).state == "BUILDING"
    build_or_update(tmp_path, index, SafetyLimits())
    assert status(tmp_path, index).usable


def test_index_build_reports_archive_boundary_progress(tmp_path: Path) -> None:
    write_zip(tmp_path / "one.zip", "needle\n")
    write_zip(tmp_path / "two.zip", "needle\n")
    events: list[dict[str, int]] = []
    build_or_update(
        tmp_path,
        default_index_path(tmp_path),
        SafetyLimits(),
        rebuild=True,
        progress=events.append,
    )
    assert events[-1]["archives_completed"] == events[-1]["archives_total"] == 2
    assert events[-1]["records_indexed"] == 2


def test_inline_posting_promotes_and_survives_incremental_archive_replacement(
    tmp_path: Path, capsys
) -> None:
    write_zip(tmp_path / "one.zip", "shared compactneedle\n")
    build_or_update(tmp_path, default_index_path(tmp_path), SafetyLimits(), rebuild=True)
    write_zip(tmp_path / "two.zip", "shared compactneedle\n")
    build_or_update(tmp_path, default_index_path(tmp_path), SafetyLimits())
    assert main(["search", str(tmp_path), "compactneedle", "--jsonl"]) == 0
    assert len(match_objects(capsys)) == 2
    write_zip(tmp_path / "one.zip", "replacement compactneedle\n")
    build_or_update(tmp_path, default_index_path(tmp_path), SafetyLimits())
    assert main(["search", str(tmp_path), "compactneedle", "--jsonl"]) == 0
    assert {item["text"] for item in match_objects(capsys)} == {
        "replacement compactneedle",
        "shared compactneedle",
    }


def test_indexed_sqlite_rows_from_multiple_tables_keep_distinct_locators(
    tmp_path: Path, capsys
) -> None:
    database = tmp_path / "source.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE first_table (value TEXT)")
    connection.execute("CREATE TABLE second_table (value TEXT)")
    connection.execute("INSERT INTO first_table VALUES ('first needle')")
    connection.execute("INSERT INTO second_table VALUES ('second needle')")
    connection.commit()
    connection.close()
    with zipfile.ZipFile(tmp_path / "database.zip", "w") as archive:
        archive.writestr("data.sqlite", database.read_bytes())
        archive.writestr("unrelated-a.txt", "ordinary\n")
        archive.writestr("unrelated-b.txt", "ordinary\n")
    assert main(["index", "build", str(tmp_path)]) == 0
    assert main(["search", str(tmp_path), "needle", "--jsonl"]) == 0
    output = match_objects(capsys)
    assert [item["line"] for item in output] == [1, 2]
    assert all(item["execution_path"] == "index" for item in output)


def test_indexed_structured_provenance_matches_direct_scan(tmp_path: Path, capsys) -> None:
    with zipfile.ZipFile(tmp_path / "records.zip", "w") as archive:
        archive.writestr("records.csv", "name,email\nneedle,person@example.invalid\n")
        archive.writestr("other-a.txt", "ordinary\n")
        archive.writestr("other-b.txt", "ordinary\n")
    assert main(["index", "build", str(tmp_path)]) == 0
    assert main(["search", str(tmp_path), "needle", "--jsonl"]) == 0
    indexed = match_objects(capsys)
    assert main(["search", str(tmp_path), "needle", "--no-index", "--jsonl"]) == 0
    direct = match_objects(capsys)
    assert indexed[0]["provenance"] == direct[0]["provenance"]
