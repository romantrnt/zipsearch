from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .engine import (
    SearchConfigurationError,
    discover_archives,
    inspect_archive,
    rank_matches,
    scan_archives,
)
from .models import SafetyLimits, SearchOptions, Summary
from .render import (
    render_issue,
    render_json_summary,
    render_jsonl_match,
    render_match,
    render_summary,
)


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _add_common_search_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "path", type=Path, help="a ZIP archive or a directory containing ZIP archives"
    )
    parser.add_argument(
        "patterns", nargs="+", help="literal strings, or regular expressions with --regex"
    )
    parser.add_argument(
        "-r", "--regex", action="store_true", help="treat patterns as regular expressions"
    )
    parser.add_argument(
        "--smart", action="store_true", help="rank normalized token-aware human matches"
    )
    parser.add_argument(
        "-i", "--ignore-case", action="store_true", help="match without case sensitivity (default)"
    )
    parser.add_argument("--case-sensitive", action="store_true", help="match case exactly")
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="only member paths matching this glob; repeatable",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="skip member paths matching this glob; repeatable",
    )
    parser.add_argument(
        "--extension",
        action="append",
        default=[],
        metavar="EXT",
        help="only scan an extension, e.g. txt; repeatable",
    )
    parser.add_argument(
        "--max-matches", type=_positive, default=1000, help="global output limit (default: 1000)"
    )
    parser.add_argument(
        "-C", "--context", type=int, default=0, help="show this many lines around each match"
    )
    parser.add_argument(
        "--encoding", default="auto", help="text decoding: auto, or a Python codec (default: auto)"
    )
    parser.add_argument(
        "-j", "--workers", type=_positive, default=4, help="archive worker threads (default: 4)"
    )
    parser.add_argument(
        "--no-recursive", action="store_true", help="do not descend into input directories"
    )
    parser.add_argument(
        "--jsonl", action="store_true", help="write one JSON object per result to stdout"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="suppress human summary and warnings"
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="show progress; repeat for debug logs"
    )
    parser.add_argument(
        "--max-member-mib",
        type=_positive,
        default=512,
        help="maximum declared member size (default: 512)",
    )
    parser.add_argument(
        "--max-total-mib",
        type=_positive,
        default=2048,
        help="maximum declared expanded archive size (default: 2048)",
    )
    parser.add_argument(
        "--max-members", type=_positive, default=100000, help="maximum members per archive"
    )
    parser.add_argument(
        "--max-ratio", type=float, default=200.0, help="maximum compression ratio (default: 200)"
    )
    parser.add_argument(
        "--nested-depth",
        type=int,
        default=2,
        help="maximum nested ZIP depth (default: 2; 0 disables)",
    )


def _options(args: argparse.Namespace) -> SearchOptions:
    if args.context < 0 or args.nested_depth < 0 or args.max_ratio <= 0:
        raise SearchConfigurationError(
            "context and nested depth must be non-negative; ratio must be positive"
        )
    if args.smart and args.regex:
        raise SearchConfigurationError("--smart and --regex are mutually exclusive")
    return SearchOptions(
        patterns=tuple(args.patterns),
        regex=args.regex,
        smart=args.smart,
        case_sensitive=args.case_sensitive and not args.ignore_case,
        include=tuple(args.include),
        exclude=tuple(args.exclude),
        extensions=tuple(args.extension),
        max_matches=args.max_matches,
        context=args.context,
        encoding=args.encoding,
        workers=args.workers,
        limits=SafetyLimits(
            max_members=args.max_members,
            max_member_bytes=args.max_member_mib * 1024 * 1024,
            max_total_bytes=args.max_total_mib * 1024 * 1024,
            max_compression_ratio=args.max_ratio,
            max_nested_depth=args.nested_depth,
        ),
    )


def _search(args: argparse.Namespace) -> int:
    options = _options(args)
    summary = Summary()
    try:
        paths = discover_archives(args.path, recursive=not args.no_recursive)
        smart_matches = []
        for scan in scan_archives(paths, options):
            summary.archives_seen += 1
            summary.archives_completed += 1
            summary.members_seen += scan.members_seen
            summary.members_scanned += scan.members_scanned
            summary.issues += len(scan.issues)
            if args.verbose and not args.jsonl:
                print(f"zipsearch: completed {scan.archive}", file=sys.stderr)
            for issue in scan.issues:
                if not args.quiet and not args.jsonl:
                    render_issue(
                        scan, f"{issue.member + ': ' if issue.member else ''}{issue.message}"
                    )
            if options.smart:
                smart_matches.extend(scan.matches)
                if len(smart_matches) > options.max_matches * 2:
                    smart_matches = rank_matches(iter(smart_matches), options.max_matches)
                continue
            for match in scan.matches:
                if summary.matches >= options.max_matches:
                    break
                summary.matches += 1
                if args.jsonl:
                    render_jsonl_match(match)
                else:
                    render_match(match)
            if not options.smart and summary.matches >= options.max_matches:
                break
        if options.smart:
            for match in rank_matches(iter(smart_matches), options.max_matches):
                summary.matches += 1
                if args.jsonl:
                    render_jsonl_match(match)
                else:
                    render_match(match)
    except KeyboardInterrupt:
        if not args.quiet:
            print("zipsearch: cancelled", file=sys.stderr)
        return 130
    if args.jsonl:
        render_json_summary(summary)
    elif not args.quiet:
        render_summary(summary)
    return 1 if summary.issues else 0


def _inspect(args: argparse.Namespace) -> int:
    scan = inspect_archive(args.archive, SafetyLimits(max_members=args.max_members))
    payload = {
        "archive": str(args.archive),
        "members": scan.members_seen,
        "declared_uncompressed_bytes": scan.bytes_declared,
        "issues": [issue.as_dict() for issue in scan.issues],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"{args.archive}: {scan.members_seen} members, {scan.bytes_declared} declared bytes")
        for issue in scan.issues:
            print(f"  {issue.kind}: {issue.member + ': ' if issue.member else ''}{issue.message}")
    return 1 if scan.issues else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zipsearch",
        description="Search ZIP archives directly; never bulk-extract archive collections.",
    )
    parser.add_argument("--version", action="version", version="zipsearch 5.0.0")
    subcommands = parser.add_subparsers(dest="command", required=True)
    search = subcommands.add_parser("search", help="search archive members")
    _add_common_search_flags(search)
    search.set_defaults(handler=_search)
    inspect = subcommands.add_parser("inspect", help="inspect a ZIP archive's declared contents")
    inspect.add_argument("archive", type=Path)
    inspect.add_argument("--max-members", type=_positive, default=100000)
    inspect.add_argument("--json", action="store_true")
    inspect.set_defaults(handler=_inspect)
    tui = subcommands.add_parser("tui", help="open the interactive terminal application")
    tui.add_argument("path", type=Path, nargs="?", help="optional initial ZIP or directory")
    tui.set_defaults(handler=_tui)
    return parser


def _tui(args: argparse.Namespace) -> int:
    from .tui import run_tui

    return run_tui(args.path)


def main(argv: list[str] | None = None) -> int:
    if argv is None and len(sys.argv) == 1:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print(
                "zipsearch: use `zipsearch search ...` when stdin or stdout is not a terminal",
                file=sys.stderr,
            )
            return 2
        from .tui import run_tui

        return run_tui(None)
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "verbose", 0) > 1:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")
    try:
        return args.handler(args)
    except SearchConfigurationError as exc:
        parser.error(str(exc))
    except BrokenPipeError:
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
