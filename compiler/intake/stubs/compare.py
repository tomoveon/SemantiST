"""Compare the core intake with the run that adds environment stubs.

    python3 -m compiler.intake.stubs.compare --core artifacts/intake/libraries \\
        --stubbed artifacts/intake/libraries-stubbed --output artifacts/intake/stubs/comparison.json

Per family: units accepted by the core run, by the stubbed run, the units
accepted only with stubs (second tier: their behaviour depends on an
approximation) and the units the core run accepts but the stubbed run does not
(a stub that breaks a unit; expected to be zero).
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path


def _accepted(directory: Path) -> tuple[str, int, set[tuple[str, int]]] | None:
    report = directory / "compatibility-report.json"
    if not report.is_file():
        return None
    meta = json.loads((directory / "compatibility.json").read_text(encoding="utf-8"))
    units = json.loads(report.read_text(encoding="utf-8"))["units"]
    return meta["dialect"], len(units), {(u["file"], u["line"]) for u in units if u["accepted"]}


def compare(core: Path, stubbed: Path) -> dict:
    families = collections.defaultdict(collections.Counter)
    lost, missing = [], []
    for report in sorted(core.rglob("compatibility-report.json")):
        library = report.parent.relative_to(core)
        first, second = _accepted(report.parent), _accepted(stubbed / library)
        if second is None:
            missing.append(str(library))
            continue
        family, total, accepted = first
        row = families[family]
        row["libraries"] += 1
        row["units"] += total
        row["core_accepted"] += len(accepted)
        row["stubbed_accepted"] += len(second[2])
        row["only_with_stubs"] += len(second[2] - accepted)
        row["lost_with_stubs"] += len(accepted - second[2])
        if second[2] - accepted:
            row["libraries_gaining"] += 1
        lost += [{"library": str(library), "file": f, "line": n} for f, n in sorted(accepted - second[2])]
    totals = sum(families.values(), collections.Counter())
    return {"schema_version": "semantist.intake-stub-comparison/1.0.0",
            "families": {f: dict(sorted(c.items())) for f, c in sorted(families.items())},
            "total": dict(sorted(totals.items())), "lost": lost, "missing_in_stubbed_run": missing}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--stubbed", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = compare(args.core, args.stubbed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for family, row in result["families"].items():
        print(f"{family}: {row['core_accepted']} -> {row['stubbed_accepted']} / {row['units']} "
              f"(+{row['only_with_stubs']}, lost {row['lost_with_stubs']})")
    print("total", result["total"], "missing", len(result["missing_in_stubbed_run"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
