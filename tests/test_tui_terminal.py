"""PTY coverage for the actual curses event loop, not just its controller."""

from __future__ import annotations

import fcntl
import os
import pty
import select
import struct
import sys
import termios
import time
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="PTY integration is POSIX-only")


def _collect(master: int, seconds: float) -> bytes:
    end = time.monotonic() + seconds
    data: list[bytes] = []
    while time.monotonic() < end:
        if select.select([master], [], [], 0.05)[0]:
            try:
                data.append(os.read(master, 65_536))
            except (BlockingIOError, OSError):
                break
    return b"".join(data)


def test_tui_pty_submits_unicode_query_to_the_real_engine(tmp_path: Path) -> None:
    """Protect the keyboard-to-search path that a controller-only test cannot see."""
    archive = tmp_path / "records.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("people.txt", "Скрепкин Игорь\n")
    root = str(tmp_path)
    code = f"from zipsearch.cli import main; raise SystemExit(main(['tui', {root!r}]))"
    pid, master = pty.fork()
    if pid == 0:
        environment = os.environ | {"TERM": "xterm-256color", "LANG": "C.UTF-8"}
        os.execve(sys.executable, [sys.executable, "-c", code], environment)
    try:
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
        os.set_blocking(master, False)
        transcript = _collect(master, 0.5)
        os.write(master, b"/")
        for character in "Скрепкин Игорь":
            os.write(master, character.encode("utf-8"))
            time.sleep(0.01)
        os.write(master, b"\r")
        transcript += _collect(master, 2.0)
        os.write(master, b"q")
        transcript += _collect(master, 0.2)
        _, status = os.waitpid(pid, 0)
    finally:
        os.close(master)
    rendered = transcript.decode("utf-8", "replace")
    assert os.waitstatus_to_exitcode(status) == 0
    assert "COMPLETE" in rendered
    assert "Скрепкин Игорь" in rendered
