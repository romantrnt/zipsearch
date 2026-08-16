from __future__ import annotations

from zipsearch.grouping import group
from zipsearch.models import Match


def test_exact_grouping_retains_each_raw_occurrence_and_provenance() -> None:
    matches = [
        Match("a.zip", "one.txt", 1, "same", ("same",), provenance=(("record", "1"),)),
        Match("b.zip", "two.txt", 4, "same", ("same",), provenance=(("record", "4"),)),
        Match("b.zip", "two.txt", 5, "other", ("other",)),
    ]
    grouped = group(matches, "exact")
    assert [(item.key, len(item.occurrences)) for item in grouped] == [("same", 2), ("other", 1)]
    assert grouped[0].occurrences[1].provenance == (("record", "4"),)


def test_phone_email_and_entity_grouping_are_deterministic() -> None:
    matches = [
        Match("a.zip", "one.txt", 1, "phone +7 (999) 555-01-23", ("x",)),
        Match("b.zip", "two.txt", 1, "phone +79995550123", ("x",)),
        Match("c.zip", "three.txt", 1, "mail user@example.invalid", ("x",)),
    ]
    assert len(group(matches, "phone")[0].occurrences) == 2
    assert group(matches, "email")[2].key == "user@example.invalid"
    assert group(matches, "entity")[2].key == "user@example.invalid"
