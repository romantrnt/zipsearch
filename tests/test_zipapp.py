from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_stdlib_zipapp_builder_runs_cli(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    target = tmp_path / "zipsearch.pyz"
    subprocess.run(
        [sys.executable, "tools/build_zipapp.py", str(target)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    completed = subprocess.run(
        [sys.executable, str(target), "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "zipsearch 6.0.0"
