"""Build a dependency-free ZipSearch zipapp from a source checkout.

Usage: ``python tools/build_zipapp.py [DESTINATION]``.
"""

from __future__ import annotations

import argparse
import zipapp
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", nargs="?", type=Path, default=Path("zipsearch.pyz"))
    args = parser.parse_args(argv)
    source = Path(__file__).resolve().parents[1] / "src"
    zipapp.create_archive(source, args.destination, main="zipsearch.cli:main", compressed=True)
    print(args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
