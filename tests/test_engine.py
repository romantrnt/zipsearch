from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path

from zipsearch.engine import discover_archives, scan_archive, scan_archives
from zipsearch.models import SafetyLimits, SearchOptions


def write_zip(
    path: Path, entries: dict[str, bytes | str], compression: int = zipfile.ZIP_DEFLATED
) -> None:
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)


def options(*patterns: str, **overrides: object) -> SearchOptions:
    values: dict[str, object] = {"patterns": patterns, "workers": 2}
    values.update(overrides)
    return SearchOptions(**values)  # type: ignore[arg-type]


def test_direct_text_search_multiple_patterns_context_and_unicode(tmp_path: Path) -> None:
    archive = tmp_path / "one.zip"
    write_zip(archive, {"данные/readme.txt": "before\nAlpha needle\nafter\nneedle beta\n"})
    scan = scan_archive(archive, options("needle", "beta", context=1))
    assert len(scan.matches) == 2
    assert scan.matches[0].member == "данные/readme.txt"
    assert scan.matches[0].context_before == ("before",)
    assert scan.matches[0].context_after == ("after",)
    assert scan.matches[1].patterns == ("needle", "beta")


def test_regex_filters_and_binary_skip(tmp_path: Path) -> None:
    archive = tmp_path / "filters.zip"
    write_zip(
        archive,
        {
            "keep/a.csv": "id,token\n1,INV-1234\n",
            "skip/b.txt": "INV-9999",
            "binary.bin": b"x\0INV-0000",
        },
    )
    scan = scan_archive(
        archive,
        options(r"INV-\d{4}", regex=True, include=("keep/**",), extensions=("csv",)),
    )
    assert [match.text for match in scan.matches] == ["1,INV-1234"]


def test_auto_encoding_reads_cp1251(tmp_path: Path) -> None:
    archive = tmp_path / "legacy.zip"
    write_zip(archive, {"данные.txt": "Контрольная игла\n".encode("cp1251")})
    assert len(scan_archive(archive, options("контрольная игла")).matches) == 1


def test_directory_discovery_and_bounded_concurrent_scan(tmp_path: Path) -> None:
    write_zip(tmp_path / "a.zip", {"a.txt": "needle"})
    nested = tmp_path / "child"
    nested.mkdir()
    write_zip(nested / "b.zip", {"b.txt": "needle"})
    paths = list(discover_archives(tmp_path))
    scans = list(scan_archives(iter(paths), options("needle", workers=2)))
    assert len(scans) == 2
    assert sum(len(scan.matches) for scan in scans) == 2
    assert list(discover_archives(tmp_path, recursive=False)) == [tmp_path / "a.zip"]


def test_bad_archive_and_duplicate_member_names_are_tolerated(tmp_path: Path) -> None:
    damaged = tmp_path / "bad.zip"
    damaged.write_bytes(b"not a zip")
    bad_scan = scan_archive(damaged, options("needle"))
    assert bad_scan.issues and bad_scan.issues[0].kind == "archive"
    duplicate = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr("same.txt", "first needle")
        archive.writestr("same.txt", "second needle")
    scan = scan_archive(duplicate, options("needle"))
    assert [match.text for match in scan.matches] == ["first needle", "second needle"]


def test_safety_limits_and_unsafe_paths(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    write_zip(archive, {"../escape.txt": "needle", "big.txt": "x" * 2000})
    limited = scan_archive(archive, options("needle", limits=SafetyLimits(max_member_bytes=100)))
    assert not limited.matches
    assert {issue.kind for issue in limited.issues} == {"safety"}
    assert any("unsafe" in issue.message for issue in limited.issues)


def test_nested_zip_and_depth_limit(tmp_path: Path) -> None:
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as archive:
        archive.writestr("inside.txt", "nested needle")
    outer = tmp_path / "outer.zip"
    write_zip(outer, {"inner.zip": inner.getvalue()})
    scan = scan_archive(outer, options("needle", limits=SafetyLimits(max_nested_depth=1)))
    assert len(scan.matches) == 1
    assert scan.matches[0].nested_path == ("inner.zip",)
    disabled = scan_archive(outer, options("needle", limits=SafetyLimits(max_nested_depth=0)))
    assert not disabled.matches
    assert any("depth" in issue.message for issue in disabled.issues)


def test_nested_zip_children_can_match_include_filter(tmp_path: Path) -> None:
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as archive:
        archive.writestr("data.jsonl", '{"value":"needle"}\n')
    outer = tmp_path / "outer.zip"
    write_zip(outer, {"container.zip": inner.getvalue()})
    scan = scan_archive(outer, options("needle", include=("*.jsonl",)))
    assert len(scan.matches) == 1


def test_sqlite_member_is_searched_and_temp_data_is_cleaned(tmp_path: Path) -> None:
    database = tmp_path / "source.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE users (email TEXT, state TEXT)")
    connection.execute("INSERT INTO users VALUES ('alice@example.com', 'active')")
    connection.commit()
    connection.close()
    archive = tmp_path / "database.zip"
    write_zip(archive, {"data.sqlite": database.read_bytes()})
    scan = scan_archive(archive, options("alice@example.com"))
    assert len(scan.matches) == 1
    assert "[users]" in scan.matches[0].text
    assert not list(tmp_path.glob("zipsearch-*"))


def test_xlsx_member_is_searched(tmp_path: Path) -> None:
    workbook = tmp_path / "book.xlsx"
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            "<worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'><sheetData>"
            "<row r='1'><c t='inlineStr'><is><t>needle value</t></is></c></row>"
            "</sheetData></worksheet>",
        )
    archive = tmp_path / "xlsx.zip"
    write_zip(archive, {"book.xlsx": workbook.read_bytes()})
    scan = scan_archive(archive, options("needle"))
    assert [match.text for match in scan.matches] == ["needle value"]


def test_compression_ratio_and_match_limit(tmp_path: Path) -> None:
    archive = tmp_path / "ratio.zip"
    write_zip(archive, {"compressible.txt": "needle\n" * 1000})
    rejected = scan_archive(
        archive, options("needle", limits=SafetyLimits(max_compression_ratio=1.1))
    )
    assert not rejected.matches
    assert any("ratio" in issue.message for issue in rejected.issues)
    accepted = scan_archive(
        archive, options("needle", max_matches=2, limits=SafetyLimits(max_compression_ratio=1000))
    )
    assert len(accepted.matches) == 2


def test_smart_search_normalizes_names_orders_prefixes_and_ranks(tmp_path: Path) -> None:
    archive = tmp_path / "smart.zip"
    write_zip(
        archive,
        {"people.txt": "Скрепкин Глеб\nГлеб Скрепкин\nСкрепкин Борис\nГлеб, другой человек\n"},
    )
    smart = scan_archive(archive, options("Глеб Скрепкин", smart=True, max_matches=10))
    assert [match.match_type for match in smart.matches[:2]] == ["phrase", "all_tokens"]
    assert smart.matches[0].text == "Глеб Скрепкин"
    assert smart.matches[0].query_spans == ((0, 4), (5, 13))
    assert smart.matches[0].text_spans == ((0, 4), (5, 13))
    assert {match.text for match in smart.matches} >= {"Скрепкин Борис", "Глеб, другой человек"}
    prefix = scan_archive(archive, options("Скрепкин Гле", smart=True, max_matches=10))
    assert prefix.matches[0].text == "Скрепкин Глеб"
    insensitive = scan_archive(archive, options("глеб скрепкин", smart=True, max_matches=10))
    assert insensitive.matches[0].text == "Глеб Скрепкин"
    sensitive = scan_archive(
        archive,
        options("глеб скрепкин", smart=True, case_sensitive=True, max_matches=10),
    )
    assert not sensitive.matches


def test_smart_result_cap_keeps_late_strong_match(tmp_path: Path) -> None:
    archive = tmp_path / "cap.zip"
    lines = [f"Скрепкин {number}" for number in range(200)] + ["Глеб Скрепкин"]
    write_zip(archive, {"people.txt": "\n".join(lines)})
    scan = scan_archive(archive, options("Скрепкин Глеб", smart=True, max_matches=3))
    assert scan.matches[0].text == "Глеб Скрепкин"
    assert len(scan.matches) == 3


def test_smart_match_evidence_attributes_partial_reordered_and_normalized_phone(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "evidence.zip"
    query = "Глеб Скрепкин +79087562342"
    write_zip(archive, {"people.txt": "Игорь Скрепкин; +7 (908) 756-23-42\nСкрепкин Глеб\n"})
    scan = scan_archive(archive, options(query, smart=True, max_matches=10))
    phone = next(item for item in scan.matches if item.text.startswith("Игорь"))
    # The unmatched name is absent, while both independently matching query
    # components remain attributable even though phone has the highest score.
    assert phone.query_spans == ((5, 13), (14, 26))
    assert phone.text_spans == ((6, 14), (17, 34))
    reordered = next(item for item in scan.matches if item.text == "Скрепкин Глеб")
    assert reordered.query_spans == ((0, 4), (5, 13))
    assert reordered.text_spans == ((0, 8), (9, 13))


def test_literal_and_regex_match_evidence_uses_actual_match_spans(tmp_path: Path) -> None:
    archive = tmp_path / "evidence-modes.zip"
    write_zip(archive, {"people.txt": "before NEEDLE after\n"})
    literal = scan_archive(archive, options("needle", max_matches=10)).matches[0]
    regex = scan_archive(archive, options(r"NE+DL[E]", regex=True, max_matches=10)).matches[0]
    assert literal.text_spans == ((7, 13),) and literal.query_spans == ((0, 6),)
    assert regex.text_spans == ((7, 13),) and regex.query_spans == ((0, 8),)


def test_literal_search_remains_contiguous_and_order_sensitive(tmp_path: Path) -> None:
    archive = tmp_path / "literal.zip"
    write_zip(archive, {"people.txt": "Скрепкин Глеб\nГлеб Скрепкин\n"})
    literal = scan_archive(archive, options("Глеб Скрепкин"))
    assert [match.text for match in literal.matches] == ["Глеб Скрепкин"]
