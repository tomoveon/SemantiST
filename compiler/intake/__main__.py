"""Library intake commands.

    python3 -m compiler.intake library --vendor 02_CODESYS23 FILE --output DIR
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import corpus
from .library import intake


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m compiler.intake", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("library", help="intake one extracted library file")
    one.add_argument("source", type=Path)
    one.add_argument("--vendor", required=True, choices=sorted(corpus.BY_DIRECTORY))
    one.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "library":
        vendor = corpus.BY_DIRECTORY[args.vendor]
        if vendor.dialect is None:
            raise SystemExit(f"{args.vendor}: {vendor.note}")
        try:
            report = intake(args.source.resolve(), vendor, args.output.resolve())
        except Exception as error:  # noqa: BLE001 - CLI must expose a structured failed run
            print(json.dumps({"status": "FAILED", "reason": str(error)}), file=sys.stderr)
            return 1
        syntax = report["syntax"]
        status = {"full": "READY", "partial": "APPROXIMATE", "none": "UNSUPPORTED"}.get(
            syntax.get("level"), "FAILED"
        )
        print(json.dumps({"status": status, "syntax": syntax, "behaviour": report["behaviour"]["level"],
                          "rules": report["rules_applied"]}, indent=1))
        return 2 if status == "UNSUPPORTED" else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
