"""Normalize one library: ``python -m compiler.intake.normalize SOURCE --dialect codesys --output DIR``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .. import rusty
from .engine import normalize, write_outputs
from .profiles import load_dialect, load_library
from .rules import rules_for


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, nargs="+")
    parser.add_argument("--dialect", required=True)
    parser.add_argument("--library-config")
    parser.add_argument("--compatibility-source", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    dialect = load_dialect(args.dialect)
    library = load_library(args.library_config) if args.library_config else {}
    files = {path.name: path.read_text(encoding="utf-8") for path in args.source}
    result = normalize(files, dialect, library, rules_for(dialect), args.output / ".work",
                       args.compatibility_source, dialect.get("compiler_args", []))
    record = write_outputs(result, args.output, {
        "dialect": {"id": dialect["id"], "version": dialect["version"]},
        "toolchain": rusty.toolchain_identity(),
    })
    print(json.dumps({"rules": record["rule_summary"], "remaining": len(record["remaining_errors"]),
                      "panic": record["compiler_panic"]}))
    return 0 if result.type_checks else 1


if __name__ == "__main__":
    raise SystemExit(main())
