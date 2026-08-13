from __future__ import annotations

import time
import zipfile
from pathlib import Path

from zipsearch import tui
from zipsearch.models import Match
from zipsearch.tui import ZipSearchTui


class RecordingScreen:
    """Minimal curses-window stand-in that catches shifted addstr arguments."""

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        assert isinstance(y, int)
        assert isinstance(x, int)
        assert isinstance(text, str)


class AttributeScreen(RecordingScreen):
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, str, int]] = []

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        super().addstr(y, x, text, attr)
        self.calls.append((y, x, text, attr))


class GridScreen(AttributeScreen):
    def __init__(self, height: int = 28, width: int = 80) -> None:
        super().__init__()
        self.height, self.width = height, width
        self.grid = [[" " for _ in range(width)] for _ in range(height)]

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def erase(self) -> None:
        self.grid = [[" " for _ in range(self.width)] for _ in range(self.height)]

    def box(self) -> None:
        pass

    def refresh(self) -> None:
        pass

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        super().addstr(y, x, text, attr)
        if 0 <= y < self.height:
            for offset, char in enumerate(text):
                if 0 <= x + offset < self.width:
                    self.grid[y][x + offset] = char



def wait_for_terminal_state(app: ZipSearchTui) -> None:
    deadline = time.monotonic() + 5
    while app.state == "SEARCHING" and time.monotonic() < deadline:
        app.drain()
        time.sleep(0.01)
    app.drain()


def test_tui_controller_runs_real_engine_and_matches_expected_result(tmp_path: Path) -> None:
    archive = tmp_path / "данные.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("данные/люди.txt", "до\nСкрепкин Игорь\nпосле\n")
    app = ZipSearchTui(tmp_path)
    app.query = "Скрепкин Игорь"
    app.run_search()
    assert app.state == "SEARCHING"
    wait_for_terminal_state(app)
    assert app.state == "COMPLETE"
    assert len(app.results) == 1
    assert app.selected_match() is not None
    assert app.selected_match().member == "данные/люди.txt"


def test_tui_regex_no_match_cancel_repeat_and_filters(tmp_path: Path) -> None:
    archive = tmp_path / "records.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("records.csv", "name,email\nИгорь Скрепкин,x@synthetic.invalid\n")
    app = ZipSearchTui(tmp_path)
    app.query, app.mode = r"Игорь\s+Скрепкин", "REGEX"
    app.run_search()
    wait_for_terminal_state(app)
    assert len(app.results) == 1
    app.query, app.mode = "absent-value", "SMART"
    app.run_search()
    wait_for_terminal_state(app)
    assert app.state == "COMPLETE" and not app.results
    app.query = "Игорь"
    app.run_search()
    app.cancel()
    assert app.state == "CANCELLED"
    app.query = "Скрепкин"
    app.run_search()
    wait_for_terminal_state(app)
    assert app.state == "COMPLETE" and len(app.results) == 1


def test_tui_filters_and_export(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "records.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("records.jsonl", '{"name":"Аркадий"}\n')
    app = ZipSearchTui(tmp_path)
    app.settings[0][1] = "jsonl"
    app.query = "Аркадий"
    app.run_search()
    wait_for_terminal_state(app)
    monkeypatch.chdir(tmp_path)
    message = app.export()
    assert "Exported 1" in message
    assert {item.suffix for item in tmp_path.glob("zipsearch-results-*")} == {
        ".jsonl",
        ".csv",
        ".txt",
    }


def test_tui_root_switch_invalidates_stale_results_and_keeps_query(tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    with zipfile.ZipFile(first / "first.zip", "w") as output:
        output.writestr("one.txt", "first needle")
    with zipfile.ZipFile(second / "second.zip", "w") as output:
        output.writestr("two.txt", "second needle")
    app = ZipSearchTui(first)
    app.query = "needle"
    app.run_search()
    wait_for_terminal_state(app)
    assert app.results and "first" in app.results[0].text
    assert app.apply_root(str(second))
    assert app.root == second.resolve()
    assert app.query == "needle" and not app.results and app.state == "IDLE"
    app.run_search()
    wait_for_terminal_state(app)
    assert app.results and "second" in app.results[0].text
    assert app.apply_root(str(second / "second.zip"))
    assert app.root == (second / "second.zip").resolve()
    app.run_search()
    wait_for_terminal_state(app)
    assert app.results and "second" in app.results[0].text
    assert not app.apply_root(str(tmp_path / "missing"))
    assert app.root == (second / "second.zip").resolve()


def test_tui_enter_detail_overlay_draws_without_shifted_addstr_arguments() -> None:
    """Enter opens the detail overlay; drawing it must pass y, x, text, attrs in order."""
    app = ZipSearchTui()
    app.results = [
        Match(
            archive="fixture.zip",
            member="records/people.csv",
            line=17,
            text="Скрепкин Игорь",
            patterns=("Скрепкин",),
        )
    ]
    app._rebuild_rows()
    app.editing = False
    assert app.handle(10)
    assert app.overlay == "detail"
    app._draw_overlay(RecordingScreen(), height=30, width=120)  # type: ignore[arg-type]


def test_detail_keeps_full_query_and_emphasizes_only_engine_evidence() -> None:
    app = ZipSearchTui()
    app.active_query = "Глеб Скрепкин +79087562342"
    app.results = [
        Match(
            archive="fixture.zip", member="people.txt", line=1,
            text="Игорь Скрепкин +7 (908) 756-23-42",
            patterns=(app.active_query,), text_spans=((6, 14), (15, 32)),
            query_spans=((5, 13), (14, 26)),
        )
    ]
    app._rebuild_rows()
    screen = AttributeScreen()
    app._draw_detail(screen, top=0, left=0, width=80, height=12)  # type: ignore[arg-type]
    query_calls = [call for call in screen.calls if call[0] == 3]
    attrs = {x: attr for _, x, text, attr in query_calls for x in range(x, x + len(text))}
    # "Глеб" starts after the fixed "matched  " label and stays plain.
    assert attrs[9] == 0
    assert attrs[14] & __import__("curses").A_UNDERLINE
    assert attrs[23] & __import__("curses").A_UNDERLINE


def test_color_fallback_and_selected_match_combine_safely(monkeypatch) -> None:
    import curses

    app = ZipSearchTui()
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    app._init_colors()
    assert app.color_attr == 0
    app.color_attr = 32
    app.results = [Match("a", "b", 1, "needle", ("needle",), text_spans=((0, 6),))]
    app._rebuild_rows()
    screen = AttributeScreen()
    app._draw_results(screen, top=0, left=0, width=30, rows=1)  # type: ignore[arg-type]
    match_attr = next(attr for _, _, text, attr in screen.calls if text == "n")
    assert match_attr & curses.A_REVERSE
    assert match_attr & curses.A_UNDERLINE
    assert match_attr & 32


def test_color_capable_terminal_initializes_one_default_background_pair(monkeypatch) -> None:
    import curses

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: calls.append(("start",)))
    monkeypatch.setattr(curses, "use_default_colors", lambda: calls.append(("default",)))
    monkeypatch.setattr(curses, "init_pair", lambda *args: calls.append(("pair", *args)))
    monkeypatch.setattr(curses, "color_pair", lambda number: number << 8)
    app = ZipSearchTui()
    app._init_colors()
    app._init_colors()
    assert calls == [("start",), ("default",), ("pair", 1, curses.COLOR_CYAN, -1)]
    assert app.color_attr == 1 << 8


def test_active_search_timer_increases_while_session_uptime_is_independent(monkeypatch) -> None:
    now = [10.0]
    monkeypatch.setattr(tui.time, "perf_counter", lambda: now[0])
    app = ZipSearchTui()
    assert app.session_uptime() == 0
    app.search_started = 12.0
    now[0] = 15.5
    assert app.search_duration() == 3.5
    assert app.session_uptime() == 5.5


def test_completed_search_duration_freezes_while_session_uptime_continues(monkeypatch) -> None:
    now = [20.0]
    monkeypatch.setattr(tui.time, "perf_counter", lambda: now[0])
    app = ZipSearchTui()
    app.state, app.search_started = "SEARCHING", 22.0
    now[0] = 26.0
    app.session.events.put(("done", None, app.generation))
    app.drain()
    assert app.state == "COMPLETE" and app.search_duration() == 4.0
    now[0] = 40.0
    assert app.search_duration() == 4.0
    assert app.session_uptime() == 20.0


def test_cancelled_search_duration_freezes(monkeypatch) -> None:
    now = [30.0]
    monkeypatch.setattr(tui.time, "perf_counter", lambda: now[0])
    app = ZipSearchTui()
    app.state, app.search_started = "SEARCHING", 31.0
    now[0] = 35.0
    app.cancel()
    now[0] = 44.0
    assert app.state == "CANCELLED" and app.search_duration() == 4.0


def test_new_search_resets_only_search_duration(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr(tui.time, "perf_counter", lambda: now[0])
    app = ZipSearchTui()
    monkeypatch.setattr(app.session, "start", lambda root, options: 1)
    app.query = "needle"
    now[0] = 110.0
    app.run_search()
    now[0] = 115.0
    app.cancel()
    assert app.search_duration() == 5.0
    now[0] = 130.0
    app.run_search()
    assert app.search_duration() == 0.0
    assert app.session_uptime() == 30.0


def _tree_app() -> ZipSearchTui:
    app = ZipSearchTui()
    app.results = [
        Match("one.zip", "first.txt", 1, "one first", ("one",)),
        Match("one.zip", "first.txt", 2, "one second", ("one",)),
        Match("one.zip", "nested/second.txt", 3, "one nested", ("one",)),
        Match("two.zip", "third.txt", 4, "two third", ("two",)),
    ]
    app._rebuild_rows()
    app.editing = False
    return app


def _select(app: ZipSearchTui, kind: str, text: str) -> None:
    app.selected = next(
        index for index, row in enumerate(app.rows) if row.kind == kind and row.text == text
    )


def test_history_navigation_bounds_preserve_editable_draft() -> None:
    import curses

    app = ZipSearchTui()
    assert app.handle(curses.KEY_UP) and app.query == ""
    assert app.handle(curses.KEY_DOWN) and app.query == ""
    app.history = ["old", "new"]
    app.query = "draft"
    for _ in range(4):
        app.handle(curses.KEY_UP)
    assert app.history_index == 0 and app.query == "old"
    app.handle(curses.KEY_DOWN)
    assert app.history_index == 1 and app.query == "new"
    for _ in range(4):
        app.handle(curses.KEY_DOWN)
    assert app.history_index is None and app.query == "draft"


def test_member_collapse_expand_and_navigation_only_use_visible_rows() -> None:
    import curses

    app = _tree_app()
    _select(app, "member", "first.txt")
    app.handle(" ")
    assert [row.text for row in app.rows if row.kind == "match"] == ["one nested", "two third"]
    assert app.selected_row() and app.selected_row().kind == "member"
    app.handle(curses.KEY_DOWN)
    assert app.selected_row() and app.selected_row().text == "nested/second.txt"
    _select(app, "member", "first.txt")
    app.handle(curses.KEY_RIGHT)
    assert [row.text for row in app.rows if row.kind == "match"] == [
        "one first", "one second", "one nested", "two third"
    ]


def test_archive_collapse_hides_nested_descendants_without_restarting_search() -> None:
    import curses

    app = _tree_app()
    _select(app, "archive", "one.zip")
    generation, results = app.generation, list(app.results)
    app.handle(curses.KEY_LEFT)
    assert [row.text for row in app.rows] == ["one.zip", "two.zip", "third.txt", "two third"]
    assert app.selected == 0 and app.generation == generation and app.results == results
    app.handle(curses.KEY_RIGHT)
    assert any(row.text == "nested/second.txt" for row in app.rows)


def test_enter_on_archive_and_member_opens_node_detail() -> None:
    app = _tree_app()
    _select(app, "archive", "one.zip")
    assert app.handle(10) and app.overlay == "detail"
    screen = AttributeScreen()
    app._draw_overlay(screen, height=30, width=120)  # type: ignore[arg-type]
    assert any("type     ZIP archive" in text for _, _, text, _ in screen.calls)
    app.overlay = None
    _select(app, "member", "first.txt")
    assert app.handle(10) and app.overlay == "detail"
    screen = AttributeScreen()
    app._draw_overlay(screen, height=30, width=120)  # type: ignore[arg-type]
    assert any("results  2" in text for _, _, text, _ in screen.calls)


def test_results_never_overwrite_divider_when_scrolling_long_unicode_tree() -> None:
    app = ZipSearchTui()
    app.results = [
        Match(
            "very-long-archive-name.zip",
            f"nested/member-{number}.txt",
            number,
            "Скрепкин 漢字 " + ("очень-длинная-запись-" * 12),
            ("Скрепкин",),
            text_spans=((0, 8),),
        )
        for number in range(1, 60)
    ]
    app._rebuild_rows()
    app.editing = False
    app.selected = len(app.rows) - 1
    screen = GridScreen()
    app.draw(screen)  # type: ignore[arg-type]
    split = max(34, screen.width * 3 // 5)
    assert all(screen.grid[y][split] == "│" for y in range(3, screen.height - 3))
    # No left-pane draw call reaches the separator; the final divider redraw is
    # a belt-and-suspenders safeguard for terminal-specific wide glyph behavior.
    assert all(
        x >= split or x + len(text) <= split
        for y, x, text, _ in screen.calls
        if 3 <= y < screen.height - 3 and text != "│"
    )
    archive_index = next(index for index, row in enumerate(app.rows) if row.kind == "archive")
    app.selected = archive_index
    app.handle(" ")
    app.handle(" ")
    app.selected = len(app.rows) - 1
    screen.calls.clear()
    app.draw(screen)  # type: ignore[arg-type]
    assert all(screen.grid[y][split] == "│" for y in range(3, screen.height - 3))
