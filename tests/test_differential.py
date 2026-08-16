from __future__ import annotations

import io
import random
import zipfile
from pathlib import Path

from zipsearch.execution import execute_plan, plan_search
from zipsearch.index import build_or_update, default_index_path
from zipsearch.models import SafetyLimits, SearchOptions


def _keys(root: Path, options: SearchOptions, *, force_scan: bool = False) -> set[tuple[str, ...]]:
    plan = plan_search(root, options, force_scan=force_scan)
    return {
        (match.archive, "!".join((*match.nested_path, match.member)), str(match.line), match.text)
        for scan in execute_plan(root, options, plan)
        for match in scan.matches
    }


def _corpus(root: Path, seed: int) -> None:
    randomizer = random.Random(seed)
    vocabulary = ("alpha", "bravo", "needle", "Москва", "Скрепкин", "обычный")
    for archive_number in range(3):
        with zipfile.ZipFile(root / f"{archive_number}.zip", "w") as archive:
            rows = []
            for line in range(12):
                words = randomizer.sample(vocabulary, randomizer.randint(1, 3))
                phone = " +7 (999) 555-01-23" if line == seed % 12 else ""
                rows.append(" ".join(words) + phone + "\n")
            archive.writestr("records.txt", "".join(rows))
            archive.writestr("records.txt", "duplicate needle\n")
            archive.writestr("events.jsonl", f'{{"value":"needle", "seed":{seed}}}\n')
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w") as nested:
                nested.writestr("inside.txt", "nested needle Скрепкин\n")
            archive.writestr("nested.zip", inner.getvalue())


def test_randomized_direct_index_and_hybrid_parity(tmp_path: Path) -> None:
    for seed in range(8):
        root = tmp_path / str(seed)
        root.mkdir()
        _corpus(root, seed)
        build_or_update(root, default_index_path(root), SafetyLimits(), rebuild=True)
        for options in (
            SearchOptions(("needle",)),
            SearchOptions(("Скрепкин needle",), smart=True),
            SearchOptions(("+79995550123",), smart=True),
        ):
            assert _keys(root, options) == _keys(root, options, force_scan=True)
        # Add one archive without updating: the planner must merge its scan leg
        # with the still-valid compact coverage rather than hiding it.
        with zipfile.ZipFile(root / "new.zip", "w") as archive:
            archive.writestr("new.txt", "staleonly coverage\n")
        options = SearchOptions(("staleonly",))
        assert plan_search(root, options).execution == "hybrid"
        assert _keys(root, options) == _keys(root, options, force_scan=True)
