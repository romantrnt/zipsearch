# ruff: noqa: E501, E701, E702
"""Compact, disposable SQLite candidate index for ZipSearch.

SQLite owns small durable metadata and archive-scoped posting segments.  Record
text is never copied.  A segment contains delta-varint ``(unit, line)`` pairs
and is always verified by reopening the original ZIP member before a result is
returned.  Keeping segments per archive makes an incremental update a local
delete/rebuild operation rather than a corpus-wide posting rewrite.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import tempfile
import zipfile
import zlib
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Event

from .engine import (
    OFFICE_EXTENSIONS,
    SQLITE_EXTENSIONS,
    XLSX_EXTENSIONS,
    _copy_member_bounded,
    _office_lines,
    _path_allowed,
    _path_excluded,
    _sqlite_lines,
    _text_lines,
    _validate_infos,
    _xlsx_lines,
    _zipfile_source,
    discover_archives,
)
from .models import Issue, SafetyLimits, SearchOptions
from .smart import _PHONE_COMPONENT, phone_digits, tokens

SCHEMA_VERSION = 7
POSTING_MAGIC = b"ZSP1"
POSTING_VERSION = 1
_HEADER = struct.Struct(">4sBBII8s")
_DELTA = 1
_DELTA_ZLIB = 2
_MAX_SEGMENT_PAIRS = 20_000_000


class IndexError(RuntimeError):
    """A generated index cannot safely be used."""


@dataclass(frozen=True)
class IndexStatus:
    path: Path
    state: str
    usable: bool
    archives: int = 0
    members: int = 0
    records: int = 0
    terms: int = 0
    phones: int = 0
    bytes: int = 0
    new: int = 0
    changed: int = 0
    removed: int = 0
    source_compressed_bytes: int = 0
    source_expanded_bytes: int = 0
    ready_archives: int = 0


def default_index_path(root: Path) -> Path:
    return root / ".zipsearch.sqlite" if root.is_dir() else root.with_suffix(".zipsearch.sqlite")


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro" if readonly else path, uri=readonly)
    connection.row_factory = sqlite3.Row
    return connection


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS archives (
            id INTEGER PRIMARY KEY, relative_path TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
            size INTEGER NOT NULL, declared_bytes INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'ready'
        );
        CREATE TABLE IF NOT EXISTS units (
            id INTEGER PRIMARY KEY, archive_id INTEGER NOT NULL REFERENCES archives(id) ON DELETE CASCADE,
            member TEXT NOT NULL, nested_path TEXT NOT NULL, member_indices TEXT NOT NULL,
            records INTEGER NOT NULL, UNIQUE(archive_id, member_indices)
        );
        CREATE TABLE IF NOT EXISTS terms (
            id INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE,
            single_archive_id INTEGER REFERENCES archives(id) ON DELETE SET NULL,
            single_payload BLOB
        );
        CREATE TABLE IF NOT EXISTS posting_segments (
            term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
            archive_id INTEGER NOT NULL REFERENCES archives(id) ON DELETE CASCADE,
            payload BLOB NOT NULL, PRIMARY KEY(term_id, archive_id)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS entity_terms (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, value TEXT NOT NULL,
            single_archive_id INTEGER REFERENCES archives(id) ON DELETE SET NULL,
            single_payload BLOB, UNIQUE(kind, value)
        );
        CREATE TABLE IF NOT EXISTS entity_segments (
            entity_id INTEGER NOT NULL REFERENCES entity_terms(id) ON DELETE CASCADE,
            archive_id INTEGER NOT NULL REFERENCES archives(id) ON DELETE CASCADE,
            payload BLOB NOT NULL, PRIMARY KEY(entity_id, archive_id)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS units_archive ON units(archive_id);
        INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '7');
        INSERT OR IGNORE INTO meta(key, value) VALUES ('state', 'READY');
        INSERT OR IGNORE INTO meta(key, value) VALUES ('posting_format', 'ZSP1/v1');
        INSERT OR IGNORE INTO meta(key, value) VALUES ('config_fingerprint', 'normalization:nfkc-casefold-e-to-yo;units:v1');
        """
    )


def _put_varint(output: bytearray, value: int) -> None:
    if value < 0:
        raise ValueError("varints are unsigned")
    while value >= 128:
        output.append((value & 127) | 128)
        value >>= 7
    output.append(value)


def _get_varint(payload: bytes, position: int) -> tuple[int, int]:
    value = shift = 0
    for _ in range(10):
        if position >= len(payload):
            raise IndexError("truncated posting varint")
        byte = payload[position]
        position += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, position
        shift += 7
    raise IndexError("oversized posting varint")


def encode_posting(pairs: Iterable[tuple[int, int]]) -> bytes:
    """Encode sorted locators in a portable, checksummed ZSP1 envelope."""
    ordered = sorted(set(pairs))
    raw = bytearray()
    previous_unit = previous_line = 0
    for unit, line in ordered:
        if unit < 1 or line < 1:
            raise ValueError("posting locators start at one")
        _put_varint(raw, unit - previous_unit)
        _put_varint(raw, line - previous_line if unit == previous_unit else line)
        previous_unit, previous_line = unit, line
    stored = bytes(raw)
    compressed = zlib.compress(stored, level=6)
    encoding = _DELTA
    if len(compressed) + 4 < len(stored):
        stored, encoding = compressed, _DELTA_ZLIB
    checksum = hashlib.blake2s(raw, digest_size=8).digest()
    return (
        _HEADER.pack(POSTING_MAGIC, POSTING_VERSION, encoding, len(ordered), len(raw), checksum)
        + stored
    )


def decode_posting(blob: bytes) -> tuple[tuple[int, int], ...]:
    """Strictly decode a ZSP1 segment; corruption is never treated as empty."""
    if len(blob) < _HEADER.size:
        raise IndexError("truncated posting header")
    magic, version, encoding, count, raw_length, checksum = _HEADER.unpack_from(blob)
    if magic != POSTING_MAGIC or version != POSTING_VERSION or count > _MAX_SEGMENT_PAIRS:
        raise IndexError("unsupported or unsafe posting segment")
    raw = blob[_HEADER.size :]
    try:
        if encoding == _DELTA_ZLIB:
            raw = zlib.decompress(raw)
        elif encoding != _DELTA:
            raise IndexError("unknown posting encoding")
    except zlib.error as exc:
        raise IndexError("invalid compressed posting") from exc
    if len(raw) != raw_length or hashlib.blake2s(raw, digest_size=8).digest() != checksum:
        raise IndexError("posting checksum mismatch")
    result: list[tuple[int, int]] = []
    position = previous_unit = previous_line = 0
    for _ in range(count):
        unit_delta, position = _get_varint(raw, position)
        line_value, position = _get_varint(raw, position)
        unit = previous_unit + unit_delta
        line = previous_line + line_value if unit_delta == 0 else line_value
        if unit < 1 or line < 1 or result and (unit, line) <= result[-1]:
            raise IndexError("non-monotonic posting locator")
        result.append((unit, line))
        previous_unit, previous_line = unit, line
    if position != len(raw):
        raise IndexError("trailing posting bytes")
    return tuple(result)


def _fingerprint(path: Path) -> tuple[str, int, int, int]:
    stat = path.stat()
    declared = 0
    digest = hashlib.blake2b(digest_size=16)
    digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                declared += info.file_size
                digest.update(info.filename.encode("utf-8", "surrogateescape"))
                digest.update(
                    f"{info.CRC}:{info.compress_size}:{info.file_size}:{info.header_offset}".encode()
                )
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        digest.update(b"unreadable")
    return digest.hexdigest(), stat.st_size, stat.st_mtime_ns, declared


def _relative(root: Path, archive: Path) -> str:
    return str(archive.resolve().relative_to(root.resolve())) if root.is_dir() else archive.name


def _phones(text: str) -> set[str]:
    return {
        phone_digits(match.group())
        for match in _PHONE_COMPONENT.finditer(text)
        # Seven- and eight-digit numerical identifiers are common in real
        # exports and are not selective phone evidence. SMART still
        # recognises them during source verification; they simply do not
        # enter the optional entity index.
        if len(phone_digits(match.group())) >= 10
    }


def _records(
    archive_label: str,
    source: Path | object,
    options: SearchOptions,
    depth: int = 0,
    nested_path: tuple[str, ...] = (),
    member_indices: tuple[int, ...] = (),
    issues: list[Issue] | None = None,
) -> Iterator[tuple[str, tuple[str, ...], tuple[int, ...], int, str]]:
    issues = issues if issues is not None else []
    try:
        with zipfile.ZipFile(source) as archive:  # type: ignore[arg-type]
            infos = archive.infolist()
            permitted, found = _validate_infos(infos, archive_label, options)
            issues.extend(found)
            permitted_ids = {id(info) for info in permitted}
            for index, info in enumerate(infos):
                if id(info) not in permitted_ids:
                    continue
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
                                continue
                            with tempfile.SpooledTemporaryFile(
                                max_size=8 * 1024 * 1024, mode="w+b"
                            ) as spool:
                                _copy_member_bounded(member, spool, options.limits.max_member_bytes)
                                spool.seek(0)
                                yield from _records(
                                    archive_label,
                                    _zipfile_source(spool),
                                    options,
                                    depth + 1,
                                    nested_path + (info.filename,),
                                    member_indices + (index,),
                                    issues,
                                )
                            continue
                        lines = (
                            _sqlite_lines(member, options.limits.max_member_bytes)
                            if suffix in SQLITE_EXTENSIONS
                            else _xlsx_lines(member, options.limits.max_member_bytes)
                            if suffix in XLSX_EXTENSIONS
                            else _office_lines(member, suffix, options.limits.max_member_bytes)
                            if suffix in OFFICE_EXTENSIONS
                            else _text_lines(member, options.encoding)
                        )
                        for line, text in lines:
                            yield info.filename, nested_path, member_indices + (index,), line, text
                except (
                    OSError,
                    RuntimeError,
                    ValueError,
                    sqlite3.Error,
                    zipfile.BadZipFile,
                ) as exc:
                    issues.append(Issue(archive_label, str(exc), info.filename, "member"))
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError) as exc:
        issues.append(Issue(archive_label, f"cannot read ZIP: {exc}", kind="archive"))


def _store_value(
    connection: sqlite3.Connection,
    *,
    dictionary: str,
    segments: str,
    id_column: str,
    value: str,
    archive_id: int,
    payload: bytes,
    phone: bool = False,
    cache: dict[tuple[bool, str], tuple[int, int | None, bytes | None]] | None = None,
) -> None:
    """Inline a one-archive value; promote it exactly once when it spreads."""
    cache_key = phone, value
    cached = cache.get(cache_key) if cache is not None else None
    created = False
    if cached is None:
        if phone:
            row = connection.execute(
                "SELECT id, single_archive_id, single_payload FROM entity_terms "
                "WHERE kind='phone' AND value=?",
                (value,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT id, single_archive_id, single_payload FROM terms WHERE value=?", (value,)
            ).fetchone()
        if row is None:
            if phone:
                row_id = connection.execute(
                    "INSERT INTO entity_terms(kind, value) VALUES ('phone', ?)", (value,)
                ).lastrowid
            else:
                row_id = connection.execute(
                    "INSERT INTO terms(value) VALUES (?)", (value,)
                ).lastrowid
            cached = int(row_id), None, None
            created = True
        else:
            cached = row["id"], row["single_archive_id"], row["single_payload"]
    row_id, single_archive_id, single_payload = cached
    if created:
        connection.execute(
            f"UPDATE {dictionary} SET single_archive_id=?, single_payload=? WHERE id=?",
            (archive_id, payload, row_id),
        )
        if cache is not None:
            cache[cache_key] = row_id, archive_id, payload
        return
    if single_archive_id is None:
        # Existing promoted term, or a term whose only archive was deleted.
        existing = connection.execute(
            f"SELECT 1 FROM {segments} WHERE {id_column}=? AND archive_id=?",
            (row_id, archive_id),
        ).fetchone()
        if existing is None:
            connection.execute(
                f"INSERT INTO {segments}({id_column},archive_id,payload) VALUES (?,?,?)",
                (row_id, archive_id, payload),
            )
        return
    if single_archive_id == archive_id:
        connection.execute(
            f"UPDATE {dictionary} SET single_payload=? WHERE id=?", (payload, row_id)
        )
        if cache is not None:
            cache[cache_key] = row_id, archive_id, payload
        return
    connection.execute(
        f"INSERT OR REPLACE INTO {segments}({id_column},archive_id,payload) VALUES (?,?,?)",
        (row_id, single_archive_id, single_payload),
    )
    connection.execute(
        f"INSERT INTO {segments}({id_column},archive_id,payload) VALUES (?,?,?)",
        (row_id, archive_id, payload),
    )
    connection.execute(
        f"UPDATE {dictionary} SET single_archive_id=NULL, single_payload=NULL WHERE id=?",
        (row_id,),
    )
    if cache is not None:
        cache[cache_key] = row_id, None, None


def build_or_update(
    root: Path,
    index_path: Path,
    limits: SafetyLimits,
    *,
    rebuild: bool = False,
    cancelled: Event | None = None,
    progress: Callable[[dict[str, int]], None] | None = None,
) -> tuple[IndexStatus, list[Issue]]:
    """Build archive-local compact segments; every completed archive is durable."""
    if rebuild and index_path.exists():
        clean(index_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    issues: list[Issue] = []
    options = SearchOptions(patterns=("_",), limits=limits)
    with closing(_connect(index_path)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        _schema(connection)
        version = connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        if version != str(SCHEMA_VERSION):
            raise IndexError("index schema is outdated; rebuild required")
        connection.execute("UPDATE meta SET value='BUILDING' WHERE key='state'")
        connection.commit()
        current = {
            _relative(root, path): (path, *_fingerprint(path)) for path in discover_archives(root)
        }
        existing = {
            row["relative_path"]: row for row in connection.execute("SELECT * FROM archives")
        }
        with connection:
            for relative in set(existing) - set(current):
                connection.execute("DELETE FROM archives WHERE id=?", (existing[relative]["id"],))
        done = record_count = member_count = source = expanded = 0
        value_cache: dict[tuple[bool, str], tuple[int, int | None, bytes | None]] = {}

        def report() -> None:
            if progress:
                progress(
                    {
                        "archives_completed": done,
                        "archives_total": len(current),
                        "members_processed": member_count,
                        "records_indexed": record_count,
                        "source_bytes": source,
                        "expanded_bytes": expanded,
                        "issues": len(issues),
                    }
                )

        for relative, (archive, fingerprint, size, mtime, declared) in current.items():
            if cancelled and cancelled.is_set():
                break
            old = existing.get(relative)
            if old is not None and old["fingerprint"] == fingerprint and old["status"] == "ready":
                done += 1
                source += size
                expanded += declared
                report()
                continue
            with connection:
                if old is not None:
                    connection.execute("DELETE FROM archives WHERE id=?", (old["id"],))
                archive_id = connection.execute(
                    "INSERT INTO archives(relative_path,fingerprint,size,declared_bytes,mtime_ns,status) VALUES (?,?,?,?,?,'building')",
                    (relative, fingerprint, size, declared, mtime),
                ).lastrowid
                term_pairs: dict[str, list[tuple[int, int]]] = defaultdict(list)
                phone_pairs: dict[str, list[tuple[int, int]]] = defaultdict(list)
                unit_map: dict[tuple[str, tuple[str, ...], tuple[int, ...]], int] = {}
                unit_records: dict[int, int] = defaultdict(int)
                for member, nested, route, line, text in _records(
                    str(archive), archive, options, issues=issues
                ):
                    key = member, nested, route
                    if key not in unit_map:
                        unit_map[key] = len(unit_map) + 1
                    unit = unit_map[key]
                    unit_records[unit] += 1
                    record_count += 1
                    for value in set(tokens(text)):
                        term_pairs[value].append((unit, line))
                    for value in _phones(text):
                        phone_pairs[value].append((unit, line))
                unit_db: dict[int, int] = {}
                for (member, nested, route), local in unit_map.items():
                    unit_db[local] = connection.execute(
                        "INSERT INTO units(archive_id,member,nested_path,member_indices,records) VALUES (?,?,?,?,?)",
                        (
                            archive_id,
                            member,
                            json.dumps(nested, ensure_ascii=False),
                            json.dumps(route),
                            unit_records[local],
                        ),
                    ).lastrowid
                for value, pairs in term_pairs.items():
                    remapped = ((unit_db[unit], line) for unit, line in pairs)
                    _store_value(
                        connection,
                        dictionary="terms",
                        segments="posting_segments",
                        id_column="term_id",
                        value=value,
                        archive_id=archive_id,
                        payload=encode_posting(remapped),
                        cache=value_cache,
                    )
                for value, pairs in phone_pairs.items():
                    remapped = ((unit_db[unit], line) for unit, line in pairs)
                    _store_value(
                        connection,
                        dictionary="entity_terms",
                        segments="entity_segments",
                        id_column="entity_id",
                        value=value,
                        archive_id=archive_id,
                        payload=encode_posting(remapped),
                        phone=True,
                        cache=value_cache,
                    )
                connection.execute("UPDATE archives SET status='ready' WHERE id=?", (archive_id,))
                member_count += len(unit_map)
            done += 1
            source += size
            expanded += declared
            report()
        connection.execute(
            "UPDATE meta SET value=? WHERE key='state'",
            ("BUILDING" if cancelled and cancelled.is_set() else "READY",),
        )
        connection.commit()
    # Every archive transaction has committed before this point. Deep blob
    # decoding belongs to explicit ``index verify``; making a normal update pay
    # for it made unchanged updates scale with all historical postings.
    return status(root, index_path, verify_integrity=False), issues


def _freshness(root: Path, connection: sqlite3.Connection) -> tuple[dict[str, str], dict[str, str]]:
    indexed = {
        row["relative_path"]: row["fingerprint"]
        for row in connection.execute(
            "SELECT relative_path,fingerprint FROM archives WHERE status='ready'"
        )
    }
    actual = {_relative(root, path): _fingerprint(path)[0] for path in discover_archives(root)}
    return indexed, actual


def status(root: Path, index_path: Path, *, verify_integrity: bool = False) -> IndexStatus:
    if not index_path.exists():
        return IndexStatus(index_path, "NONE", False)
    try:
        with closing(_connect(index_path, readonly=True)) as connection:
            if verify_integrity:
                if (
                    connection.execute("PRAGMA quick_check").fetchone()[0] != "ok"
                    or connection.execute("PRAGMA foreign_key_check").fetchone() is not None
                ):
                    return IndexStatus(
                        index_path, "CORRUPT", False, bytes=index_path.stat().st_size
                    )
                for (payload,) in connection.execute(
                    "SELECT payload FROM posting_segments UNION ALL "
                    "SELECT payload FROM entity_segments UNION ALL "
                    "SELECT single_payload FROM terms WHERE single_payload IS NOT NULL UNION ALL "
                    "SELECT single_payload FROM entity_terms WHERE single_payload IS NOT NULL"
                ):
                    decode_posting(payload)
            version = connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            state = connection.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0]
            if version != str(SCHEMA_VERSION):
                return IndexStatus(
                    index_path, "SCHEMA_MISMATCH", False, bytes=index_path.stat().st_size
                )
            indexed, actual = _freshness(root, connection)
            new = len(set(actual) - set(indexed))
            removed = len(set(indexed) - set(actual))
            changed = sum(actual[k] != indexed[k] for k in set(actual) & set(indexed))
            counts = connection.execute(
                "SELECT (SELECT count(*) FROM archives), "
                "(SELECT coalesce(sum(records),0) FROM units), "
                "(SELECT count(*) FROM terms), "
                "(SELECT count(*) FROM entity_terms WHERE kind='phone'), "
                "(SELECT count(*) FROM units), "
                "(SELECT coalesce(sum(size),0) FROM archives), "
                "(SELECT coalesce(sum(declared_bytes),0) FROM archives)"
            ).fetchone()
            usable = state == "READY" and not (new or removed or changed)
            return IndexStatus(
                index_path,
                "READY" if usable else ("STALE" if state == "READY" else state),
                usable,
                counts[0],
                counts[4],
                counts[1],
                counts[2],
                counts[3],
                index_path.stat().st_size,
                new,
                changed,
                removed,
                counts[5],
                counts[6],
                len(set(actual) & set(indexed)),
            )
    except (OSError, sqlite3.Error, TypeError, IndexError):
        return IndexStatus(
            index_path,
            "CORRUPT",
            False,
            bytes=index_path.stat().st_size if index_path.exists() else 0,
        )


def fresh_indexed_paths(root: Path, index_path: Path) -> set[Path]:
    """Return only archive paths whose stored fingerprint still matches disk.

    This deliberately does not consult the database state flag: a BUILDING
    update may have durable, complete archive transactions that are safe to
    search while the remaining corpus is handled by a scan leg.
    """
    with closing(_connect(index_path, readonly=True)) as connection:
        indexed, actual = _freshness(root, connection)
    return {
        (root / relative if root.is_dir() else root)
        for relative in set(actual) & set(indexed)
        if actual[relative] == indexed[relative]
    }


def candidates(
    root: Path,
    index_path: Path,
    patterns: tuple[str, ...],
    *,
    smart: bool,
    case_sensitive: bool,
    allowed_relative: set[str] | None = None,
) -> tuple[dict[Path, set[tuple[str, tuple[str, ...], tuple[int, ...], int]]], dict[str, int]]:
    query_terms = {
        value for pattern in patterns for value in tokens(pattern, case_sensitive=case_sensitive)
    }
    all_phone_like = {
        phone_digits(pattern) for pattern in patterns if len(phone_digits(pattern)) >= 7
    }
    query_phones = {value for value in all_phone_like if len(value) >= 10}
    if all_phone_like - query_phones:
        # The compact entity index deliberately omits short numeric strings.
        # A token index cannot prove their absence when punctuation was part of
        # the query, so force the caller onto source scanning.
        return {}, {"candidate_records": 0, "planner_seed": "scan", "indexable": False}
    if not query_terms and not query_phones:
        return {}, {"candidate_records": 0, "planner_seed": "scan", "indexable": False}
    result: dict[Path, set[tuple[str, tuple[str, ...], tuple[int, ...], int]]] = defaultdict(set)
    with closing(_connect(index_path, readonly=True)) as connection:
        indexed, actual = _freshness(root, connection)
        permitted = set(actual) & set(indexed)
        permitted = {p for p in permitted if actual[p] == indexed[p]}
        if allowed_relative is not None:
            permitted &= allowed_relative
        if not permitted:
            return {}, {"candidate_records": 0, "planner_seed": "scan", "indexable": True}
        where = ",".join("?" for _ in permitted)
        rows: list[sqlite3.Row] = []
        literal_groups: list[tuple[tuple[str, ...], list[sqlite3.Row]]] = []
        if smart:
            terms = sorted(query_terms)
            if terms:
                term_where = " OR ".join(
                    "(t.value=? OR t.value LIKE ?)" if len(v) >= 2 else "t.value=?" for v in terms
                )
                parameters: list[object] = [*permitted]
                for value in terms:
                    parameters.extend((value, value + "%") if len(value) >= 2 else (value,))
                rows.extend(
                    connection.execute(
                        f"SELECT a.relative_path,s.payload FROM posting_segments s JOIN terms t ON t.id=s.term_id JOIN archives a ON a.id=s.archive_id WHERE a.relative_path IN ({where}) AND ({term_where})",
                        parameters,
                    )
                )
                rows.extend(
                    connection.execute(
                        f"SELECT a.relative_path,t.single_payload AS payload FROM terms t JOIN archives a ON a.id=t.single_archive_id WHERE a.relative_path IN ({where}) AND ({term_where})",
                        parameters,
                    )
                )
        else:
            # Every normalized literal component must occur in the same source
            # record. Intersecting its postings is therefore a safe, much
            # narrower candidate superset than the old token union.
            for pattern in patterns:
                required = sorted(set(tokens(pattern, case_sensitive=case_sensitive)))
                if not required:
                    return {}, {"candidate_records": 0, "planner_seed": "scan", "indexable": False}
                # A literal regex search can match inside a larger source token
                # (``needle`` in ``needles``).  Exact dictionary equality would
                # silently omit that real result, so each required normalized
                # component uses a SQL substring superset and source verification
                # remains the final authority.
                group: list[tuple[str, sqlite3.Row]] = []
                for value in required:
                    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    parameters = [*permitted, f"%{escaped}%"]
                    group.extend(
                        (value, row)
                        for row in connection.execute(
                            f"SELECT a.relative_path,s.payload FROM posting_segments s "
                            f"JOIN terms t ON t.id=s.term_id JOIN archives a ON a.id=s.archive_id "
                            f"WHERE a.relative_path IN ({where}) AND t.value LIKE ? ESCAPE '\\'",
                            parameters,
                        )
                    )
                    group.extend(
                        (value, row)
                        for row in connection.execute(
                            f"SELECT a.relative_path,t.single_payload AS payload FROM terms t "
                            f"JOIN archives a ON a.id=t.single_archive_id WHERE a.relative_path IN ({where}) "
                            "AND t.value LIKE ? ESCAPE '\\'",
                            parameters,
                        )
                    )
                literal_groups.append((tuple(required), group))
        if query_phones:
            rows.extend(
                connection.execute(
                    f"SELECT a.relative_path,s.payload FROM entity_segments s JOIN entity_terms e ON e.id=s.entity_id JOIN archives a ON a.id=s.archive_id WHERE a.relative_path IN ({where}) AND e.kind='phone' AND e.value IN ({','.join('?' for _ in query_phones)})",
                    [*permitted, *query_phones],
                )
            )
            rows.extend(
                connection.execute(
                    f"SELECT a.relative_path,e.single_payload AS payload FROM entity_terms e JOIN archives a ON a.id=e.single_archive_id WHERE a.relative_path IN ({where}) AND e.kind='phone' AND e.value IN ({','.join('?' for _ in query_phones)})",
                    [*permitted, *query_phones],
                )
            )
        # Segment payload stores unit database ids, so map them once per archive. Query rows are
        # intentionally expanded in Python: decoding is checksum-verified and avoids row-per-hit SQL.
        unit_rows = {
            row["id"]: row
            for row in connection.execute(
                "SELECT id,archive_id,member,nested_path,member_indices FROM units"
            )
        }
        # SMART and phone branches deliberately retain union semantics.
        for row in rows:
            pairs = decode_posting(row["payload"])
            current = {(row["relative_path"], unit, line) for unit, line in pairs}
            for relative, unit, line in current:
                unit_row = unit_rows.get(unit)
                if unit_row:
                    result[root / relative if root.is_dir() else root].add(
                        (
                            unit_row["member"],
                            tuple(json.loads(unit_row["nested_path"])),
                            tuple(json.loads(unit_row["member_indices"])),
                            line,
                        )
                    )
        for required, group in literal_groups:
            by_value: dict[str, set[tuple[str, int, int]]] = defaultdict(set)
            for required_value, row in group:
                by_value[required_value].update(
                    (row["relative_path"], unit, line)
                    for unit, line in decode_posting(row["payload"])
                )
            if any(value not in by_value for value in required):
                continue
            for relative, unit, line in set.intersection(*(by_value[value] for value in required)):
                unit_row = unit_rows.get(unit)
                if unit_row:
                    result[root / relative if root.is_dir() else root].add(
                        (
                            unit_row["member"],
                            tuple(json.loads(unit_row["nested_path"])),
                            tuple(json.loads(unit_row["member_indices"])),
                            line,
                        )
                    )
    return dict(result), {
        "candidate_records": sum(len(v) for v in result.values()),
        "planner_seed": "phone" if query_phones else "token",
        "indexable": True,
    }


def storage_breakdown(index_path: Path) -> dict[str, object]:
    with closing(_connect(index_path, readonly=True)) as connection:
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        result: dict[str, object] = {
            "page_size": page_size,
            "page_count": page_count,
            "freelist_pages": connection.execute("PRAGMA freelist_count").fetchone()[0],
            "allocated_bytes": page_size * page_count,
            "dbstat": None,
        }
        try:
            result["dbstat"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT name,SUM(pgsize) AS bytes,COUNT(*) AS pages FROM dbstat GROUP BY name ORDER BY bytes DESC"
                )
            ]
        except sqlite3.Error:
            result["dbstat"] = "unavailable"
        return result


def clean(index_path: Path) -> None:
    for path in (index_path, Path(str(index_path) + "-wal"), Path(str(index_path) + "-shm")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def compact(index_path: Path) -> None:
    """Reclaim free SQLite pages after incremental archive replacement."""
    if not index_path.exists():
        raise IndexError("index does not exist")
    with closing(_connect(index_path)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
