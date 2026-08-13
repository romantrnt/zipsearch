"""A compact, keyboard-first curses frontend for the shared ZipSearch engine."""

from __future__ import annotations

import csv
import curses
import json
import locale
import queue
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .engine import SearchConfigurationError, discover_archives, rank_matches, scan_archives
from .models import ArchiveScan, Issue, Match, SafetyLimits, SearchOptions, Summary


def _patterns(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _number(value: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except ValueError:
        return default


@dataclass(frozen=True)
class Row:
    kind: str
    text: str
    match: Match | None = None
    archive: str = ""
    member: str = ""
    nested_path: tuple[str, ...] = ()


class SearchSession:
    """Thread-safe search controller; it contains no terminal-rendering code."""

    def __init__(self) -> None:
        self.events: queue.Queue[tuple[str, object, int]] = queue.Queue()
        self.generation = 0
        self.cancelled = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self, root: Path, options: SearchOptions) -> int:
        self.cancel()
        self.generation += 1
        generation = self.generation
        self.cancelled = threading.Event()

        def runner() -> None:
            try:
                paths = discover_archives(root, recursive=True)
                for scan in scan_archives(iter(paths), options, self.cancelled):
                    if self.cancelled.is_set():
                        break
                    self.events.put(("scan", scan, generation))
            except Exception as exc:  # configuration and unexpected worker failures must be visible
                self.events.put(("error", str(exc), generation))
            finally:
                self.events.put(("done", None, generation))

        self.thread = threading.Thread(target=runner, name="zipsearch-tui", daemon=True)
        self.thread.start()
        return generation

    def cancel(self) -> None:
        self.cancelled.set()


class ZipSearchTui:
    """Dense terminal UI inspired by classic operational tools, not GUI widgets."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path.cwd()
        self.query = ""
        self.mode = "SMART"
        self.editing = True
        self.state = "IDLE"
        self.message = "Type a query and press Enter. Commas add patterns."
        self.options = SearchOptions(patterns=("_",))
        self.session = SearchSession()
        self.generation = 0
        self.results: list[Match] = []
        self.issues: list[Issue] = []
        self.summary = Summary()
        self.rows: list[Row] = []
        self.collapsed_archives: set[str] = set()
        self.collapsed_members: set[tuple[str, str, tuple[str, ...]]] = set()
        self.archive_scans: dict[str, ArchiveScan] = {}
        self.selected = 0
        self.offset = 0
        self.session_started = time.perf_counter()
        self.search_started: float | None = None
        self.search_elapsed = 0.0
        self.overlay: str | None = None
        self.filter_index = 0
        self.history: list[str] = []
        self.history_index: int | None = None
        self.history_draft: str | None = None
        self.root_history: list[Path] = [self.root]
        self.root_history_index: int | None = None
        self.root_buffer = str(self.root)
        self.active_query = ""
        self.color_attr = 0
        self._colors_initialized = False
        self.settings: list[list[str]] = [
            ["extensions", ""],
            ["include glob", ""],
            ["exclude glob", ""],
            ["encoding", "auto"],
            ["context", "0"],
            ["nested depth", "2"],
            ["workers", "4"],
            ["result cap", "1000"],
            ["max member MiB", "512"],
            ["max total MiB", "2048"],
            ["max members", "100000"],
            ["max ratio", "200"],
        ]

    def build_options(self) -> SearchOptions:
        patterns = _patterns(self.query)
        if not patterns:
            raise SearchConfigurationError("query is empty")
        values = {key: value for key, value in self.settings}
        regex = self.mode == "REGEX"
        smart = self.mode == "SMART"
        case = getattr(self, "case", False)
        flags = 0 if case else re.IGNORECASE
        if regex:
            for pattern in patterns:
                re.compile(pattern, flags)
        return SearchOptions(
            patterns=patterns,
            regex=regex,
            smart=smart,
            case_sensitive=case,
            include=_patterns(values["include glob"]),
            exclude=_patterns(values["exclude glob"]),
            extensions=_patterns(values["extensions"]),
            context=_number(values["context"], 0),
            encoding=values["encoding"] or "auto",
            workers=_number(values["workers"], 4, 1),
            max_matches=_number(values["result cap"], 1000, 1),
            limits=SafetyLimits(
                max_member_bytes=_number(values["max member MiB"], 512, 1) * 1024 * 1024,
                max_total_bytes=_number(values["max total MiB"], 2048, 1) * 1024 * 1024,
                max_members=_number(values["max members"], 100000, 1),
                max_compression_ratio=max(0.1, float(values["max ratio"] or 200)),
                max_nested_depth=_number(values["nested depth"], 2),
            ),
        )

    def run_search(self) -> None:
        try:
            self.options = self.build_options()
            if not self.root.exists():
                raise SearchConfigurationError(f"path does not exist: {self.root}")
        except (SearchConfigurationError, re.error, ValueError) as exc:
            self.state, self.message = "ERROR", str(exc)
            return
        if self.query not in self.history:
            self.history.append(self.query)
        self.active_query = self.query
        self.history_index = None
        self.results, self.issues, self.rows = [], [], []
        self.collapsed_archives, self.collapsed_members, self.archive_scans = set(), set(), {}
        self.summary, self.selected, self.offset = Summary(), 0, 0
        self.search_started = time.perf_counter()
        self.search_elapsed = 0.0
        self.state, self.message = "SEARCHING", "Scanning archives…"
        self.generation = self.session.start(self.root, self.options)

    def search_duration(self) -> float:
        """Return the live duration of an active search or its final frozen value."""
        if self.search_started is None:
            return self.search_elapsed
        return time.perf_counter() - self.search_started

    def session_uptime(self) -> float:
        """Return the TUI lifetime; it intentionally never resets between searches."""
        return time.perf_counter() - self.session_started

    def _freeze_search_duration(self) -> None:
        if self.search_started is not None:
            self.search_elapsed = time.perf_counter() - self.search_started
            self.search_started = None

    def cancel(self) -> None:
        if self.state == "SEARCHING":
            self.session.cancel()
            self._freeze_search_duration()
            self.state, self.message = "CANCELLED", "Search cancelled; safe cleanup in progress."

    def apply_root(self, value: str) -> bool:
        """Validate and switch roots without losing query/options or retaining stale hits."""
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidate = candidate.resolve()
        if not candidate.exists():
            self.message = f"Invalid root: {candidate} does not exist"
            return False
        if candidate.is_file() and candidate.suffix.lower() != ".zip":
            self.message = f"Invalid root: {candidate} is not a ZIP archive"
            return False
        if not candidate.is_dir() and not candidate.is_file():
            self.message = f"Invalid root: {candidate} is not searchable"
            return False
        if self.state == "SEARCHING":
            self._freeze_search_duration()
        self.session.cancel()
        self.generation = -1
        self.root = candidate
        self.root_buffer = str(candidate)
        if candidate not in self.root_history:
            self.root_history.append(candidate)
        self.root_history_index = None
        self.results, self.issues, self.rows = [], [], []
        self.collapsed_archives, self.collapsed_members, self.archive_scans = set(), set(), {}
        self.summary, self.selected, self.offset = Summary(), 0, 0
        self.state, self.message = "IDLE", "Root changed. Press Enter to search this root."
        return True

    def drain(self) -> None:
        """Accept up to eight archive completions per UI refresh."""
        for _ in range(8):
            try:
                kind, payload, generation = self.session.events.get_nowait()
            except queue.Empty:
                return
            if generation != self.generation:
                continue
            if kind == "scan":
                scan = payload
                assert isinstance(scan, ArchiveScan)
                self.summary.archives_seen += 1
                self.summary.archives_completed += 1
                self.summary.members_seen += scan.members_seen
                self.summary.members_scanned += scan.members_scanned
                self.issues.extend(scan.issues)
                self.archive_scans[str(scan.archive)] = scan
                if self.options.smart:
                    self.results.extend(scan.matches)
                    if len(self.results) > self.options.max_matches * 2:
                        self.results = rank_matches(iter(self.results), self.options.max_matches)
                else:
                    remaining = self.options.max_matches - len(self.results)
                    self.results.extend(scan.matches[:remaining])
                self._rebuild_rows()
            elif kind == "error":
                self._freeze_search_duration()
                self.state, self.message = "ERROR", str(payload)
            else:
                if self.state == "SEARCHING":
                    if self.options.smart:
                        self.results = rank_matches(iter(self.results), self.options.max_matches)
                        self._rebuild_rows()
                    self._freeze_search_duration()
                    self.state = "COMPLETE"
                    self.message = "No matches." if not self.results else "Search complete."
        self.summary.matches, self.summary.issues = len(self.results), len(self.issues)

    def _rebuild_rows(self) -> None:
        selected_key = self._row_key(self.selected_row())
        rows: list[Row] = []
        groups: dict[tuple[str, str, tuple[str, ...]], list[Match]] = {}
        for match in self.results:
            groups.setdefault((match.archive, match.member, match.nested_path), []).append(match)
        ordered_groups = sorted(
            groups.items(),
            key=lambda item: (
                -max(match.score for match in item[1]) if self.options.smart else 0,
                item[0],
            ),
        )
        previous_archive = None
        for (archive, member, nested_path), matches in ordered_groups:
            if archive != previous_archive:
                rows.append(Row("archive", Path(archive).name, archive=archive))
                previous_archive = archive
            if archive in self.collapsed_archives:
                continue
            display_member = " ! ".join((*nested_path, member))
            member_key = (archive, member, nested_path)
            rows.append(
                Row(
                    "member",
                    display_member,
                    archive=archive,
                    member=member,
                    nested_path=nested_path,
                )
            )
            if member_key in self.collapsed_members:
                continue
            matches.sort(
                key=lambda match: (
                    (-match.score, match.line) if self.options.smart else (match.line,)
                )
            )
            rows.extend(
                Row("match", match.text, match, archive, member, nested_path) for match in matches
            )
        self.rows = rows
        restored = next(
            (index for index, row in enumerate(rows) if self._row_key(row) == selected_key), None
        )
        if restored is not None:
            self.selected = restored
        elif self.selected >= len(rows):
            self.selected = max(0, len(rows) - 1)
        if selected_key is None and rows and self.selected == 0 and rows[0].kind != "match":
            self.selected = next((index for index, row in enumerate(rows) if row.match), 0)

    def selected_match(self) -> Match | None:
        if 0 <= self.selected < len(self.rows):
            return self.rows[self.selected].match
        return None

    def selected_row(self) -> Row | None:
        return self.rows[self.selected] if 0 <= self.selected < len(self.rows) else None

    @staticmethod
    def _row_key(row: Row | None) -> tuple[object, ...] | None:
        if row is None:
            return None
        return (
            row.kind,
            row.archive,
            row.member,
            row.nested_path,
            row.match.line if row.match else None,
        )

    def _toggle_tree(self, expand: bool | None = None) -> None:
        row = self.selected_row()
        if row is None or row.kind == "match":
            return
        target = self.collapsed_archives if row.kind == "archive" else self.collapsed_members
        key: str | tuple[str, str, tuple[str, ...]] = (
            row.archive if row.kind == "archive" else (row.archive, row.member, row.nested_path)
        )
        if expand is True:
            target.discard(key)
        elif expand is False:
            target.add(key)
        elif key in target:
            target.remove(key)
        else:
            target.add(key)
        self._rebuild_rows()

    def export(self) -> str:
        if not self.results:
            return "No results to export."
        base = Path.cwd() / f"zipsearch-results-{datetime.now():%Y%m%d-%H%M%S}"
        with base.with_suffix(".jsonl").open("w", encoding="utf-8") as output:
            for item in self.results:
                output.write(json.dumps(item.as_dict(), ensure_ascii=False) + "\n")
        with base.with_suffix(".txt").open("w", encoding="utf-8") as output:
            for item in self.results:
                output.write(f"{item.archive}:{item.member}:{item.line}: {item.text}\n")
        with base.with_suffix(".csv").open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(("archive", "member", "line", "text", "patterns"))
            writer.writerows(
                (m.archive, m.member, m.line, m.text, ";".join(m.patterns)) for m in self.results
            )
        return f"Exported {len(self.results)} results: {base.name}.{{jsonl,csv,txt}}"

    def draw(self, screen: curses.window) -> None:
        self._init_colors()
        height, width = screen.getmaxyx()
        screen.erase()
        if height < 12 or width < 50:
            self._safe_add(screen, 0, 0, "Terminal too small — resize to at least 50×12.")
            screen.refresh()
            return
        screen.box()
        root = str(self.root)
        mode = self.mode
        case = "ON" if getattr(self, "case", False) else "OFF"
        header = f" ZipSearch  root:{root[: max(10, width - 54)]}  mode:{mode} case:{case} "
        self._safe_add(screen, 0, 2, header, curses.A_BOLD)
        self._safe_add(
            screen,
            1,
            2,
            "query> " + self.query,
            curses.A_REVERSE if self.editing else curses.A_BOLD,
        )
        split = max(34, width * 3 // 5)
        self._safe_add(screen, 2, 2, " RESULTS ", curses.A_BOLD)
        self._safe_add(screen, 2, split + 2, " DETAIL ", curses.A_BOLD)
        self._draw_results(screen, 3, 1, split - 2, height - 7)
        self._draw_detail(screen, 3, split + 2, width - split - 3, height - 7)
        # Pane content is always clipped, but redraw this structural boundary last
        # as well: terminals may render a wide glyph differently from Python's
        # code-point indexing.
        self._draw_divider(screen, split, 3, height - 3)
        search_duration = self.search_duration()
        session_uptime = self.session_uptime()
        cap = " CAP" if len(self.results) >= self.options.max_matches else ""
        archives = f"A:{self.summary.archives_completed}/{self.summary.archives_seen}"
        members = f"M:{self.summary.members_scanned}/{self.summary.members_seen}"
        runtime = (
            f"E:{len(self.issues)} S:{search_duration:5.1f}s "
            f"U:{session_uptime:5.1f}s J:{self.options.workers}"
        )
        hits = f"H:{len(self.results)}{cap}"
        status = f" {self.state:<10} {archives} {members} {hits} {runtime}"
        status += f" D:{self.options.limits.max_nested_depth} "
        self._safe_add(screen, height - 3, 2, status[: width - 4], curses.A_REVERSE)
        self._safe_add(
            screen,
            height - 2,
            2,
            (
                "/ query  Enter run/detail  r root  m mode  c case  f filters  "
                "x errors  e export  Ctrl-C cancel  ? help  q quit"
            )[: width - 4],
            curses.A_DIM,
        )
        if self.overlay:
            self._draw_overlay(screen, height, width)
        screen.refresh()

    def _draw_divider(self, screen: curses.window, x: int, top: int, bottom: int) -> None:
        for y in range(top, bottom):
            self._safe_add(screen, y, x, "│")

    def _draw_results(
        self, screen: curses.window, top: int, left: int, width: int, rows: int
    ) -> None:
        if self.selected < self.offset:
            self.offset = self.selected
        if self.selected >= self.offset + rows:
            self.offset = self.selected - rows + 1
        for visual, index in enumerate(range(self.offset, min(len(self.rows), self.offset + rows))):
            row = self.rows[index]
            attr = curses.A_REVERSE if index == self.selected else 0
            if row.kind == "archive":
                marker = "▸" if row.archive in self.collapsed_archives else "▾"
                self._safe_add_clipped(
                    screen,
                    top + visual,
                    left,
                    marker + " " + row.text,
                    attr | curses.A_BOLD,
                    width,
                )
            elif row.kind == "member":
                key = (row.archive, row.member, row.nested_path)
                marker = "▸" if key in self.collapsed_members else "▾"
                self._safe_add_clipped(
                    screen,
                    top + visual,
                    left + 2,
                    marker + " " + row.text,
                    attr | curses.A_DIM,
                    width - 2,
                )
            else:
                prefix = f"  {row.match.line:>6} " if row.match else ""
                text_left = left + 2
                self._safe_add_clipped(screen, top + visual, text_left, prefix, attr, width - 2)
                self._draw_highlight(
                    screen,
                    top + visual,
                    text_left + self._cell_width(prefix),
                    row.match,
                    width - 2 - self._cell_width(prefix),
                    attr,
                )

    def _init_colors(self) -> None:
        """Add one optional match accent without assuming a terminal background."""
        if self._colors_initialized:
            return
        self._colors_initialized = True
        try:
            if not curses.has_colors():
                return
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            self.color_attr = curses.color_pair(1)
        except curses.error:
            self.color_attr = 0

    def _draw_highlight(
        self, screen: curses.window, y: int, x: int, match: Match | None, width: int, base: int
    ) -> None:
        if not match:
            return
        self._draw_spans(screen, y, x, match.text, width, base, match.text_spans)

    def _draw_spans(
        self, screen: curses.window, y: int, x: int, text: str, width: int, base: int,
        spans: tuple[tuple[int, int], ...],
    ) -> None:
        x_offset = 0
        for index, char in enumerate(text):
            cell_width = self._cell_width(char)
            if x_offset + cell_width > max(0, width):
                break
            attr = base | (
                curses.A_BOLD | curses.A_UNDERLINE | self.color_attr
                if any(start <= index < end for start, end in spans) else 0
            )
            self._safe_add(screen, y, x + x_offset, char, attr)
            x_offset += cell_width

    def _query_spans(self, match: Match, query: str) -> tuple[tuple[int, int], ...]:
        """Translate engine offsets in a source pattern to the original query line."""
        if not match.patterns:
            return ()
        remaining = list(re.finditer(r"[^,]+", query))
        result: list[tuple[int, int]] = []
        if len(match.patterns) == 1:
            source = match.patterns[0]
            for piece in remaining:
                leading = len(piece.group()) - len(piece.group().lstrip())
                if piece.group().strip() == source:
                    return tuple(
                        (piece.start() + leading + start, piece.start() + leading + end)
                        for start, end in match.query_spans
                    )
            return ()
        # SMART has one winning pattern.  Literal/regex may have several;
        # their corresponding source-local spans are retained in order.
        for source, source_span in zip(match.patterns, match.query_spans, strict=False):
            for index, piece in enumerate(remaining):
                leading = len(piece.group()) - len(piece.group().lstrip())
                if piece.group().strip() == source:
                    result.append(
                        (
                            piece.start() + leading + source_span[0],
                            piece.start() + leading + source_span[1],
                        )
                    )
                    remaining.pop(index)
                    break
        return tuple(result)

    def _draw_detail(
        self, screen: curses.window, top: int, left: int, width: int, height: int
    ) -> None:
        row = self.selected_row()
        match = row.match if row else None
        if row is None:
            lines = [
                self.message,
                "",
                "Select a result with j/k or arrows.",
                "Press f for filters; ? for keys.",
            ]
        elif match:
            member = " ! ".join((*match.nested_path, match.member))
            query = self.active_query or self.query
            lines = [
                f"archive  {Path(match.archive).name}",
                f"member   {member}",
                f"line     {match.line}",
                "matched  " + query,
                "",
                "record",
                match.text,
            ]
            if match.context_before:
                lines += ["", "before"] + list(match.context_before)
            if match.context_after:
                lines += ["", "after"] + list(match.context_after)
        else:
            lines = self._node_detail_lines(row)
        for index, line in enumerate(lines[:height]):
            if match and index == 3:
                prefix = "matched  "
                self._safe_add_clipped(screen, top + index, left, prefix, 0, width)
                self._draw_spans(
                    screen,
                    top + index,
                    left + self._cell_width(prefix),
                    query,
                    width - self._cell_width(prefix),
                    0,
                    self._query_spans(match, query),
                )
            elif match and index == 6:
                self._draw_spans(screen, top + index, left, line, width, 0, match.text_spans)
            else:
                self._safe_add_clipped(screen, top + index, left, line, 0, width)

    def _node_detail_lines(self, row: Row) -> list[str]:
        if row.kind == "archive":
            scan = self.archive_scans.get(row.archive)
            path = Path(row.archive)
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            members = len(
                {
                    (candidate.member, candidate.nested_path)
                    for candidate in self.results
                    if candidate.archive == row.archive
                }
            )
            lines = [
                f"archive  {path.name}",
                f"path     {row.archive}",
                "type     ZIP archive",
                f"size     {size} bytes" if size is not None else "size     unavailable",
                f"members  {scan.members_seen if scan else members}",
                f"results  {sum(match.archive == row.archive for match in self.results)}",
            ]
            if scan:
                lines.append(f"expanded {scan.bytes_declared} bytes")
            return lines
        matches = [
            match
            for match in self.results
            if (match.archive, match.member, match.nested_path)
            == (row.archive, row.member, row.nested_path)
        ]
        path = Path(row.member)
        return [
            f"archive  {row.archive}",
            f"member   {' ! '.join((*row.nested_path, row.member))}",
            f"type     {path.suffix.lower() or 'unknown'}",
            "size     unavailable",
            f"results  {len(matches)}",
            f"lines    {len({match.line for match in matches})}",
        ]

    def _draw_overlay(self, screen: curses.window, height: int, width: int) -> None:
        left, top, box_width, box_height = 4, 3, width - 8, height - 6
        for y in range(top, top + box_height):
            self._safe_add(screen, y, left, " " * box_width, curses.A_REVERSE)
        title = self.overlay.upper()
        self._safe_add(screen, top, left + 2, f" {title} ", curses.A_BOLD | curses.A_REVERSE)
        if self.overlay == "root":
            self._safe_add(
                screen,
                top + 1,
                left + 2,
                "Enter applies · Esc cancels · ↑↓ recent roots",
                curses.A_DIM | curses.A_REVERSE,
            )
            self._safe_add(
                screen,
                top + 3,
                left + 2,
                "root> " + self.root_buffer[: box_width - 12],
                curses.A_BOLD | curses.A_REVERSE,
            )
            self._safe_add(
                screen, top + 5, left + 2, self.message[: box_width - 4], curses.A_REVERSE
            )
        elif self.overlay == "filters":
            self._safe_add(
                screen,
                top + 1,
                left + 2,
                "Tab/↑↓ select · type edits · Enter applies · Esc closes",
                curses.A_DIM | curses.A_REVERSE,
            )
            for index, (name, value) in enumerate(self.settings[: box_height - 3]):
                marker = ">" if index == self.filter_index else " "
                self._safe_add(
                    screen,
                    top + 2 + index,
                    left + 2,
                    f"{marker} {name:<16} {value}"[: box_width - 4],
                    curses.A_REVERSE if index == self.filter_index else 0,
                )
        elif self.overlay == "errors":
            lines = [
                f"[{i.kind}] {Path(i.archive).name} {i.member or ''}: {i.message}"
                for i in self.issues
            ] or ["No recoverable errors."]
            for index, line in enumerate(lines[: box_height - 2]):
                self._safe_add(
                    screen, top + 1 + index, left + 2, line[: box_width - 4], curses.A_REVERSE
                )
        elif self.overlay == "detail":
            row = self.selected_row()
            match = row.match if row else None
            lines = (
                ["No selection."]
                if row is None
                else (
                    [
                    f"archive: {match.archive}",
                    f"member: {' ! '.join((*match.nested_path, match.member))}",
                    f"line: {match.line}  type: {match.match_type}  score: {match.score}",
                    "",
                    match.text,
                ]
                    if match
                    else self._node_detail_lines(row)
                )
            )
            for index, line in enumerate(lines[: box_height - 2]):
                self._safe_add(
                    screen, top + 2 + index, left + 2, line[: box_width - 4], curses.A_REVERSE
                )
        else:
            lines = [
                "/  edit query",
                "Enter  run query / inspect selection",
                "j/k, arrows, PgUp/PgDn, Home/End  navigate",
                "Space toggle tree · Left collapse · Right expand",
                "r root · m search mode · c toggle case",
                "f filters · x errors · e export",
                "Ctrl-C cancel active scan · q quit",
            ]
            for index, line in enumerate(lines):
                self._safe_add(screen, top + 2 + index, left + 2, line, curses.A_REVERSE)

    @staticmethod
    def _safe_add(screen: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
        try:
            screen.addstr(y, x, text, attr)
        except curses.error:
            pass

    @staticmethod
    def _cell_width(text: str) -> int:
        return sum(
            0
            if unicodedata.combining(char)
            else 2
            if unicodedata.east_asian_width(char) in "WF"
            else 1
            for char in text
        )

    def _safe_add_clipped(
        self, screen: curses.window, y: int, x: int, text: str, attr: int, width: int
    ) -> None:
        """Draw only complete terminal cells inside one pane's reserved columns."""
        used = 0
        clipped: list[str] = []
        for char in text:
            cell_width = self._cell_width(char)
            if used + cell_width > max(0, width):
                break
            clipped.append(char)
            used += cell_width
        self._safe_add(screen, y, x, "".join(clipped), attr)

    def handle(self, key: int | str) -> bool:
        if isinstance(key, str):
            key = ord(key)
        if self.overlay:
            return self._handle_overlay(key)
        if self.editing:
            if key == ord("/") and not self.query:
                return True
            if key in (10, 13, curses.KEY_ENTER):
                self.editing = False
                self.run_search()
                return True
            if key == 27:
                self.editing = False
                return True
            if key in (curses.KEY_BACKSPACE, 127, 8):
                self.query = self.query[:-1]
                return True
            if key == curses.KEY_UP:
                if not self.history:
                    return True
                if self.history_index is None:
                    self.history_draft = self.query
                    self.history_index = len(self.history) - 1
                else:
                    self.history_index = max(0, min(self.history_index - 1, len(self.history) - 1))
                self.query = self.history[self.history_index]
                return True
            if key == curses.KEY_DOWN:
                if self.history_index is None:
                    return True
                if self.history_index >= len(self.history) - 1:
                    self.history_index = None
                    self.query = self.history_draft or ""
                    self.history_draft = None
                else:
                    self.history_index += 1
                    self.query = self.history[self.history_index]
                return True
            if 32 <= key <= 0x10FFFF:
                self.query += chr(key)
                self.history_index = None
                self.history_draft = None
                return True
            return True
        if key in (ord("q"),):
            return False
        if key == ord("/"):
            self.editing = True
        elif key in (3,):
            self.cancel()
        elif key in (10, 13, curses.KEY_ENTER):
            self.overlay = "detail" if self.selected_row() else None
            self.editing = not bool(self.overlay)
        elif key == ord(" "):
            self._toggle_tree()
        elif key == curses.KEY_LEFT:
            self._toggle_tree(expand=False)
        elif key == curses.KEY_RIGHT:
            self._toggle_tree(expand=True)
        elif key == ord("r"):
            self.root_buffer = str(self.root)
            self.overlay = "root"
        elif key == ord("m"):
            self.mode = {"SMART": "LITERAL", "LITERAL": "REGEX", "REGEX": "SMART"}[self.mode]
            self.message = f"Mode: {self.mode}"
        elif key == ord("c"):
            self.case = not getattr(self, "case", False)
            self.message = f"Case {'on' if self.case else 'off'}"
        elif key == ord("f"):
            self.overlay = "filters"
        elif key == ord("x"):
            self.overlay = "errors"
        elif key == ord("?"):
            self.overlay = "help"
        elif key == ord("e"):
            self.message = self.export()
        elif key in (ord("j"), curses.KEY_DOWN):
            self.selected = min(len(self.rows) - 1, self.selected + 1)
        elif key in (ord("k"), curses.KEY_UP):
            self.selected = max(0, self.selected - 1)
        elif key == curses.KEY_NPAGE:
            self.selected = min(len(self.rows) - 1, self.selected + 10)
        elif key == curses.KEY_PPAGE:
            self.selected = max(0, self.selected - 10)
        elif key == curses.KEY_HOME:
            self.selected = 0
        elif key == curses.KEY_END:
            self.selected = max(0, len(self.rows) - 1)
        return True

    def _handle_overlay(self, key: int) -> bool:
        if key == 27:
            self.overlay = None
            return True
        if self.overlay == "root":
            return self._handle_root_overlay(key)
        if self.overlay != "filters":
            self.overlay = None
            return True
        if key in (10, 13, curses.KEY_ENTER):
            self.overlay = None
            self.message = "Filters applied."
            return True
        if key in (9, curses.KEY_DOWN):
            self.filter_index = (self.filter_index + 1) % len(self.settings)
            return True
        if key == curses.KEY_UP:
            self.filter_index = (self.filter_index - 1) % len(self.settings)
            return True
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.settings[self.filter_index][1] = self.settings[self.filter_index][1][:-1]
            return True
        if 32 <= key <= 0x10FFFF:
            self.settings[self.filter_index][1] += chr(key)
        return True

    def _handle_root_overlay(self, key: int) -> bool:
        if key == 27:
            self.overlay = None
            return True
        if key in (10, 13, curses.KEY_ENTER):
            if self.apply_root(self.root_buffer):
                self.overlay = None
            return True
        if key == curses.KEY_UP and self.root_history:
            self.root_history_index = (
                len(self.root_history) - 1
                if self.root_history_index is None
                else max(0, self.root_history_index - 1)
            )
            self.root_buffer = str(self.root_history[self.root_history_index])
            return True
        if key == curses.KEY_DOWN and self.root_history_index is not None:
            self.root_history_index += 1
            self.root_buffer = (
                str(self.root_history[self.root_history_index])
                if self.root_history_index < len(self.root_history)
                else str(self.root)
            )
            return True
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.root_buffer = self.root_buffer[:-1]
        elif 32 <= key <= 0x10FFFF:
            self.root_buffer += chr(key)
        return True


def _curses_main(screen: curses.window, root: Path | None) -> None:
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    screen.timeout(75)
    app = ZipSearchTui(root)
    running = True
    while running:
        app.drain()
        app.draw(screen)
        try:
            key: int | str = screen.get_wch()
        except curses.error:
            key = -1
        if key != -1:
            running = app.handle(key)
    app.cancel()


def run_tui(initial_path: Path | None = None) -> int:
    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(_curses_main, initial_path)
    return 0
