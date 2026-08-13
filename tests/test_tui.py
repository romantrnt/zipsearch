from __future__ import annotations

import time
import zipfile
from pathlib import Path

from zipsearch.models import Match
from zipsearch.tui import ZipSearchTui


class RecordingScreen:
    """Minimal curses-window stand-in that catches shifted addstr arguments."""

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        assert isinstance(y, int)
        assert isinstance(x, int)
        assert isinstance(text, str)



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
