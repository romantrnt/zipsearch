from __future__ import annotations

from pathlib import Path

from zipsearch.state import LocalState, load, save
from zipsearch.tui import ZipSearchTui


def test_local_state_is_bounded_atomic_and_corruption_tolerant(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = LocalState(history=[str(number) for number in range(250)], roots=["/corpus"])
    save(path, state)
    loaded = load(path)
    assert loaded.history == [str(number) for number in range(50, 250)]
    path.write_text("not json", encoding="utf-8")
    assert load(path) == LocalState()


def test_tui_state_is_optional_and_persists_history_and_roots(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "state.json"
    app = ZipSearchTui(tmp_path, state_path=state_path)
    monkeypatch.setattr(app.session, "start", lambda root, options: 1)
    app.query = "needle"
    app.run_search()
    app.apply_root(str(tmp_path))
    restored = ZipSearchTui(tmp_path, state_path=state_path)
    assert "needle" in restored.history
    assert tmp_path in restored.root_history
