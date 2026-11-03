"""Validation report of the RuSTy compatibility layer (JSON authoritative, Markdown generated).

Everything in the report is read from artifacts or logs; nothing is typed in::

    python3 -m compiler.intake.validation \\
        --libraries artifacts/intake/libraries --compare artifacts/intake/libraries-rerun \\
        --stats artifacts/intake/stats/raw/summary.json --matrix artifacts/intake/compatibility-matrix.json \\
        --clones artifacts/intake/clones.json --unittests artifacts/intake/unittests.json \\
        --benchmarks artifacts/benchmarks/external_compile/compile-results.jsonl \\
        --ecosystem artifacts/intake/ecosystem.json --probes artifacts/intake/probes.json \\
        --log pytest=PATH --log cargo-test=PATH --log rusty-cargo-test=PATH --log rusty-lit=PATH \\
        --output artifacts/intake/validation-report.json --markdown artifacts/intake/validation-report.md

``--compare`` names the output of a second, independent corpus run; every file
of both runs is compared byte by byte. Both runs must sit at the same depth
below the repository: ``compatibility.json`` holds paths relative to its own
directory. ``--log`` takes the console output of a
test suite and extracts its summary lines (pytest, cargo test, lit).
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
from pathlib import Path

from . import corpus, rusty
from .normalize.probes import probe_names
from .normalize.profiles import load_dialect
from .normalize.rules import RULES

SCHEMA = "semantist.intake-validation/1.0.0"


def _load(path: Path | None):
    if path is None or not path.exists():
        return None
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return json.loads(path.read_text(encoding="utf-8"))


def raw_statistics(stats: dict | None, top: int = 8) -> dict | None:
    """Root constructs (not caused by other errors) per family, by number of libraries."""
    if not stats:
        return None
    families = collections.defaultdict(list)
    for row in stats["rows"]:
        if row.get("root_libraries") and not row["construct"].startswith("cascade:"):
            families[row["family"]].append(row)
    result = {}
    for family, rows in sorted(families.items()):
        rows.sort(key=lambda r: (-r["root_libraries"], -r["root_diagnostics"], r["code"], r["construct"]))
        result[family] = [{k: r[k] for k in ("code", "construct", "root_libraries", "root_diagnostics")}
                          for r in rows[:top]]
    return {"families": result, "outcomes": stats.get("outcomes")}


def rule_hits(libraries: Path) -> dict:
    corpus = json.loads((libraries / "corpus.json").read_text(encoding="utf-8"))["libraries"]
    hits = collections.Counter()
    in_libraries = collections.Counter()
    per_family = collections.defaultdict(collections.Counter)
    for entry in corpus:
        for rule, count in (entry.get("rules_applied") or {}).items():
            hits[rule] += count
            in_libraries[rule] += 1
            per_family[entry["family"]][rule] += count
    probes = {}
    for name in probe_names():
        meta = json.loads((Path(__file__).parent / "normalize" / "probes" / name / "probe.json").read_text())
        probes.setdefault(meta["rule"], []).append(name)
    rules = []
    for rule_id, rule in RULES.items():
        rules.append({"rule": rule_id, "version": rule.spec.version, "level": rule.spec.level,
                      "edits": hits.get(rule_id, 0), "libraries": in_libraries.get(rule_id, 0),
                      "probes": probes.get(rule_id, [])})
    for rule_id in sorted(set(probes) - set(RULES)):
        rules.append({"rule": rule_id, "version": None, "level": "representation", "edits": hits.get(rule_id, 0),
                      "libraries": in_libraries.get(rule_id, 0), "probes": probes[rule_id]})
    return {"rules": rules, "per_family": {f: dict(sorted(c.items())) for f, c in sorted(per_family.items())},
            "rules_without_probe": [r["rule"] for r in rules if not r["probes"]]}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare_runs(first: Path, second: Path) -> dict:
    def files(root: Path) -> dict[str, Path]:
        return {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()}

    a, b = files(first), files(second)
    only_first = sorted(set(a) - set(b))
    only_second = sorted(set(b) - set(a))
    different = sorted(name for name in set(a) & set(b) if _digest(a[name]) != _digest(b[name]))
    return {"first": str(first), "second": str(second), "files_compared": len(set(a) & set(b)),
            "identical": not (only_first or only_second or different),
            "different": different[:50], "different_count": len(different),
            "only_in_first": only_first[:50], "only_in_second": only_second[:50]}


_PYTEST = re.compile(r"^=*\s*(\d+ \w+(?:, \d+ \w+(?: \w+)?)*) in [\d.]+s", re.M)
_CARGO = re.compile(r"^test result: (\w+)\. (\d+) passed; (\d+) failed; (\d+) ignored", re.M)
_LIT = re.compile(r"^\s+(Passed|Failed|Expectedly Failed|Unsupported|Unexpectedly Passed)\s*:\s*(\d+)", re.M)
_LIT_FAIL = re.compile(r"^FAIL: \S+ :: (\S+)", re.M)


def _fmt_bool(value) -> str:
    return "–" if value is None else ("yes" if value else "no")


def _fmt_count(result: dict) -> str:
    return str(result["total_count"]) if result.get("total_count") is not None else result.get("error", "–")


def summarize_log(name: str, path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    summary: dict = {"log": str(path)}
    pytest = _PYTEST.findall(text)
    if pytest:
        summary["pytest"] = {kind: int(n) for n, kind in
                             (part.split(" ", 1) for part in pytest[-1].split(", "))}
    cargo = _CARGO.findall(text)
    if cargo:
        summary["cargo"] = {"suites": len(cargo), "passed": sum(int(c[1]) for c in cargo),
                            "failed": sum(int(c[2]) for c in cargo), "ignored": sum(int(c[3]) for c in cargo),
                            "failed_suites": sum(1 for c in cargo if c[0] != "ok")}
    lit = _LIT.findall(text)
    if lit:
        summary["lit"] = {kind: int(n) for kind, n in lit}
        summary["lit"]["failed_tests"] = _LIT_FAIL.findall(text)
    return summary


def benchmarks(results: list | None) -> dict | None:
    if results is None:
        return None
    by_suite = collections.defaultdict(collections.Counter)
    for item in results:
        by_suite[item["suite"]][item["status"]] += 1
    return {"targets": len(results), "passed": sum(1 for r in results if r["status"] == "pass"),
            "by_suite": {s: dict(c) for s, c in sorted(by_suite.items())},
            "failed": [r["id"] for r in results if r["status"] != "pass"]}


def build(args) -> dict:
    matrix = _load(args.matrix)
    unittests = _load(args.unittests)
    report = {
        "schema_version": SCHEMA,
        "toolchain": rusty.toolchain_identity(),
        "claims": "Accepted means type check + LLVM IR + native link with the pinned, patched RuSTy. It is not "
                  "evidence of equal behaviour; behaviour evidence is graded separately (design 4.3).",
        "raw_statistics": raw_statistics(_load(args.stats)),
        "rules": rule_hits(args.libraries),
        "families": matrix["families"] if matrix else None,
        "excluded_vendors": matrix["excluded_vendors"] if matrix else None,
        "clone_totals": matrix.get("clone_totals") if matrix else None,
        "unit_tests": {"summary": unittests.get("summary"), "suites": [
            {k: s.get(k) for k in ("id", "library", "framework", "status", "passed", "test_cases", "reason")}
            for s in unittests.get("suites", [])]} if unittests else None,
        "benchmarks": benchmarks(_load(args.benchmarks)),
        "determinism": compare_runs(args.libraries, args.compare) if args.compare else None,
        "test_suites": {name: summarize_log(name, Path(path)) for name, path in
                        (item.split("=", 1) for item in args.log)},
        "ecosystem": _load(args.ecosystem),
        "probes": _load(args.probes),
    }
    report["line_levels"] = line_levels(report["families"], report["probes"])
    return report


def line_levels(families: list | None, probes: dict | None) -> list | None:
    """Whether a dialect line is treated at the depth of the priority A family.

    A line is at A level when (1) every rule of its dialect profiles has a
    passing probe, (2) it has a representation profile for its conversions,
    (3) at least one of its libraries is fully accepted, and (4) the share of
    its POUs rejected for a diagnostic that no rule handles is no higher than
    in the priority A family; every other rejection has a classified cause
    outside simple substitution (``matrix.rejection_cause``)."""
    if not families:
        return None
    passing = {p["rule"] for p in (probes or {}).get("probes", []) if p["passed"]}
    share = {f["family"]: f.get("rejection_causes", {}).get("unhandled_diagnostic", 0) / f["pous"]
             for f in families if f["pous"]}
    reference = max((share[f["family"]] for f in families if f["priority"] == "A" and f["family"] in share),
                    default=0.0)
    rows = []
    for f in families:
        if not f["pous"]:
            continue
        dialects = sorted({v.dialect for v in corpus.VENDORS if v.family == f["family"] and v.dialect})
        profiles = [load_dialect(d) for d in dialects]
        rules = sorted({r for d in profiles for r in d["rules"]})
        criteria = {
            "rules_probed": (not set(rules) - passing) if probes else None,
            "representation_profiles": sorted({c for d in profiles for c in d.get("compatibility_profiles", [])}),
            "full_libraries": f["syntax_levels"].get("full", 0),
            "unhandled_share": round(share[f["family"]], 4),
            "reference_share": round(reference, 4),
        }
        rows.append({"family": f["family"], "priority": f["priority"], "rules": rules, **criteria,
                     "a_level": bool(criteria["rules_probed"] and criteria["representation_profiles"]
                                     and criteria["full_libraries"] and share[f["family"]] <= reference)})
    return rows


def _pct(a, b) -> str:
    return f"{a / b:.1%}" if b else "–"


def markdown(report: dict) -> str:
    lines = ["# RuSTy compatibility layer: validation report", "",
             "Generated from `validation-report.json` by `python3 -m compiler.intake.validation`; the JSON is "
             "authoritative.", "", report["claims"], ""]
    tool = report["toolchain"]
    lines += ["## Toolchain", "", f"- RuSTy `{tool['rusty_revision']}`, `plc` sha256 `{tool['plc_sha256']}`",
              "- patches: " + ", ".join(f"`{p['name']}`" for p in tool["patches"]), ""]
    raw = report["raw_statistics"]
    if raw:
        lines += ["## Diagnostics before normalization", "",
                  "Root constructs per family (errors not caused by another error), by number of libraries.", ""]
        for family, rows in raw["families"].items():
            lines.append(f"- **{family}**: " + "; ".join(
                f"{r['code']} `{r['construct']}` ({r['root_libraries']} libraries)" for r in rows[:5]))
        lines.append("")
    if report["families"]:
        lines += ["## Intake results", "",
                  "| Family | Priority | Libraries | POUs accepted | Independent POUs (accepted) | Syntax levels |"
                  " Behaviour evidence |", "| --- | --- | ---: | ---: | ---: | --- | --- |"]
        for f in report["families"]:
            lines.append(
                f"| {f['family']} | {f['priority']} | {f['libraries']} | {f['pous_accepted']} / {f['pous']} "
                f"({_pct(f['pous_accepted'], f['pous'])}) | {f.get('independent_pous_accepted')} / "
                f"{f.get('independent_pous')} | "
                + ", ".join(f"{k} {v}" for k, v in sorted(f["syntax_levels"].items())) + " | "
                + ", ".join(f"{k} {v}" for k, v in sorted(f["behaviour_levels"].items())) + " |")
        lines += ["", "Per-library rows and failure reasons: `compatibility-matrix.json` / `.md`.", ""]
    rules = report["rules"]
    lines += ["## Rules", "", "| Rule | Version | Level | Edits | Libraries | Probes |",
              "| --- | ---: | --- | ---: | ---: | --- |"]
    for r in sorted(rules["rules"], key=lambda r: (-r["libraries"], r["rule"])):
        lines.append(f"| `{r['rule']}` | {r['version'] or '–'} | {r['level']} | {r['edits']} | {r['libraries']} | "
                     + ", ".join(r["probes"]) + " |")
    lines += ["", "Rules without a probe: " + (", ".join(rules["rules_without_probe"]) or "none"), ""]
    probes = report.get("probes")
    if probes:
        failed = [p["probe"] for p in probes["probes"] if not p["passed"]]
        lines += [f"Probes run with the pinned toolchain: {len(probes['probes']) - len(failed)} / "
                  f"{len(probes['probes'])} passed" + (f" (failed: {', '.join(failed)})" if failed else "") + ".", ""]
        lines += ["## Rules of the earlier converter", "",
                  "Every numbered rule of the earlier regex converter, and the constructs it did not cover, with "
                  "the pinned compiler's verdict on the original form (`compiler/intake/normalize/legacy.json`).",
                  "", "| Old rule | Form | Disposition | Taken over by | Pinned RuSTy | Check |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for item in probes["legacy"]:
            verdict = "rejects " + ", ".join(item["codes"]) if item["codes"] else "accepts"
            lines.append(f"| {item['legacy']} | {item['name']} | {item['disposition']} | "
                         f"{item['handled_by'] or '–'} | {verdict} | {'pass' if item['passed'] else 'FAIL'} |")
        lines.append("")
    levels = report.get("line_levels")
    if levels:
        lines += ["## Line levels", "",
                  "A line is at A level when every rule of its profiles has a passing probe, it has a "
                  "representation profile, at least one library is fully accepted, and its share of POUs "
                  "rejected for an unhandled diagnostic is no higher than in the priority A family "
                  "(`validation.line_levels`).", "",
                  "| Family | Priority | Rules probed | Representation profiles | Full libraries | "
                  "Unhandled share | A level |", "| --- | --- | --- | --- | ---: | ---: | --- |"]
        for row in levels:
            lines.append(f"| {row['family']} | {row['priority']} | {_fmt_bool(row['rules_probed'])} | "
                         f"{', '.join(row['representation_profiles']) or '–'} | {row['full_libraries']} | "
                         f"{row['unhandled_share']:.1%} | {'yes' if row['a_level'] else 'no'} |")
        lines.append("")
    if report["unit_tests"]:
        lines += ["## Library unit tests", "", "| Suite | Framework | Status | Passed | Reason |",
                  "| --- | --- | --- | --- | --- |"]
        # Suites that were not run with the same reason form one row.
        groups: dict = {}
        for s in report["unit_tests"]["suites"]:
            passed = f"{s['passed']} / {s['test_cases']}" if s.get("test_cases") is not None else "–"
            row = (s["id"], s["framework"], s["status"], passed, s.get("reason") or "")
            groups[row] = groups.get(row, 0) + 1
        lines[-2:] = ["| Suite | Framework | Libraries | Status | Passed | Reason |",
                      "| --- | --- | ---: | --- | --- | --- |"]
        for (suite, framework, status, passed, reason), count in groups.items():
            lines.append(f"| {suite} | {framework} | {count} | {status} | {passed} | {reason} |")
        lines.append("")
    bench = report["benchmarks"]
    if bench:
        lines += ["## Benchmark targets", "",
                  f"{bench['passed']} / {bench['targets']} targets build through the SemantiST pipeline "
                  "(recorded build-only runs). By suite: "
                  + ", ".join(f"{s} {c}" for s, c in bench["by_suite"].items())
                  + (". Failed: " + ", ".join(bench["failed"]) if bench["failed"] else "."), ""]
    det = report["determinism"]
    if det:
        lines += ["## Determinism", "",
                  f"Two independent corpus runs, {det['files_compared']} files compared byte by byte: "
                  + ("identical." if det["identical"] else
                     f"{det['different_count']} differ, {len(det['only_in_first'])} only in the first run, "
                     f"{len(det['only_in_second'])} only in the second."), ""]
    if report["test_suites"]:
        lines += ["## Test suites", ""]
        for name, s in report["test_suites"].items():
            parts = []
            if "pytest" in s:
                parts.append(", ".join(f"{v} {k}" for k, v in s["pytest"].items()))
            if "cargo" in s:
                c = s["cargo"]
                parts.append(f"cargo test: {c['passed']} passed, {c['failed']} failed, {c['ignored']} ignored "
                             f"in {c['suites']} test binaries")
            if "lit" in s:
                lit = s["lit"]
                parts.append("lit: " + ", ".join(f"{k} {v}" for k, v in lit.items() if k != "failed_tests")
                             + (" (failing: " + ", ".join(lit["failed_tests"]) + ")" if lit["failed_tests"] else ""))
            lines.append(f"- **{name}**: " + ("; ".join(parts) or "no summary found in the log"))
        lines.append("")
    eco = report["ecosystem"]
    if eco:
        lines += ["## GitHub ecosystem measurement", "", f"Status: {eco['status']}."
                  + (f" {eco['reason']}" if eco.get("reason") else ""), ""]
        code = eco.get("code_search") or {}
        if code.get("status"):
            lines += [f"Code search (files per format): {code['status']}."
                      + (f" {code['reason']}" if code.get("reason") else ""), ""]
            if code.get("results"):
                lines += ["| Format | Family | Query | Files |", "| --- | --- | --- | ---: |"]
                lines += [f"| {r['format']} | {r['family']} | `{r['query']}` | {_fmt_count(r)} |"
                          for r in code["results"]]
                lines.append("")
        topics = eco.get("repository_topics") or {}
        if topics.get("results"):
            lines += [f"Repository search by topic ({eco['date']}): {topics['status']}. Topics are set by the "
                      "repository owners and overlap; a tagged repository need not contain ST source.", "",
                      "| Family | Topic | Repositories |", "| --- | --- | ---: |"]
            lines += [f"| {r['family']} | `{r['query']}` | {_fmt_count(r)} |" for r in topics["results"]]
            lines.append("")
    if report["excluded_vendors"]:
        lines += ["## Not included", ""]
        lines += [f"- {v['vendor']}: {v['reason']}" for v in report["excluded_vendors"]]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--libraries", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--stats", type=Path)
    parser.add_argument("--matrix", type=Path)
    parser.add_argument("--clones", type=Path)
    parser.add_argument("--unittests", type=Path)
    parser.add_argument("--benchmarks", type=Path)
    parser.add_argument("--ecosystem", type=Path)
    parser.add_argument("--probes", type=Path)
    parser.add_argument("--log", action="append", default=[], help="NAME=PATH of a test-suite console log")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args(argv)
    report = build(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.write_text(markdown(report) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
