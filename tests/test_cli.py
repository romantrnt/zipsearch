from __future__ import annotations

import json
import zipfile
from pathlib import Path

from zipsearch.cli import main


def make_archive(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("records.txt", "first\nneedle\n")


def test_cli_human_and_jsonl_output(tmp_path: Path, capsys) -> None:
    archive = tmp_path / "sample.zip"
    make_archive(archive)
    assert main(["search", str(archive), "needle", "-q"]) == 0
    captured = capsys.readouterr()
    assert "records.txt:2: needle" in captured.out
    assert main(["search", str(archive), "needle", "--jsonl"]) == 0
    objects = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert objects[0]["type"] == "match"
    assert objects[-1]["type"] == "summary"


def test_cli_inspect_and_invalid_regex(tmp_path: Path, capsys) -> None:
    archive = tmp_path / "sample.zip"
    make_archive(archive)
    assert main(["inspect", str(archive), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["members"] == 1
    try:
        main(["search", str(archive), "[​", "--regex"])
    except SystemExit as error:
        assert error.code == 2


def test_cli_smart_is_ranked_and_literal_remains_unchanged(tmp_path: Path, capsys) -> None:
    archive = tmp_path / "names.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("people.txt", "Скрепкин Глеб\nГлеб Скрепкин\nСкрепкин Борис\n")
    assert main(["search", str(archive), "Глеб Скрепкин", "--smart", "--jsonl"]) == 0
    objects = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert objects[0]["match_type"] == "phrase"
    assert objects[0]["text"] == "Глеб Скрепкин"
    assert main(["search", str(archive), "Глеб Скрепкин", "--jsonl"]) == 0
    literal = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [item["text"] for item in literal if item["type"] == "match"] == ["Глеб Скрепкин"]
