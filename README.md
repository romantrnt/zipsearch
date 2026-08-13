# ZipSearch

ZipSearch is a fast command-line search tool for large collections of ZIP archives. It searches files *inside* ZIPs directly, so you can inspect thousands of archives without first expanding them into a huge temporary tree.

It is useful for data engineering, incident response, archives, exports, backups, and any collection where the archive boundary should not get in the way of a search.

## Two first-class interfaces

For interactive work, launch the terminal application:

```console
zipsearch
# or: zipsearch tui ./archives
```

It opens with an editable `query>` prompt, streams grouped archive/member matches into a navigable table, highlights matches, and shows detail, progress, errors, and export controls. It is deliberately terminal-aware: a bare `zipsearch` never opens an interactive screen when standard input or output is piped.

For scripts and pipelines, use the stable command interface:

```console
zipsearch search ./archives "Иван Соколов" --regex -j 4 --jsonl
zipsearch inspect evidence.zip --json
```

`search` and `inspect` retain their existing behaviour; the TUI is a separate presentation layer over the same engine.

### TUI keys

| Key | Action |
| --- | --- |
| `/` | Focus the query field |
| `Enter` | Start search, or inspect selected result |
| `r` | Change search root without restarting |
| `m` | Cycle SMART → LITERAL → REGEX search modes |
| `f` | Open filters and advanced safety controls |
| `↑`/`↓`, `j`/`k`, PgUp/PgDn, Home/End | Navigate virtualized results |
| `x` | Inspect recoverable errors |
| `e` | Export current results as JSONL, CSV, and text |
| `Ctrl+C` | Cancel the current UI result stream |
| `q` / `?` | Quit / built-in shortcut help |

Use comma-separated patterns in the search field for multi-pattern search. SMART is the TUI default: it normalizes Unicode/case/whitespace/punctuation and ranks exact phrases, all-token records in either order, safe token prefixes, then individual-token matches. It is not edit-distance fuzzy search. LITERAL and REGEX retain their explicit predictable semantics. The root prompt accepts directories, a single `.zip`, relative paths, and `~`; changing it preserves query/settings but clears stale results.

The Filters dialog exposes extensions, include/exclude globs, encoding, context, nested depth, result cap, worker count, and the engine’s archive safety limits. Regex is validated before a scan starts.

The TUI is implemented with Python's standard `curses` layer, so the search engine and CLI remain free of UI dependencies. It uses terminal attributes (reverse, bold, underline) rather than a hard-coded truecolor theme; it is therefore usable in monochrome and limited-color terminals. JSONL is always uncolored.

## Install

Requires Python 3.10 or later.

```console
python -m pip install .
```

For development:

```console
python -m pip install -e . pytest ruff
pytest
ruff check .
```

## Quick start

Search one archive:

```console
zipsearch search evidence.zip "customer@example.com"
```

Search every ZIP below a directory with four bounded workers:

```console
zipsearch search ./archives "invoice-2025" -j 4
```

Use the optional ranked human-search mode without changing ordinary CLI semantics:

```console
zipsearch search ./archives "Глеб Скрепкин" --smart
```

Search several terms, only in CSV and JSON members, with path filters:

```console
zipsearch search ./archives alice@example.com "account closed" \
  --extension csv --extension json --include 'exports/**' --exclude '*backup*'
```

Regular expressions, context, and machine-readable output:

```console
zipsearch search ./archives 'INV-[0-9]{8}' --regex -C 2 --jsonl > matches.jsonl
```

Inspect the central directory of an archive without decompressing members:

```console
zipsearch inspect evidence.zip --json
```

Run `zipsearch search --help` for the complete command reference.

## What it searches

By default ZipSearch scans line-oriented text and common structured-text extensions: TXT, CSV/TSV, logs, JSON/JSONL, XML, HTML, Markdown, YAML, INI/config files, and the historical project's DAT/TAD/CPY formats. It also searches SQLite rows and XLSX worksheet values. Legacy `.xls` is deliberately not decoded because it requires a large optional parser and is unsafe to guess from raw binary; convert it to XLSX or CSV first.

The historical tool's useful capabilities are retained in a scriptable form: recursive discovery, multiple literal patterns, regex search, case controls, member include/exclude globs, extension filtering, result grouping by archive/member path, result limits, structured JSONL output, and SQLite/XLSX handling.

Nested ZIP members are supported up to depth 2 by default. Their source appears as `outer.zip:inner.zip!file.txt:line`. Set `--nested-depth 0` to disable this.

## Output and exit behavior

Human output is one match per line:

```text
/data/batch/a.zip:exports/users.csv:42: alice@example.com,active
```

`--jsonl` writes match objects and one final summary object to standard output, making it suitable for pipelines. Diagnostics and the normal final summary go to standard error. A damaged archive is reported and does not stop the rest of the collection. Exit status is zero for a completed scan with no archive/member errors, one when recoverable errors occurred, two for invalid command configuration, and 130 for Ctrl-C.

## Safety and resource model

ZipSearch never calls `extractall`. Normal members are opened with `ZipFile.open()` and scanned incrementally. SQLite is the only built-in handler requiring random access; only that member is copied to an isolated `TemporaryDirectory`, opened read-only, and removed even if parsing fails. XLSX and nested ZIP handling use an automatically removed spooled temporary file, which remains in memory only up to 8 MiB and then uses the system temporary area.

Before decompressing, ZipSearch checks the central directory. Defaults reject archives with more than 100,000 members, members larger than 512 MiB, total declared expansion above 2 GiB, or a member compression ratio over 200:1. Unsafe member paths, encrypted members, and excessive nesting are skipped and reported. Tune these only when you trust the input:

```console
zipsearch search ./trusted-exports needle --max-member-mib 1024 --max-total-mib 8192
```

Archive concurrency uses a fixed-size thread pool (four workers by default) and keeps at most two worker windows queued. This is a deliberate fit for mixed disk I/O, ZIP decompression, and text scanning; it avoids the abandoned project's unbounded all-data index and excessive process creation. Results are emitted as archives finish, with a global output cap (default 1,000).

## Limitations

Only ZIP is supported in this release. RAR and 7z were experimental in the historical scripts and depended on external native tools; they are intentionally excluded instead of presenting unreliable support. Password-protected ZIP members are reported as skipped; passwords are not read from ad-hoc files. Search defaults to `--encoding auto` (UTF-8, then CP1251 for legacy Russian exports); use an explicit Python codec when the encoding is known. Binary members are skipped when an early NUL byte is detected.

ZIP metadata is attacker-controlled. Safety checks reduce risk but are not a substitute for running untrusted content with normal OS-level isolation.

## Development and release

The test suite creates deterministic ZIP fixtures at runtime—no real databases, archives, password files, or personal data are committed. CI tests Python 3.10–3.12, package installation, and Ruff. The historical pre-Git snapshots are intentionally ignored by `.gitignore`; they were used as local archaeology and are not part of the distributable source tree.

This project is licensed under the [MIT License](LICENSE).

## Reproducible realistic dataset

For a disposable five-archive stress dataset made entirely from fixed-seed fictional Russian institutional records, run:

```console
python benchmarks/validate_realistic_dataset.py
```

It creates or refreshes the deliberately external sibling dataset at
`../testdata/realistic/`, runs the public CLI through literal, regex,
multi-pattern, format-filtered, nested-ZIP, JSONL, and concurrent searches,
checks expected match counts, and confirms temporary cleanup. The generated ZIPs
are never repository artifacts. To generate without validation, use
`python benchmarks/generate_realistic_dataset.py`.
