from __future__ import annotations

import json
import sys
from typing import TextIO

from .models import ArchiveScan, Match, Summary


def render_match(match: Match, output: TextIO | None = None) -> None:
    output = output or sys.stdout
    source = "!".join((*match.nested_path, match.member)) if match.nested_path else match.member
    print(f"{match.archive}:{source}:{match.line}: {match.text}", file=output)
    for line in match.context_before:
        print(f"  - {line}", file=output)
    for line in match.context_after:
        print(f"  + {line}", file=output)


def render_jsonl_match(match: Match, output: TextIO | None = None) -> None:
    output = output or sys.stdout
    print(
        json.dumps({"type": "match", **match.as_dict()}, ensure_ascii=False, sort_keys=True),
        file=output,
    )


def render_issue(scan: ArchiveScan, message: str, output: TextIO | None = None) -> None:
    output = output or sys.stderr
    print(f"zipsearch: {scan.archive}: {message}", file=output)


def render_summary(summary: Summary, output: TextIO | None = None) -> None:
    output = output or sys.stderr
    print(
        "zipsearch: "
        f"archives={summary.archives_completed}/{summary.archives_seen} "
        f"members={summary.members_scanned}/{summary.members_seen} "
        f"matches={summary.matches} issues={summary.issues}",
        file=output,
    )


def render_json_summary(summary: Summary, output: TextIO | None = None) -> None:
    output = output or sys.stdout
    print(json.dumps({"type": "summary", **summary.as_dict()}, sort_keys=True), file=output)
