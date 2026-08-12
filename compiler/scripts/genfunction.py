#!/usr/bin/env python3
"""Extract one FUNCTION or FUNCTION_BLOCK from a normalized ST library."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LIBRARY = ROOT / "benchmarks" / "oscat_basic" / "source" / "oscat.st"


def candidate_names(name: str) -> list[str]:
    names = [name]
    if not name.startswith("_"):
        names.append(f"_{name}")
    return names


def extract_pou(source: str, name: str) -> tuple[str, str, str]:
    for candidate in candidate_names(name):
        start = re.search(
            rf"(?im)^[ \t]*(FUNCTION_BLOCK|FUNCTION)[ \t]+{re.escape(candidate)}\b[^\n]*\n",
            source,
        )
        if not start:
            continue
        kind = start.group(1).upper()
        end_keyword = "END_FUNCTION_BLOCK" if kind == "FUNCTION_BLOCK" else "END_FUNCTION"
        end = re.search(rf"(?im)^[ \t]*{end_keyword}\b[^\n]*(?:\n|$)", source[start.end() :])
        if not end:
            raise SystemExit(f"[genfunction] found {candidate}, but missing {end_keyword}")
        block_end = start.end() + end.end()
        return candidate, kind, source[start.start() : block_end].strip() + "\n"

    tried = ", ".join(candidate_names(name))
    raise SystemExit(f"[genfunction] could not find FUNCTION/FUNCTION_BLOCK named: {tried}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract a POU from a normalized ST library")
    parser.add_argument("function", help="Function name, e.g. INC2 or STRING_TO_BUFFER")
    parser.add_argument(
        "--library",
        "--oscat",
        dest="library",
        type=Path,
        default=Path(DEFAULT_LIBRARY),
        help="normalized source library (legacy alias: --oscat)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output target.st path",
    )
    args = parser.parse_args()

    library = args.library.resolve()
    output = args.output.resolve()
    source = library.read_text(encoding="utf-8", errors="replace")
    actual_name, kind, block = extract_pou(source, args.function)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(block, encoding="utf-8")
    print(f"[genfunction] wrote {output} from {kind} {actual_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
