<div align="center">

[**English**](README.md) | [中文](README.zh-CN.md) | [Русский](README.ru.md)

# ZipSearch

**Search and inspect text and structured records inside ZIP archives without bulk extraction.**

<img src="docs/assets/tui.png" alt="ZipSearch TUI showing archive search results and record details" width="900">

</div>

ZipSearch is a local terminal tool for searching heterogeneous collections stored in ZIP files: exports, logs, spreadsheets, SQLite databases, and nested archives. It reads archive members directly, groups results for inspection, and applies bounded resource controls instead of requiring a permanently expanded working tree.

The keyboard-first TUI keeps the archive, member, result, and record detail in one view for fast inspection.

ZipSearch is the spiritual successor to `awerpars`, an earlier project whose ideas and lessons evolved into this independent implementation.

## Why it exists

Large archive collections are awkward to inspect by hand. Expanding every archive just to locate a value duplicates storage, costs I/O, and leaves cleanup work behind. ZipSearch discovers ZIPs, reads eligible members, and exposes the resulting records through both a command-line interface and a curses TUI.

> ZipSearch does decompress data while reading it. “Without bulk extraction” means it does not expand an archive collection into a persistent directory tree. Some handlers use automatically removed temporary storage; see [Storage behavior](#storage-behavior).

## At a glance

| Area | What ZipSearch provides |
| --- | --- |
| Search | Literal, regular-expression, and normalized token-aware SMART search |
| Data | Text-like members, SQLite rows, XLSX worksheet rows, nested ZIPs |
| Inspection | Archive → member → result tree; detail panel; central-directory inspection |
| Operations | Include/exclude globs, extension filters, context, root switching, export, cancellation |
| Controls | Member/count/expanded-size/compression-ratio/nesting limits; recoverable issue reporting |

## Install

Python **3.10 or newer** is required. ZipSearch has no runtime third-party dependencies.

```console
python -m pip install .
zipsearch --version
```

For a checkout used during development:

```console
python -m pip install -e . pytest ruff
```

Running `zipsearch` with no arguments opens the TUI only when both standard input and output are terminals. In scripts, use an explicit subcommand.

## Quick start

```console
# Interactive TUI, rooted at a directory or one ZIP file
zipsearch tui ./archives

# Ordinary literal search (case-insensitive by default)
zipsearch search ./archives 'customer@example.com'

# SMART: punctuation/case/word-order-aware human search
zipsearch search ./archives 'Глеб Скрепкин +79087562342' --smart

# Regex, context lines, and JSON Lines output
zipsearch search ./archives 'INV-[0-9]{8}' --regex -C 2 --jsonl > matches.jsonl

# Limit eligible member paths and extensions
zipsearch search ./archives needle --extension csv --extension json \
  --include 'exports/**' --exclude '*backup*'

# Read only an archive central directory; do not decompress members
zipsearch inspect evidence.zip --json
```

Run `zipsearch search --help` for the complete CLI reference.

## Search semantics

| Mode | Selection rule | Ordering |
| --- | --- | --- |
| **LITERAL** | Each pattern is an escaped, contiguous regular-expression search. Multi-word order matters. | Archive/member/line scan order, subject to the global cap. |
| **REGEX** | Each pattern is compiled as a Python regular expression. | Archive/member/line scan order, subject to the global cap. |
| **SMART** | Normalized phrase, token, prefix, and phone evidence are considered for each query pattern. | Stronger evidence ranks first globally. |

LITERAL is the CLI default; SMART is the TUI default. `--smart` and `--regex` are mutually exclusive. Searches are case-insensitive unless `--case-sensitive` is selected; `--ignore-case` explicitly selects the default.

### SMART matching

SMART normalizes Unicode with NFKC, folds case (and Russian `ё` to `е` when case-insensitive), treats punctuation as separators, and collapses whitespace. It does not use edit distance or spelling guesses.

For a multi-token pattern, SMART ranks normalized phrase matches first, then records containing all tokens in any order, then safe token-prefix matches, then useful partial-token matches. A record may remain in the result set when only part of a multi-token query matched. For phone-shaped query components of at least seven digits, separators are ignored and Russian `8xxxxxxxxxx` is normalized to `7xxxxxxxxxx`.

For example, `Глеб Скрепкин +79087562342` can match `Игорь Скрепкин; +7 (908) 756-23-42`. The matching evidence is `Скрепкин` and the phone number; `Глеб` remains visible in the query but is not treated as evidence.

`match_type` and `score` appear in JSONL output and result-detail inspection. A score is an internal ordering signal, not a percentage or a cross-query relevance measure.

## TUI

`zipsearch tui [PATH]` opens a keyboard-first interface with an editable query line, a RESULTS pane, a DETAIL pane, and a compact status/footer area.

- **RESULTS** is a tree: archive → member → matching record. Archive and member nodes can be collapsed without changing the loaded result set, ranking, or search.
- **DETAIL** shows the selected record, including the full original `matched` query and only the query components that contributed to that result. It also renders available archive/member metadata when a tree node is selected.
- Match evidence is underlined and may receive one restrained terminal accent color when curses color support is available. Monochrome terminals retain attribute-based highlighting. The selected row remains reverse-video.
- The status line reports archive/member counts, hits, issues, workers, nesting depth, **S** (current or last search duration), and **U** (continuously increasing TUI uptime).

### Keyboard reference

| Key | Action |
| --- | --- |
| <kbd>/</kbd> | Focus/edit the query |
| <kbd>Enter</kbd> | Run the query; when browsing, inspect the selected tree node or result |
| <kbd>↑</kbd>/<kbd>↓</kbd>, <kbd>j</kbd>/<kbd>k</kbd> | Navigate results; while editing, browse query history |
| <kbd>PgUp</kbd>/<kbd>PgDn</kbd>, <kbd>Home</kbd>/<kbd>End</kbd> | Navigate results |
| <kbd>Space</kbd> | Toggle selected archive/member node |
| <kbd>←</kbd> / <kbd>→</kbd> | Collapse / expand selected archive/member node |
| <kbd>r</kbd> | Change root (directory or `.zip`) |
| <kbd>m</kbd> | Cycle SMART → LITERAL → REGEX |
| <kbd>c</kbd> | Toggle case sensitivity |
| <kbd>f</kbd> | Open filters and limits |
| <kbd>x</kbd> | Show recoverable issues |
| <kbd>e</kbd> | Export current results as JSONL, CSV, and text files |
| <kbd>Ctrl-C</kbd> | Cancel an active search |
| <kbd>?</kbd> / <kbd>q</kbd> | Help / quit |

Comma-separated TUI query components become separate patterns. Root changes preserve the query and settings but discard stale results. Query history is bounded by the entries accumulated in the session and returns to the current editable draft after the newest entry.

## Data and archive support

| Category | Supported input | Handling |
| --- | --- | --- |
| Container | `.zip` | Direct root archive or recursive directory discovery; nested ZIP members up to configured depth |
| Text-like members | `.txt`, `.csv`, `.tsv`, `.log`, `.json`, `.jsonl`, `.xml`, `.html`, `.htm`, `.md`, `.rst`, `.yaml`, `.yml`, `.ini`, `.cfg`, `.conf`, `.dat`, `.tad`, `.cpy` | Line-oriented decoding and search |
| SQLite | `.db`, `.sqlite`, `.sqlite3` | Read-only table rows rendered as searchable records |
| XLSX | `.xlsx` | Worksheet XML rows and shared strings rendered as searchable records |

By default, members outside those extensions are skipped. `--extension EXT` selects extensions explicitly; extension, include, and exclude filters are applied to member paths. Text decoding is `auto`: UTF-8 when possible, otherwise CP1251; pass `--encoding CODEC` when the source encoding is known. A NUL byte in the initial text probe causes the member to be skipped as binary.

Nested ZIP paths are shown with `!`, for example `outer.zip:inner.zip!records.txt:42` in CLI output.

## CLI options and limits

### Common search controls

| Option | Default | Meaning |
| --- | ---: | --- |
| `--max-matches` | `1000` | Global output/result cap |
| `-C`, `--context` | `0` | Lines shown before and after a matched text record |
| `--encoding` | `auto` | `auto` or any Python codec name |
| `-j`, `--workers` | `4` | Archive worker threads |
| `--nested-depth` | `2` | Nested ZIP depth; `0` disables nested ZIP traversal |
| `--include GLOB` | — | Require a matching member path or basename; repeatable |
| `--exclude GLOB` | — | Skip matching member paths or basenames; repeatable |
| `--extension EXT` | — | Scan only selected extension(s); repeatable |
| `--no-recursive` | off | Do not descend below the input directory |
| `--jsonl` | off | Write match objects and final summary as JSON Lines |
| `-q`, `--quiet` | off | Suppress human summary and warnings |

### Resource limits

| Option | Default | Checked before member decompression |
| --- | ---: | --- |
| `--max-member-mib` | `512` MiB | Declared size of one member |
| `--max-total-mib` | `2048` MiB | Declared expanded size of an archive |
| `--max-members` | `100000` | Entries in one archive |
| `--max-ratio` | `200.0` | Declared compression ratio of a member |

The TUI exposes equivalent settings: extensions, include/exclude globs, encoding, context, nesting depth, workers, cap, and the same safety limits.

## Output, export, and failure handling

Human CLI matches are written as `archive:member:line: text`; context lines use `-` and `+`. `--jsonl` emits a JSON object per match followed by one summary object. Human diagnostics and summaries are written to standard error. The TUI export command writes timestamped `.jsonl`, `.csv`, and `.txt` files in the current working directory.

Malformed archives, unreadable/unsupported members, decoding or parser failures, unsafe paths, encrypted members, and limit violations are reported as recoverable issues where possible; unrelated archives continue scanning. CLI exit status is `0` without issues, `1` when recoverable issues occurred, `2` for invalid configuration, and `130` after Ctrl-C.

## Storage behavior

ZipSearch never calls `extractall`. Ordinary text members are opened from the archive and read incrementally. SQLite requires random access, so that member alone is copied to an automatically removed temporary directory and opened read-only. XLSX and nested ZIP processing use spooled temporary files that remain in memory up to 8 MiB before using the system temporary area. Archive metadata shown by `inspect` comes from the central directory and does not decompress members.

## How it works

```text
root path → deterministic ZIP discovery → bounded archive workers
         → central-directory safety checks → member decoder/parser
         → LITERAL / REGEX / SMART matching and evidence attribution
         → Match records → CLI rendering, JSONL, or TUI tree/detail/export
```

Directory discovery does not follow directory symlinks. Archive scanning uses a fixed-size thread pool and keeps at most two worker windows queued. SMART results are retained and ranked with a bounded top-N process so stronger later matches can displace weaker earlier ones.

## Performance

Speed depends on archive count, compressed and expanded sizes, member formats, storage latency, query mode, filters, compression ratio, and worker count. The repository includes a small reproducible smoke benchmark; it is not a performance claim:

```console
python benchmarks/benchmark.py
```

For a generated functional dataset exercised through the public CLI:

```console
python benchmarks/validate_realistic_dataset.py
```

<details>
<summary>Operational notes</summary>

Use extension/include/exclude filters to avoid decoding irrelevant members. Increasing `--workers` may help across many archives on suitable storage, but it also increases concurrent I/O and decompression. Treat raised size/ratio limits as a trust decision for the input collection.

</details>

## Limitations

- ZIP is the only archive container supported.
- Encrypted ZIP members are skipped; password input is not implemented.
- XLSX handling reads worksheet values, not workbook formatting or formulas as a spreadsheet application would.
- SQLite and XLSX processing create temporary data as described above.
- Text auto-detection is intentionally limited to UTF-8 then CP1251; use `--encoding` for other known encodings.
- Safety checks reduce exposure to hostile archives but do not replace operating-system isolation for untrusted input.

## Development

```console
python -m pip install -e . pytest ruff build
pytest
ruff check .
python -m build
```

Continuous integration runs the test suite and Ruff on Python 3.10, 3.11, and 3.12. See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidance.

## Repository layout

```text
src/zipsearch/        package: engine, search modes, CLI, renderer, TUI
tests/                engine, CLI, controller, and PTY TUI coverage
benchmarks/           reproducible smoke and generated-dataset checks
CONTRIBUTING.md       contribution guidance
LICENSE               MIT license
```

## License

Released under the [MIT License](LICENSE).
