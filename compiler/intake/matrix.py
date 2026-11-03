"""Compatibility matrix: dialect family x library (JSON is authoritative).

    python3 -m compiler.intake.matrix --libraries artifacts/intake/libraries \\
        --clones artifacts/intake/clones.json --unittests artifacts/intake/unittests.json \\
        --output artifacts/intake/compatibility-matrix.json --markdown artifacts/intake/compatibility-matrix.md

Syntax intake and behaviour evidence are separate columns (design 4.3). The
family rows also give clone-deduplicated counts (design 4.1): independent
libraries, independent POUs (near-clone families) and how many of those have
at least one accepted member.
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

from . import corpus

SCHEMA = "semantist.compatibility-matrix/1.0.0"


def _reasons(report: dict) -> collections.Counter:
    counter = collections.Counter()
    for unit in report["units"]:
        if unit["accepted"] or not unit["reasons"]:
            continue
        reason = unit["reasons"][0]
        stage = reason.get("stage")
        detail = reason.get("construct") or reason.get("code") or ""
        if unit["type_check"] == "depends_on_unsupported":
            stage, detail = "depends_on_unsupported", ""
        counter[f"{stage}: {detail}".strip(": ")] += 1
    return counter


POU_KINDS = {"FUNCTION", "FUNCTION_BLOCK", "PROGRAM", "CLASS"}
CAUSES = ["unsupported_construct", "blocking_pragma", "depends_on_rejected", "missing_platform",
          "compiler_limitation", "unhandled_diagnostic"]


def rejection_cause(unit: dict) -> str:
    """Why a rejected POU is rejected, in the order the causes are checked:
    a construct outside simple substitution, a pragma that changes meaning, a
    rejected unit it depends on, a provider the platform supplies (unresolved
    name or type, undefined symbol at link), a compiler limitation (panic or
    codegen error), or a diagnostic that no rule handles."""
    stages = {r.get("stage") for r in unit["reasons"]}
    first = unit["reasons"][0] if unit["reasons"] else {}
    if "construct" in stages or unit["type_check"] == "unsupported_construct":
        return "unsupported_construct"
    if "pragma" in stages:
        return "blocking_pragma"
    if unit["type_check"] == "depends_on_unsupported":
        return "depends_on_rejected"
    if first.get("construct") in {"unresolved_reference", "unknown_type"} or first.get("stage") == "link":
        return "missing_platform"
    if first.get("stage") in {"compiler_panic", "codegen_error", "ir"}:
        return "compiler_limitation"
    return "unhandled_diagnostic"


def build(libraries: Path, clones: dict | None, unittests: dict | None) -> dict:
    entries = json.loads((libraries / "corpus.json").read_text(encoding="utf-8"))["libraries"]
    tests_by_library = collections.defaultdict(list)
    for suite in (unittests or {}).get("suites", []):
        tests_by_library[suite["library"]].append(suite)
    near_by_pou = {}
    if clones:
        for item in clones["pou_families"]:
            near_by_pou[(item["vendor"], item["library"], item["name"].upper())] = item["near"]
    rows = []
    families = collections.defaultdict(lambda: collections.Counter())
    accepted_families = collections.defaultdict(set)
    for entry in entries:
        vendor = corpus.BY_DIRECTORY[entry["vendor"]]
        row = {"family": entry["family"], "priority": entry["priority"], "vendor": entry["vendor"],
               "library": entry["library"], "status": entry["status"]}
        if entry["status"] != "done":
            row["reason"] = entry.get("reason") or entry.get("error")
            rows.append(row)
            families[entry["family"]]["libraries"] += 1
            continue
        directory = libraries / entry["vendor"] / Path(entry["library"]).stem
        report = json.loads((directory / "compatibility-report.json").read_text(encoding="utf-8"))
        dependencies = json.loads((directory / "dependency-report.json").read_text(encoding="utf-8"))
        syntax = report["syntax"]
        row.update({
            "syntax_level": syntax["level"],
            "pous": syntax["pous"],
            "pous_accepted": syntax["pous_accepted"],
            "units": syntax["units"],
            "type_checked": syntax["type_checked"],
            "ir_generated": syntax["ir_generated"],
            "linked": syntax["linked"],
            "behaviour_level": report["behaviour"]["level"],
            "behaviour_unverified": report["behaviour"]["unverified"],
            "unit_tests": [{k: s.get(k) for k in ("id", "framework", "status", "passed", "test_cases", "reason")}
                           for s in tests_by_library.get(f"{entry['vendor']}/{Path(entry['library']).stem}", [])],
            "failure_reasons": dict(_reasons(report).most_common(8)),
            "unsupported_constructs": dict(collections.Counter(c["construct"] for c in
                                                               report["unsupported_constructs"]).most_common()),
            "unresolved_dependencies": dependencies["classifications"]["unresolved"][:30],
            "unresolved_count": len(dependencies["classifications"]["unresolved"]),
            "time_sources": [t["name"] for t in report["time_sources"]],
            "implicit_check_functions": report["implicit_check_functions"],
            "rules_applied": report["rules_applied"],
            "dependency_files": [d["file"] for d in report["target_identity"].get("dependency_files", [])],
            "note": vendor.note,
        })
        rows.append(row)
        f = families[entry["family"]]
        f["libraries"] += 1
        f["pous"] += syntax["pous"]
        f["pous_accepted"] += syntax["pous_accepted"]
        f["level:" + syntax["level"]] += 1
        f["behaviour:" + report["behaviour"]["level"]] += 1
        # Clone families are keyed by the extracted (original) name in the
        # file that defines the POU; map renamed units back and use the
        # unit's own file, which differs for units of the dependency closure
        # (SCL renames record the quoted original).
        renames = json.loads((directory / "transformations.json").read_text(encoding="utf-8"))["renames"]
        original = {new.upper(): old.strip('"').upper() for old, new in renames.items()}
        for unit in report["units"]:
            if unit["kind"] in POU_KINDS and not unit["accepted"]:
                f["cause:" + rejection_cause(unit)] += 1
            name = unit["name"].upper()
            key = (entry["vendor"], Path(unit["file"]).name, original.get(name, name))
            if unit["accepted"] and key in near_by_pou:
                accepted_families[entry["family"]].add(near_by_pou[key])
    family_rows = []
    for name, description in corpus.FAMILIES.items():
        if name == "none":
            continue
        f = families.get(name, collections.Counter())
        dedup = (clones or {}).get("per_family", {}).get(name, {})
        priority = next((v.priority for v in corpus.VENDORS if v.family == name), "")
        family_rows.append({
            "family": name, "description": description, "priority": priority,
            "libraries": f["libraries"], "pous": f["pous"], "pous_accepted": f["pous_accepted"],
            "acceptance": round(f["pous_accepted"] / f["pous"], 4) if f["pous"] else None,
            "syntax_levels": {k.split(":", 1)[1]: v for k, v in f.items() if k.startswith("level:")},
            "behaviour_levels": {k.split(":", 1)[1]: v for k, v in f.items() if k.startswith("behaviour:")},
            "independent_libraries": dedup.get("library_families"),
            "independent_pous": dedup.get("near_families"),
            "independent_pous_accepted": len(accepted_families[name]) if clones else None,
            "rejection_causes": {c: f["cause:" + c] for c in CAUSES if f["cause:" + c]},
        })
    excluded = [{"vendor": v, "status": "not included", "reason": reason} for v, reason in corpus.EXCLUDED]
    return {"schema_version": SCHEMA, "families": family_rows, "libraries": rows, "excluded_vendors": excluded,
            "clone_totals": (clones or {}).get("total")}


def _fmt(value) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.1%}"
    return str(value)


def markdown(matrix: dict) -> str:
    lines = ["# Compatibility matrix", "",
             "Generated from `compatibility-matrix.json` by `python3 -m compiler.intake.matrix`; the JSON is "
             "authoritative. *Accepted* means type check, LLVM IR and native link passed and no unsupported "
             "construct or blocking pragma was found; it is not a claim of equal behaviour (see the behaviour "
             "column and the unit-test results).", "", "## Dialect families", "",
             "| Priority | Family | Libraries | POUs accepted / total | Acceptance | Independent libraries | "
             "Independent POUs (accepted) | Syntax levels | Behaviour evidence |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |"]
    for f in matrix["families"]:
        lines.append(
            f"| {f['priority']} | {f['family']} | {f['libraries']} | {f['pous_accepted']} / {f['pous']} | "
            f"{_fmt(f['acceptance'])} | {_fmt(f['independent_libraries'])} | {_fmt(f['independent_pous'])} "
            f"({_fmt(f['independent_pous_accepted'])}) | "
            + ", ".join(f"{k} {v}" for k, v in sorted(f["syntax_levels"].items())) + " | "
            + ", ".join(f"{k} {v}" for k, v in sorted(f["behaviour_levels"].items())) + " |")
    lines += ["", "## Rejected POUs by cause", "",
              "First matching cause per rejected POU (`rejection_cause`): only *unhandled diagnostic* is a "
              "compiler error that no rule handles; the other causes are outside simple substitution or "
              "outside the library.", "",
              "| Family | Rejected | " + " | ".join(c.replace("_", " ") for c in CAUSES) + " |",
              "| --- | ---: | " + " | ".join("---:" for _ in CAUSES) + " |"]
    for f in matrix["families"]:
        rejected = f["pous"] - f["pous_accepted"]
        if rejected:
            lines.append(f"| {f['family']} | {rejected} | "
                         + " | ".join(str(f["rejection_causes"].get(c, 0)) for c in CAUSES) + " |")
    lines += ["", "## Libraries", "",
              "| Family | Vendor | Library | Syntax | POUs accepted / total | Behaviour | Unit tests | Main failure "
              "reasons | Unresolved | Time sources |",
              "| --- | --- | --- | --- | ---: | --- | --- | --- | ---: | --- |"]
    for row in matrix["libraries"]:
        if row["status"] != "done":
            lines.append(f"| {row['family']} | {row['vendor']} | {row['library']} | not supported | – | – | – | "
                         f"{row.get('reason', '')} | – | – |")
            continue
        tests = "; ".join(f"{t['framework']}: {t['status']}"
                          + (f" {t['passed']}/{t['test_cases']}" if t.get('test_cases') else "")
                          for t in row["unit_tests"]) or "–"
        reasons = "; ".join(f"{k} ({v})" for k, v in list(row["failure_reasons"].items())[:3]) or "–"
        lines.append(f"| {row['family']} | {row['vendor']} | {row['library'].replace('|', '/')} | "
                     f"{row['syntax_level']} | {row['pous_accepted']} / {row['pous']} | {row['behaviour_level']} | "
                     f"{tests} | {reasons.replace('|', '/')} | {row['unresolved_count']} | "
                     f"{', '.join(row['time_sources']) or '–'} |")
    lines += ["", "## Vendors without collected open ST libraries", "", "| Vendor | Status | Reason |",
              "| --- | --- | --- |"]
    for item in matrix["excluded_vendors"]:
        lines.append(f"| {item['vendor']} | {item['status']} | {item['reason']} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--libraries", type=Path, required=True)
    parser.add_argument("--clones", type=Path)
    parser.add_argument("--unittests", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args(argv)
    clones = json.loads(args.clones.read_text(encoding="utf-8")) if args.clones and args.clones.is_file() else None
    unittests = json.loads(args.unittests.read_text(encoding="utf-8")) \
        if args.unittests and args.unittests.is_file() else None
    matrix = build(args.libraries, clones, unittests)
    args.output.write_text(json.dumps(matrix, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.write_text(markdown(matrix), encoding="utf-8")
    for f in matrix["families"]:
        print(f["family"], f["pous_accepted"], f["pous"], _fmt(f["acceptance"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
