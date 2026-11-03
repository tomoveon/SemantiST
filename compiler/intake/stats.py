"""Diagnostic statistics of the pinned RuSTy over the extracted corpus.

RuSTy stops after the parse stage when a file has parse errors, so a single
compile only shows the first layer of problems. The measurement therefore runs
in rounds: after each round the units that contain errors are blanked
(replaced by spaces, newlines kept, so every later diagnostic still points at
the original line) and the file is compiled again, until it type checks; the
remaining units are then compiled to LLVM IR. Blanking is a measurement device
only -- it never produces intake output.

Diagnostics that name a blanked unit (unresolved reference, unknown type) are
recorded as ``cascade:excluded_unit`` so they are not counted as constructs.

Run in the toolchain container::

    python3 -m compiler.intake.stats --outputs artifacts/intake/extract/outputs \\
        --work artifacts/intake/stats/raw --jobs 64
"""

from __future__ import annotations

import argparse
import collections
import json
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import corpus, rusty
from .diagnostics import SourceIndex, locate, referenced_name

MAX_ROUNDS = 25


def blank(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        for i in range(start, min(end, len(chars))):
            if chars[i] not in "\r\n":
                chars[i] = " "
    return "".join(chars)


def isolate_panic(text: str, units: list, blanked: list[tuple[int, int]], probe: Path, extra: list[str],
                  ir: Path | None, start: int = 0) -> tuple[object | None, int]:
    """Return the first unit (from ``start``) whose presence makes the compiler panic.

    Prefix search: the smallest k such that keeping ``units[:k]`` (all later
    units blanked) panics; the culprit is ``units[k - 1]``. Returns the culprit
    and its index, or ``(None, start)`` when no prefix reproduces the panic.
    """

    def panics(k: int) -> bool:
        removed = blanked + [(u.start, u.end) for u in units[k:]]
        probe.write_text(blank(text, removed), encoding="utf-8")
        return bool(rusty.check([probe], extra=extra, ir=ir).panic)

    low, high = start, len(units)
    if not panics(high):
        return None, start
    while low + 1 < high:
        middle = (low + high) // 2
        if panics(middle):
            high = middle
        else:
            low = middle
    return units[high - 1], high - 1


def measure(path: Path, work: Path, extra: list[str] | None = None) -> dict:
    """Staged compile of one library file; see the module documentation."""
    text = path.read_text(encoding="utf-8")
    index = SourceIndex(text)
    unit_names = {unit.qualified_name.upper(): unit for unit in index.units if unit.name}
    unit_names.update({unit.name.upper(): unit for unit in index.units if unit.name})
    work = work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    probe = work / (path.stem + ".probe.st")
    blanked: dict[int, dict] = {}
    blanked_names: set[str] = set()
    diagnostics = []
    rounds = []
    current = text
    outcome = "unknown"
    panic_search_start = 0
    recorded: set[tuple] = set()
    round_number = 0
    while round_number < MAX_ROUNDS + len(index.units):
        probe.write_text(current, encoding="utf-8")
        result = rusty.check([probe], extra=extra or [])
        own = [d for d in result.diagnostics if not d.path or Path(d.path).resolve() == probe]
        critical_parse = "critical parse errors" in result.output
        stage = "parse" if critical_parse else "type"
        errors = 0
        offending: list[tuple[int, int, str]] = []
        seen_units: set = set()
        for diagnostic in own:
            identity = (diagnostic.code, diagnostic.line, diagnostic.column, diagnostic.message)
            if identity in recorded:
                # Already counted in an earlier round; still blank its unit.
                located = locate(diagnostic, index)
                if diagnostic.severity == "error":
                    errors += 1
                    if located.unit is not None:
                        offending.append((located.unit.start, located.unit.end, "unit"))
                continue
            recorded.add(identity)
            located = locate(diagnostic, index)
            construct = located.construct
            name = referenced_name(diagnostic)
            if name and name.upper() in blanked_names:
                construct = "cascade:excluded_unit"
            record = {
                "round": round_number,
                "stage": stage,
                "severity": diagnostic.severity,
                "code": diagnostic.code,
                "construct": construct,
                "line": diagnostic.line,
                "column": diagnostic.column,
                "unit": located.unit.qualified_name if located.unit else None,
                "unit_kind": located.unit.kind if located.unit else None,
                "message": diagnostic.message.split("\n", 1)[0][:300],
                "name": name,
            }
            # The first error of a unit in a round is its root; later errors in
            # the same unit are usually parser-recovery cascades.
            key = located.unit.start if located.unit else ("line", diagnostic.line)
            record["root"] = diagnostic.severity == "error" and key not in seen_units
            if diagnostic.severity == "error":
                seen_units.add(key)
            diagnostics.append(record)
            if diagnostic.severity == "error":
                errors += 1
                unit = located.unit
                if unit is not None:
                    offending.append((unit.start, unit.end, "unit"))
                else:
                    line_start = index.line_starts[diagnostic.line - 1] if diagnostic.line else 0
                    line_end = text.find("\n", line_start)
                    offending.append((line_start, len(text) if line_end < 0 else line_end, "line"))
        rounds.append({"round": round_number, "stage": stage, "errors": errors,
                       "panic": result.panic, "returncode": result.returncode})
        panic_stage = None
        panic_where = result.panic
        ir = work / (path.stem + ".ll")
        if not panic_where and errors == 0 and result.returncode == 0:
            generated = rusty.check([probe], ir=ir, extra=extra or [])
            if generated.panic:
                panic_stage, panic_where = "codegen", generated.panic
            elif not generated.ok:
                outcome = "codegen_failure"
                diagnostics.append({"round": round_number, "stage": "codegen", "severity": "error",
                                    "code": "CODEGEN", "construct": "codegen_error", "line": 0, "column": 0,
                                    "unit": None, "unit_kind": None,
                                    "message": ("; ".join(d.message for d in generated.errors)
                                                or generated.output.strip())[:300], "name": None,
                                    "root": True})
                break
            else:
                outcome = "ir_generated"
                ir.unlink(missing_ok=True)
                break
        elif panic_where:
            panic_stage = "check"
        if panic_where:
            active = [u for u in index.units if u.start not in blanked]
            culprit, panic_search_start = isolate_panic(
                text, active, [(b["start"], b["end"]) for b in blanked.values()], probe, extra or [],
                ir if panic_stage == "codegen" else None, panic_search_start)
            ir.unlink(missing_ok=True)
            diagnostics.append({"round": round_number, "stage": panic_stage, "severity": "error", "code": "PANIC",
                                "construct": "compiler_panic:" + panic_where, "line": culprit.line if culprit else 0,
                                "column": 0, "unit": culprit.qualified_name if culprit else None,
                                "unit_kind": culprit.kind if culprit else None, "message": panic_where,
                                "name": None, "root": True})
            rounds[-1]["panic"] = panic_where
            if culprit is None:
                outcome = "compiler_panic"
                break
            offending.append((culprit.start, culprit.end, "unit"))
            stage = panic_stage
        new_spans = [(s, e) for s, e, _ in offending if s not in blanked]
        if not new_spans:
            outcome = "stuck"
            break
        for start, end, kind in offending:
            if start in blanked:
                continue
            unit = index.unit_at(start) if kind == "unit" else None
            blanked[start] = {"start": start, "end": end, "kind": kind, "round": round_number, "stage": stage,
                              "unit": unit.qualified_name if unit else None,
                              "unit_kind": unit.kind if unit else None}
            if unit is not None and unit.name:
                blanked_names.add(unit.name.upper())
                blanked_names.add(unit.qualified_name.upper())
        current = blank(text, [(b["start"], b["end"]) for b in blanked.values()])
        # Rounds that only remove a panicking unit do not count against the limit.
        if not panic_where:
            panic_search_start = 0
        round_number += 1
    else:
        outcome = "round_limit"
    probe.unlink(missing_ok=True)
    units = [u for u in index.units]
    excluded_units = [b for b in blanked.values() if b["kind"] == "unit"]
    return {
        "library": path.name,
        "outcome": outcome,
        "rounds": rounds,
        "units_total": len(units),
        "units_excluded": len(excluded_units),
        "units_excluded_parse": sum(1 for b in excluded_units if b["stage"] == "parse"),
        "units_excluded_type": sum(1 for b in excluded_units if b["stage"] == "type"),
        "lines_blanked_outside_units": sum(1 for b in blanked.values() if b["kind"] == "line"),
        "excluded": sorted(blanked.values(), key=lambda b: b["start"]),
        "diagnostics": diagnostics,
    }


def _job(arguments: tuple[str, str, str, str, list[str]]) -> dict:
    vendor, family, path, work, extra = arguments
    result = measure(Path(path), Path(work), extra)
    result["vendor"] = vendor
    result["family"] = family
    return result


def aggregate(results: list[dict]) -> dict:
    """Error code x construct x number of libraries, per family and stage."""
    table: dict[tuple, dict] = {}
    for result in results:
        for d in result["diagnostics"]:
            if d["severity"] != "error":
                continue
            key = (result["family"], d["stage"], d["code"], d["construct"])
            row = table.setdefault(key, {"family": key[0], "stage": key[1], "code": key[2], "construct": key[3],
                                         "diagnostics": 0, "root_diagnostics": 0, "sites": set(),
                                         "libraries": set(), "root_libraries": set(), "examples": []})
            row["diagnostics"] += 1
            if d.get("root"):
                row["root_diagnostics"] += 1
                row["root_libraries"].add((result["vendor"], result["library"]))
            row["sites"].add((result["vendor"], result["library"], d["line"]))
            row["libraries"].add((result["vendor"], result["library"]))
            if len(row["examples"]) < 3 and (result["vendor"], result["library"], d["line"]) not in {
                    (e["vendor"], e["library"], e["line"]) for e in row["examples"]}:
                row["examples"].append({"vendor": result["vendor"], "library": result["library"],
                                        "line": d["line"], "message": d["message"][:160]})
    rows = []
    for row in table.values():
        row["sites"] = len(row["sites"])
        row["libraries"] = len(row["libraries"])
        row["root_libraries"] = len(row["root_libraries"])
        rows.append(row)
    rows.sort(key=lambda r: (r["family"], r["stage"], -r["libraries"], -r["sites"], r["code"], r["construct"]))
    outcomes = collections.Counter((r["family"], r["outcome"]) for r in results)
    per_family = collections.defaultdict(lambda: collections.Counter())
    for r in results:
        c = per_family[r["family"]]
        c["libraries"] += 1
        c["units_total"] += r["units_total"]
        c["units_excluded_parse"] += r["units_excluded_parse"]
        c["units_excluded_type"] += r["units_excluded_type"]
        c["outcome:" + r["outcome"]] += 1
    return {
        "rows": rows,
        "outcomes": [{"family": f, "outcome": o, "libraries": n} for (f, o), n in sorted(outcomes.items())],
        "families": {f: dict(c) for f, c in sorted(per_family.items())},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--outputs", type=Path, required=True, help="extraction outputs directory")
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    parser.add_argument("--family", action="append", default=[])
    parser.add_argument("--plc-arg", action="append", default=[], help="extra plc argument (repeatable)")
    args = parser.parse_args(argv)
    vendors = [v for v in corpus.VENDORS if not args.family or v.family in args.family]
    jobs = []
    for vendor, path in corpus.libraries(args.outputs, vendors):
        work = args.work / "probes" / vendor.directory
        jobs.append((vendor.directory, vendor.family, str(path), str(work), list(args.plc_arg)))
    # Large files first, so the pool is not left waiting on one OSCAT export.
    jobs.sort(key=lambda job: -Path(job[2]).stat().st_size)
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(_job, jobs))
    results.sort(key=lambda r: (r["vendor"], r["library"]))
    args.work.mkdir(parents=True, exist_ok=True)
    (args.work / "libraries.json").write_text(json.dumps(results, ensure_ascii=False, indent=1) + "\n",
                                              encoding="utf-8")
    summary = aggregate(results)
    summary["toolchain"] = rusty.toolchain_identity()
    summary["plc_args"] = args.plc_arg
    (args.work / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n",
                                            encoding="utf-8")
    print(f"measured {len(results)} libraries -> {args.work}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
