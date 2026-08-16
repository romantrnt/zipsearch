"""Small optional local UI state; never part of corpus or index authority."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

STATE_VERSION = 1


def default_state_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "zipsearch" / "state.json"


@dataclass
class LocalState:
    history: list[str] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    saved_searches: list[str] = field(default_factory=list)
    bookmarks: list[dict[str, object]] = field(default_factory=list)


def load(path: Path) -> LocalState:
    """Load bounded convenience state; malformed data is simply ignored."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            return LocalState()
        return LocalState(
            history=[item for item in data.get("history", []) if isinstance(item, str)][-200:],
            roots=[item for item in data.get("roots", []) if isinstance(item, str)][-50:],
            saved_searches=[
                item for item in data.get("saved_searches", []) if isinstance(item, str)
            ][-50:],
            bookmarks=[item for item in data.get("bookmarks", []) if isinstance(item, dict)][-200:],
        )
    except (OSError, ValueError, TypeError):
        return LocalState()


def save(path: Path, state: LocalState) -> None:
    """Atomically save a capped state document with private file permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "history": state.history[-200:],
        "roots": state.roots[-50:],
        "saved_searches": state.saved_searches[-50:],
        "bookmarks": state.bookmarks[-200:],
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)
