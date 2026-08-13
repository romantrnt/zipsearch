from __future__ import annotations

import fnmatch
import io
import logging
import os
import re
import sqlite3
import tempfile
import zipfile
from collections import deque
from collections.abc import Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import closing
from pathlib import Path, PurePosixPath
from threading import Event
from typing import BinaryIO

from .models import ArchiveScan, Issue, Match, SafetyLimits, SearchOptions
from .smart import SmartPattern
from .smart import compile_patterns as compile_smart_patterns
from .smart import match as smart_match

TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".csv",
        ".tsv",
        ".log",
        ".json",
        ".jsonl",
        ".xml",
        ".html",
        ".htm",
        ".md",
        ".rst",
        ".yaml",
        ".yml",
        ".ini",
        ".cfg",
        ".conf",
        ".dat",
        ".tad",
        ".cpy",
    }
)
SQLITE_EXTENSIONS = frozenset({".db", ".sqlite", ".sqlite3"})
XLSX_EXTENSIONS = frozenset({".xlsx"})
LOGGER = logging.getLogger(__name__)


class SearchConfigurationError(ValueError):
    pass


def discover_archives(path: Path, recursive: bool = True) -> Iterator[Path]:
    """Yield ZIP files deterministically without following directory symlinks."""
    if path.is_file():
        if path.suffix.lower() != ".zip":
            raise SearchConfigurationError(f"{path} is not a ZIP archive")
        yield path
        return
    if not path.is_dir():
        raise SearchConfigurationError(f"path does not exist: {path}")
    if not recursive:
        for candidate in sorted(path.iterdir()):
            if candidate.is_file() and candidate.suffix.lower() == ".zip":
                yield candidate
        return
    # Keeps only one directory's entries in memory and never follows symlinks.
    for root, directories, filenames in os.walk(path, followlinks=False):
        directories.sort()
        for filename in sorted(filenames):
            candidate = Path(root, filename)
            if candidate.suffix.lower() == ".zip" and candidate.is_file():
                yield candidate


def _path_allowed(name: str, options: SearchOptions) -> bool:
    normalized = name.replace("\\", "/")
    basename = PurePosixPath(normalized).name
    suffix = PurePosixPath(normalized).suffix.lower()
    if options.extensions:
        accepted = {
            ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in options.extensions
        }
        if suffix not in accepted:
            return False
    elif suffix not in TEXT_EXTENSIONS | SQLITE_EXTENSIONS | XLSX_EXTENSIONS | {".zip"}:
        return False
    if options.include and not any(
        fnmatch.fnmatchcase(normalized, pattern) or fnmatch.fnmatchcase(basename, pattern)
        for pattern in options.include
    ):
        return False
    return not _path_excluded(normalized, basename, options)


def _path_excluded(normalized: str, basename: str, options: SearchOptions) -> bool:
    return any(
        fnmatch.fnmatchcase(normalized, pattern) or fnmatch.fnmatchcase(basename, pattern)
        for pattern in options.exclude
    )


def _is_unsafe_name(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    return path.is_absolute() or ".." in path.parts


def _validate_infos(
    infos: Sequence[zipfile.ZipInfo], archive_name: str, options: SearchOptions
) -> tuple[list[zipfile.ZipInfo], list[Issue]]:
    issues: list[Issue] = []
    if len(infos) > options.limits.max_members:
        return [], [
            Issue(
                archive_name,
                f"skipped archive: {len(infos)} members exceeds safety limit",
                kind="safety",
            )
        ]
    total = 0
    allowed: list[zipfile.ZipInfo] = []
    for info in infos:
        if info.is_dir():
            continue
        total += info.file_size
        if _is_unsafe_name(info.filename):
            issues.append(
                Issue(archive_name, "skipped unsafe member path", info.filename, "safety")
            )
            continue
        if info.flag_bits & 0x1:
            issues.append(
                Issue(archive_name, "skipped encrypted member", info.filename, "unsupported")
            )
            continue
        if info.file_size > options.limits.max_member_bytes:
            issues.append(Issue(archive_name, "skipped oversized member", info.filename, "safety"))
            continue
        ratio = info.file_size / max(info.compress_size, 1)
        if ratio > options.limits.max_compression_ratio:
            issues.append(
                Issue(
                    archive_name,
                    f"skipped suspicious compression ratio ({ratio:.1f}:1)",
                    info.filename,
                    "safety",
                )
            )
            continue
        allowed.append(info)
    if total > options.limits.max_total_bytes:
        return [], issues + [
            Issue(
                archive_name,
                "skipped archive: declared expanded size exceeds safety limit",
                kind="safety",
            )
        ]
    return allowed, issues


def _compile_patterns(options: SearchOptions) -> list[tuple[str, re.Pattern[str]]]:
    if not options.patterns:
        raise SearchConfigurationError("at least one search pattern is required")
    flags = 0 if options.case_sensitive else re.IGNORECASE
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for pattern in options.patterns:
        if not pattern:
            raise SearchConfigurationError("patterns cannot be empty")
        try:
            compiled.append(
                (pattern, re.compile(pattern if options.regex else re.escape(pattern), flags))
            )
        except re.error as exc:
            raise SearchConfigurationError(
                f"invalid regular expression {pattern!r}: {exc}"
            ) from exc
    return compiled


def _text_lines(stream: BinaryIO, encoding: str) -> Iterator[tuple[int, str]]:
    buffered = io.BufferedReader(stream, buffer_size=64 * 1024)
    probe = buffered.peek(4096)
    if b"\x00" in probe:
        return
    if probe.startswith(b"\xef\xbb\xbf"):
        selected = "utf-8-sig"
    elif encoding == "auto":
        try:
            probe.decode("utf-8")
            selected = "utf-8"
        except UnicodeDecodeError:
            selected = "cp1251"
    else:
        selected = encoding
    with io.TextIOWrapper(buffered, encoding=selected, errors="replace", newline="") as text:
        line_number = 0
        while True:
            line = text.readline(1_048_577)
            if not line:
                break
            line_number += 1
            # Keep line-oriented memory bounded.  The fragment still allows useful matches.
            yield line_number, line.rstrip("\r\n")


def _match_text(
    archive: str,
    member: str,
    lines: Iterator[tuple[int, str]],
    compiled: list[tuple[str, re.Pattern[str]]],
    budget: int,
    context: int = 0,
    nested_path: tuple[str, ...] = (),
    cancelled: Event | None = None,
    smart_patterns: tuple[SmartPattern, ...] = (),
    regex: bool = False,
) -> Iterator[Match]:
    before: deque[str] = deque(maxlen=context)
    iterator = iter(lines)
    buffered: deque[tuple[int, str]] = deque()
    while True:
        if cancelled is not None and cancelled.is_set():
            return
        if buffered:
            line_number, text = buffered.popleft()
        else:
            try:
                line_number, text = next(iterator)
            except StopIteration:
                return
        matched = tuple(source for source, regex in compiled if regex.search(text))
        smart = smart_match(text, smart_patterns) if smart_patterns else None
        if matched or smart:
            after: list[str] = []
            for _ in range(context):
                try:
                    following = next(iterator)
                except StopIteration:
                    break
                after.append(following[1])
                buffered.append(following)
            text_spans = (
                smart.text_spans
                if smart
                else tuple(
                    (found.start(), found.end())
                    for _, expression in compiled
                    for found in expression.finditer(text)
                )
            )
            yield Match(
                archive,
                member,
                line_number,
                text,
                smart.patterns if smart else matched,
                tuple(before),
                tuple(after),
                nested_path,
                smart.score if smart else 0,
                smart.match_type if smart else ("regex" if regex else "literal"),
                text_spans,
                smart.query_spans if smart else tuple((0, len(source)) for source in matched),
            )
            budget -= 1
            if budget <= 0:
                return
        before.append(text)


def _sqlite_lines(stream: BinaryIO) -> Iterator[tuple[int, str]]:
    """SQLite requires random access, so copy just this member into an auto-cleaned file."""
    with tempfile.TemporaryDirectory(prefix="zipsearch-") as temp_dir:
        database = Path(temp_dir) / "member.sqlite"
        with database.open("wb") as output:
            while chunk := stream.read(64 * 1024):
                output.write(chunk)
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            for (table,) in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                for row_number, row in enumerate(
                    connection.execute(f"SELECT * FROM {quoted}"), start=1
                ):
                    yield (
                        row_number,
                        f"[{table}] "
                        + " ".join("" if value is None else str(value) for value in row),
                    )


def _xlsx_lines(stream: BinaryIO) -> Iterator[tuple[int, str]]:
    """Read XLSX XML directly from a spooled temporary file; no archive tree is extracted."""
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as spool:
        while chunk := stream.read(64 * 1024):
            spool.write(chunk)
        spool.seek(0)
        with zipfile.ZipFile(spool) as workbook:
            shared: list[str] = []
            try:
                with workbook.open("xl/sharedStrings.xml") as strings:
                    import xml.etree.ElementTree as element_tree

                    for _, element in element_tree.iterparse(strings, events=("end",)):
                        if element.tag.endswith("}si"):
                            shared.append("".join(element.itertext()))
                            element.clear()
            except KeyError:
                pass
            line_number = 0
            for info in workbook.infolist():
                if not info.filename.startswith("xl/worksheets/") or not info.filename.endswith(
                    ".xml"
                ):
                    continue
                with workbook.open(info) as sheet:
                    import xml.etree.ElementTree as element_tree

                    for _, row in element_tree.iterparse(sheet, events=("end",)):
                        if not row.tag.endswith("}row"):
                            continue
                        values: list[str] = []
                        for cell in row:
                            if not cell.tag.endswith("}c"):
                                continue
                            value = next(
                                (node.text for node in cell if node.tag.endswith("}v")), None
                            )
                            if value is None:
                                value = "".join(
                                    node.text or ""
                                    for node in cell.iter()
                                    if node.tag.endswith("}t")
                                )
                            if cell.attrib.get("t") == "s" and value and value.isdigit():
                                value = shared[int(value)] if int(value) < len(shared) else value
                            if value:
                                values.append(value)
                        line_number += 1
                        if values:
                            yield line_number, " ".join(values)
                        row.clear()


def _scan_zip(
    archive_label: str,
    source: str | Path | BinaryIO,
    options: SearchOptions,
    depth: int = 0,
    nested_path: tuple[str, ...] = (),
    cancelled: Event | None = None,
) -> ArchiveScan:
    scan = ArchiveScan(Path(archive_label))
    compiled = [] if options.smart else _compile_patterns(options)
    smart_patterns = (
        compile_smart_patterns(options.patterns, case_sensitive=options.case_sensitive)
        if options.smart
        else ()
    )
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            scan.members_seen = len([info for info in infos if not info.is_dir()])
            scan.bytes_declared = sum(info.file_size for info in infos if not info.is_dir())
            permitted, issues = _validate_infos(infos, archive_label, options)
            scan.issues.extend(issues)
            for info in permitted:
                if cancelled is not None and cancelled.is_set():
                    return scan
                if not options.smart and len(scan.matches) >= options.max_matches:
                    scan.issues.append(Issue(archive_label, "match limit reached", kind="limit"))
                    break
                suffix = PurePosixPath(info.filename).suffix.lower()
                if suffix == ".zip":
                    normalized = info.filename.replace("\\", "/")
                    if _path_excluded(normalized, PurePosixPath(normalized).name, options):
                        continue
                elif not _path_allowed(info.filename, options):
                    continue
                try:
                    with archive.open(info) as member:
                        if suffix == ".zip":
                            if depth >= options.limits.max_nested_depth:
                                scan.issues.append(
                                    Issue(
                                        archive_label,
                                        "skipped nested ZIP: depth limit reached",
                                        info.filename,
                                        "safety",
                                    )
                                )
                                continue
                            with tempfile.SpooledTemporaryFile(
                                max_size=8 * 1024 * 1024, mode="w+b"
                            ) as spool:
                                while chunk := member.read(64 * 1024):
                                    spool.write(chunk)
                                spool.seek(0)
                                child = _scan_zip(
                                    archive_label,
                                    spool,
                                    options,
                                    depth + 1,
                                    nested_path + (info.filename,),
                                    cancelled,
                                )
                            scan.members_scanned += child.members_scanned
                            _append_matches(scan.matches, iter(child.matches), options)
                            scan.issues.extend(child.issues)
                            continue
                        if suffix in SQLITE_EXTENSIONS:
                            lines = _sqlite_lines(member)
                        elif suffix in XLSX_EXTENSIONS:
                            lines = _xlsx_lines(member)
                        else:
                            lines = _text_lines(member, options.encoding)
                        matches = _match_text(
                            archive_label,
                            info.filename,
                            lines,
                            compiled,
                            1_000_000_000
                            if options.smart
                            else options.max_matches - len(scan.matches),
                            options.context,
                            nested_path,
                            cancelled,
                            smart_patterns,
                            options.regex,
                        )
                        _append_matches(scan.matches, matches, options)
                        scan.members_scanned += 1
                except (
                    NotImplementedError,
                    OSError,
                    RuntimeError,
                    sqlite3.Error,
                    ValueError,
                    zipfile.BadZipFile,
                ) as exc:
                    scan.issues.append(Issue(archive_label, str(exc), info.filename, "member"))
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError) as exc:
        scan.issues.append(Issue(archive_label, f"cannot read ZIP: {exc}", kind="archive"))
    if options.smart:
        scan.matches.sort(key=_match_sort_key)
        del scan.matches[options.max_matches :]
    return scan


def _match_sort_key(match: Match) -> tuple[int, str, tuple[str, ...], str, int]:
    return (-match.score, match.archive, match.nested_path, match.member, match.line)


def rank_matches(matches: Iterator[Match], limit: int) -> list[Match]:
    """Return deterministic top-N smart results without retaining an unbounded corpus."""
    retained: list[Match] = []
    for match in matches:
        retained.append(match)
        if len(retained) > limit * 2:
            retained.sort(key=_match_sort_key)
            del retained[limit:]
    retained.sort(key=_match_sort_key)
    return retained[:limit]


def _append_matches(target: list[Match], matches: Iterator[Match], options: SearchOptions) -> None:
    """Bound smart retention so late high-confidence hits displace weak early hits."""
    if not options.smart:
        target.extend(matches)
        return
    for match in matches:
        target.append(match)
        if len(target) > options.max_matches * 2:
            target.sort(key=_match_sort_key)
            del target[options.max_matches :]


def scan_archive(path: Path, options: SearchOptions, cancelled: Event | None = None) -> ArchiveScan:
    LOGGER.debug("scanning archive %s", path)
    return _scan_zip(str(path), path, options, cancelled=cancelled)


def inspect_archive(path: Path, limits: SafetyLimits) -> ArchiveScan:
    """Read only the central directory; member data is never decompressed."""
    scan = ArchiveScan(path)
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            scan.members_seen = len([info for info in infos if not info.is_dir()])
            scan.bytes_declared = sum(info.file_size for info in infos if not info.is_dir())
            # Validation needs options only for limits; filtering is irrelevant here.
            _, scan.issues = _validate_infos(
                infos, str(path), SearchOptions(patterns=("_",), limits=limits)
            )
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError) as exc:
        scan.issues.append(Issue(str(path), f"cannot read ZIP: {exc}", kind="archive"))
    return scan


def scan_archives(
    paths: Iterator[Path], options: SearchOptions, cancelled: Event | None = None
) -> Iterator[ArchiveScan]:
    """Scan archives with a fixed-size executor and at most two task windows queued."""
    workers = max(1, options.workers)
    iterator = iter(paths)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="zipsearch") as executor:
        pending: set[Future[ArchiveScan]] = set()

        def refill() -> None:
            while (cancelled is None or not cancelled.is_set()) and len(pending) < workers * 2:
                try:
                    pending.add(executor.submit(scan_archive, next(iterator), options))
                except StopIteration:
                    return

        refill()
        while pending:
            if cancelled is not None and cancelled.is_set():
                for future in pending:
                    future.cancel()
                return
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                yield future.result()
            refill()
