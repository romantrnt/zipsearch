from __future__ import annotations

import zipfile
from pathlib import Path

from zipsearch.execution import execute_plan, plan_search
from zipsearch.index import build_or_update, default_index_path
from zipsearch.models import SafetyLimits, SearchOptions


def corpus(root: Path) -> None:
    for number in range(6):
        with zipfile.ZipFile(root / f"{number}.zip", "w") as archive:
            archive.writestr("common.txt", "common Иван\n" * 3)
            archive.writestr("other.txt", "common Иван\n")
            archive.writestr(
                "rare.txt",
                "rare Иван phone +7 (999) 555-01-23\n" if number == 2 else "common Иван\n",
            )


def test_adaptive_planner_uses_index_for_selective_phone_and_scan_for_broad_smart(
    tmp_path: Path,
) -> None:
    corpus(tmp_path)
    build_or_update(tmp_path, default_index_path(tmp_path), SafetyLimits(), rebuild=True)
    selective = plan_search(tmp_path, SearchOptions(("+79995550123",), smart=True))
    assert selective.execution == "index"
    assert selective.candidate_archives == 1 and selective.candidate_members == 1
    broad = plan_search(tmp_path, SearchOptions(("Иван",), smart=True))
    assert broad.execution == "scan"
    assert broad.reason == "indexed candidate coverage is too broad"


def test_planner_force_missing_and_stale_indexes_choose_safe_hybrid(tmp_path: Path) -> None:
    corpus(tmp_path)
    options = SearchOptions(("rare",))
    assert plan_search(tmp_path, options).execution == "scan"
    build_or_update(tmp_path, default_index_path(tmp_path), SafetyLimits(), rebuild=True)
    assert plan_search(tmp_path, options, force_scan=True).reason == "forced by --no-index"
    with zipfile.ZipFile(tmp_path / "new.zip", "w") as archive:
        archive.writestr("new.txt", "rare\n")
    stale = plan_search(tmp_path, options)
    assert stale.execution == "hybrid" and stale.index.state == "STALE"
    hybrid = list(execute_plan(tmp_path, options, stale))
    direct = list(execute_plan(tmp_path, options, plan_search(tmp_path, options, force_scan=True)))
    assert {
        (match.archive, match.member, match.line, match.text)
        for scan in hybrid
        for match in scan.matches
    } == {
        (match.archive, match.member, match.line, match.text)
        for scan in direct
        for match in scan.matches
    }


def test_indexed_execution_matches_direct_results(tmp_path: Path) -> None:
    corpus(tmp_path)
    build_or_update(tmp_path, default_index_path(tmp_path), SafetyLimits(), rebuild=True)
    options = SearchOptions(("rare",))
    indexed = list(execute_plan(tmp_path, options, plan_search(tmp_path, options)))
    direct = list(execute_plan(tmp_path, options, plan_search(tmp_path, options, force_scan=True)))
    indexed_keys = {
        (match.archive, match.member, match.line, match.text)
        for scan in indexed
        for match in scan.matches
    }
    direct_keys = {
        (match.archive, match.member, match.line, match.text)
        for scan in direct
        for match in scan.matches
    }
    assert indexed_keys == direct_keys


def test_indexed_smart_result_cap_is_per_archive_not_per_member(tmp_path: Path) -> None:
    with zipfile.ZipFile(tmp_path / "many.zip", "w") as archive:
        archive.writestr("first.txt", "Иван\n" * 10)
        archive.writestr("second.txt", "Иван\n" * 10)
    build_or_update(tmp_path, default_index_path(tmp_path), SafetyLimits(), rebuild=True)
    options = SearchOptions(("Иван",), smart=True, max_matches=3)
    indexed = list(execute_plan(tmp_path, options, plan_search(tmp_path, options)))
    direct = list(execute_plan(tmp_path, options, plan_search(tmp_path, options, force_scan=True)))
    assert len(indexed[0].matches) == len(direct[0].matches) == 3
    assert [match.as_dict() for match in indexed[0].matches] == [
        match.as_dict() for match in direct[0].matches
    ]


def test_short_numeric_smart_value_uses_scan_when_entity_index_omits_it(tmp_path: Path) -> None:
    with zipfile.ZipFile(tmp_path / "numbers.zip", "w") as archive:
        archive.writestr("records.txt", "reference 1234-5678\n")
    build_or_update(tmp_path, default_index_path(tmp_path), SafetyLimits(), rebuild=True)
    options = SearchOptions(("12345678",), smart=True)
    plan = plan_search(tmp_path, options)
    assert plan.execution == "scan"
    matches = [
        match.text for scan in execute_plan(tmp_path, options, plan) for match in scan.matches
    ]
    assert matches == ["reference 1234-5678"]
