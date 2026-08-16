from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from zipsearch.cli import main
from zipsearch.engine import scan_archive
from zipsearch.execution import plan_search
from zipsearch.models import SearchOptions
from zipsearch.query import QuerySyntaxError, parse


def test_advanced_boolean_phrase_prefix_and_metadata_fields(tmp_path: Path, capsys) -> None:
    archive = tmp_path / "records.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(
            "users.csv",
            "name,state\nГлеб Скрепкин,active\nГлеб blocked,blocked\n",
        )
        output.writestr("notes.txt", "Глеб Скрепкин\n")
    query = '"Глеб Скрепкин" OR (Глеб* AND NOT blocked)'
    options = SearchOptions((query,), smart=True, advanced_query=query)
    matches = scan_archive(archive, options).matches
    assert {match.member for match in matches} == {"users.csv", "notes.txt"}
    field_query = (
        "archive:records.zip AND member:users.csv AND format:csv AND fields:name AND NOT blocked"
    )
    field_options = SearchOptions((field_query,), smart=True, advanced_query=field_query)
    assert [match.text for match in scan_archive(archive, field_options).matches] == [
        "Глеб Скрепкин,active"
    ]
    assert plan_search(tmp_path, field_options).execution == "scan"
    assert main(["search", str(tmp_path), field_query, "--advanced", "--jsonl"]) == 0
    assert '"match"' in capsys.readouterr().out


def test_advanced_query_reports_useful_syntax_errors() -> None:
    with pytest.raises(QuerySyntaxError, match="expected a term"):
        parse("needle AND (Москва OR")
    with pytest.raises(QuerySyntaxError, match="value"):
        parse("format:")
