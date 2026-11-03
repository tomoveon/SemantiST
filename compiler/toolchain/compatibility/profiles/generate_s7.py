#!/usr/bin/env python3
"""Generate the ``s7-conversions`` profile from the S7-SCL conversion table.

The rules follow "Standard Functions of S7-SCL", tables of chapter 14 of the
S7-SCL V5.3 manual (rusty_compatibility/06_Siemens_Classic_SCL/该厂商st规范/
补充调查/Siemens_S7SCL_V53_official.pdf):

* <bit string or integer>_TO_BOOL copies the least significant bit (RuSTy's
  BYTE/WORD/DWORD_TO_BOOL already do; INT/DINT_TO_BOOL are generated);
* DWORD_TO_REAL and REAL_TO_DWORD copy the bit string (as RuSTy does);
* DATE counts days since 1990-01-01 (DINT_TO_DATE / DATE_TO_DINT copy the bit
  string); TIME and TOD count milliseconds.

Where the pinned RuSTy defines a function of the same name with other
semantics the adapter is named ``__S7_<name>`` and listed under ``rebind``.
Conversions whose S7 result format is not documented precisely enough here
(the string conversions I_STRNG, R_STRNG, ...) are not generated, so uses stay
unresolved.

Usage: generate_s7.py --stdlib DIR [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from generate_codesys import HEADER, function, stdlib_functions

HERE = Path(__file__).resolve().parent
MANUAL = "rusty_compatibility/06_Siemens_Classic_SCL/该厂商st规范/补充调查/Siemens_S7SCL_V53_official.pdf"
NS_PER_MS = 1_000_000
NS_PER_DAY = 86_400_000_000_000
DAYS_1970_TO_1990 = 7305
NOTE = """   S7-SCL conversions (S7-SCL V5.3 manual, chapter 14) on RuSTy's
   representation: DATE = days since 1990-01-01, TIME/TOD = milliseconds,
   least significant bit for INT/DINT_TO_BOOL."""


def adapters(std: set[str]) -> tuple[str, dict[str, str], list[str]]:
    out, rebind, names = [], {}, []

    def emit(vendor: str, result: str, argument: str, body: str, local: str | None = None) -> None:
        name = vendor
        if vendor in std:
            name = "__S7_" + vendor
            rebind[vendor] = name
        out.append(function(name, result, argument, body.replace("{name}", name), local))
        names.append(name)

    # BYTE/WORD/DWORD_TO_BOOL (least significant bit) and DWORD_TO_REAL /
    # REAL_TO_DWORD (bit copy) are defined by RuSTy with the S7 semantics.
    emit("INT_TO_BOOL", "BOOL", "INT", "w := IN;\n{name} := (w AND 1) <> 0;", "w : WORD;")
    emit("DINT_TO_BOOL", "BOOL", "DINT", "w := IN;\n{name} := (w AND 1) <> 0;", "w : DWORD;")
    emit("DINT_TO_DATE", "DATE", "DINT",
         f"v := IN;\n{{name}} := LINT_TO_DATE((v + LINT#{DAYS_1970_TO_1990}) * LINT#{NS_PER_DAY});", "v : LINT;")
    emit("DATE_TO_DINT", "DINT", "DATE",
         f"{{name}} := LINT_TO_DINT(DATE_TO_LINT(IN) / LINT#{NS_PER_DAY} - LINT#{DAYS_1970_TO_1990});")
    for vendor, rusty_type in (("TIME", "TIME"), ("TOD", "TIME_OF_DAY")):
        emit(f"DINT_TO_{vendor}", rusty_type, "DINT",
             f"v := IN;\n{{name}} := LINT_TO_{vendor}(v * LINT#{NS_PER_MS});", "v : LINT;")
        emit(f"{vendor}_TO_DINT", "DINT", rusty_type,
             f"{{name}} := LINT_TO_DINT({vendor}_TO_LINT(IN) / LINT#{NS_PER_MS});")
    return "\n".join(out), rebind, names


def render(stdlib: Path) -> dict[Path, str]:
    std = stdlib_functions(stdlib)
    text, rebind, names = adapters(std)
    profile = {
        "schema_version": "semantist.compatibility-profile/1.0.0",
        "name": "s7-conversions",
        "dialects": ["siemens-scl"],
        "kind": "semantic_adapter",
        "st_sources": ["conversions.st"],
        "native_sources": [],
        "native_compile_args": [],
        "link_args": [],
        "provides": sorted(names),
        "rebind": dict(sorted(rebind.items())),
        "evidence": [MANUAL + " (chapter 14, conversion functions; 6.2 elementary data types)"],
        "assumptions": [
            "TIME and TOD are whole milliseconds; the S7 32-bit range (TIME up to 24 days) is not enforced and "
            "wrap-around is not simulated.",
            "DATE values are whole days since 1990-01-01; the S7 16-bit range is not enforced.",
            "The ENO/OK flag of S7 conversions (set to FALSE on overflow) is not modelled.",
        ],
    }
    return {
        HERE / "s7-conversions" / "conversions.st": HEADER.format(body=NOTE) + "\n" + text,
        HERE / "s7-conversions" / "profile.json": json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stdlib", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    stale = []
    for path, text in render(args.stdlib).items():
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                stale.append(str(path))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if stale:
        print("stale generated profiles: " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
