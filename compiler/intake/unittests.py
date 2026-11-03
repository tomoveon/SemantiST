"""Run the unit tests that libraries ship with, on RuSTy (behaviour evidence).

Each suite names its framework, the library files that hold it and how to run
it. A suite is *run* only when the framework and the test units were accepted
by the intake (type check, IR, link); otherwise it is recorded as ``not_run``
with the first reason, and never adapted by hand. Results are pass counts as
reported by the framework itself.

Supported runners:

* ``unitest`` -- UniTest (CODESYS 3, B&R): every ``test_*`` PROGRAM is
  called once per cycle, ``RunAll`` is pulsed in the second cycle (the
  operator's "run all tests" command), until no test case is running; the
  summaries in the global ``Results`` array are printed.

Recorded as not runnable, with the reason:

* TcUnit (TwinCAT 3) -- needs Beckhoff libraries outside the corpus;
* AxUnit (SIMATIC AX test projects) -- framework not in the corpus;
* JiecUnit (Sysmac, KEYENCE) -- framework only, no test programs collected;
* GX Works test projects -- expected values only in comments, no assertions.

    python3 -m compiler.intake.unittests --libraries artifacts/intake/libraries \\
        --output artifacts/intake/unittests.json
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import tempfile
from pathlib import Path

from . import rusty
from .build import LLVM_BIN, blank, internalize
from .library import compile_natives
from .structure import split_units

MAX_CYCLES = 200

SUITES = [
    {"id": "unitest-codesys", "framework": "UniTest", "runner": "unitest",
     "library": "01_CODESYS3/xml_UniTest_codesys__UniTest"},
    {"id": "unitest-br", "framework": "UniTest", "runner": "unitest",
     "library": "08_BR_Automation_Studio/tkucic__UniTest__7d8bbb1752ac__UniTest"},
    {"id": "tctransform", "framework": "TcUnit", "runner": None,
     "library": "03_TwinCAT3/xml_BurksEngineering__TcTransform__9ae8dbf16c8b.zip__TcTransformTest",
     "reason": "TcUnit reports through Tc2_System/Tc2_Utilities (ADSLOGSTR, file access), Beckhoff libraries "
               "that are not part of the corpus"},
    {"id": "struckig", "framework": "TcUnit", "runner": None,
     "library": "03_TwinCAT3/xml_stefanbesler__struckig__233fbdb72463.zip__Struckig_unittest",
     "reason": "TcUnit reports through Tc2_System/Tc2_Utilities, Beckhoff libraries that are not part of the corpus"},
    {"id": "tcopen", "framework": "TcUnit", "runner": None, "library": "03_TwinCAT3/xml_TcOpen__*Tests",
     "reason": "TcOpen tests use TcUnit and Tc2_* Beckhoff libraries that are not part of the corpus"},
    {"id": "axopen", "framework": "AxUnit", "runner": None,
     "library": "30_Siemens_SIMATIC_AX/*__test_project",
     "reason": "AxUnit ({Test} methods, AxUnit.Assert) is a Siemens package that is not part of the corpus"},
    {"id": "jiecunit", "framework": "JiecUnit", "runner": None, "library": "*/JiecUnit_*",
     "reason": "the corpus holds the JiecUnit framework only (no JiecLib test programs); it also needs the "
               "Sysmac clock Get100nsCnt"},
    {"id": "gxworks", "framework": "GX Works test project", "runner": None,
     "library": "19_Mitsubishi_GXWorks/*__test_project",
     "reason": "the test programs exercise the library but state expected values only in comments; there is "
               "no assertion to evaluate"},
]


def _accepted(report: dict) -> dict[str, dict]:
    return {unit["name"].upper(): unit for unit in report["units"]}


ENTRY = "semantist_unittest_entry"


def run_unitest(directory: Path, report: dict, work: Path) -> dict:
    manifest = json.loads((directory / "compatibility.json").read_text(encoding="utf-8"))
    units = _accepted(report)
    tests = sorted(u["name"] for u in units.values() if u["kind"] == "PROGRAM" and u["name"].lower().startswith("test_"))
    runnable = [name for name in tests if units[name.upper()]["accepted"]]
    if not tests:
        return {"status": "not_run", "test_programs_discovered": 0,
                "reason": "the library holds the framework only: no test_* PROGRAM to run"}
    if not runnable:
        first = units[tests[0].upper()]["reasons"][0]
        return {"status": "not_run", "test_programs_discovered": len(tests),
                "reason": "no test program accepted by the intake: " + str(first.get("message"))[:200]}
    texts = {}
    for source in manifest["library"]["normalized_sources"]:
        path = directory / source
        text = path.read_text(encoding="utf-8")
        spans = []
        if path.name == report["target_identity"]["source"]["file"]:
            # Build the accepted units only (validation subset).
            for unit in split_units(text):
                info = units.get(unit.qualified_name.upper())
                if info is not None and not info["accepted"]:
                    spans.append((unit.start, unit.end))
        texts[path.name] = blank(text, spans)
    match = re.search(r"(?im)^\s*Results\s*:\s*ARRAY\s*\[\s*(\d+)\s*\.\.\s*(\d+)\s*\]", "\n".join(texts.values()))
    if not match:
        return {"status": "not_run", "reason": "UniTest Results array not found"}
    low, high = match.groups()
    calls = "\n".join(f"        {name}();" for name in runnable)
    driver = f"""{{external}}
FUNCTION printf: DINT
VAR_INPUT {{ref}} str : STRING; END_VAR
VAR_INPUT args: ...; END_VAR
END_FUNCTION

FUNCTION semantist_unittest_entry : DINT
VAR
    cycle : DINT;
    k : DINT;
    running : BOOL;
END_VAR
FOR cycle := 1 TO {MAX_CYCLES} DO
    (* UniTest's operator command: a rising edge of RunAll starts every test
       case that its SETUP action put into ut_SETUP during the first cycle. *)
    RunAll := cycle = 2;
{calls}
    running := FALSE;
    FOR k := {low} TO {high} DO
        IF Results[k].TestsRunning THEN running := TRUE; END_IF;
    END_FOR;
    IF NOT running AND cycle > 3 THEN EXIT; END_IF;
END_FOR;
FOR k := {low} TO {high} DO
    IF Results[k].Id > 0 THEN
        printf('RESULT %d %d %d %d$N', UINT_TO_DINT(Results[k].Id), UINT_TO_DINT(Results[k].TotalTests),
               UINT_TO_DINT(Results[k].TestsPassed), UINT_TO_DINT(Results[k].TestsFailed));
    END_IF;
END_FOR;
semantist_unittest_entry := 0;
END_FUNCTION
"""
    work.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, text in texts.items():
        path = work / name
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    driver_path = work / "unittest_driver.st"
    driver_path.write_text(driver, encoding="utf-8")
    compat = [(directory / p).resolve() for p in manifest["library"]["compatibility_sources"]]
    ir = work / "suite.ll"
    build = rusty.check([*paths, driver_path, *compat], ir=ir, extra=manifest.get("compiler", {}).get("args", []))
    if not build.ok:
        first = build.errors[0].message.split("\n", 1)[0] if build.errors else (build.panic or build.output[-300:])
        return {"status": "not_run", "reason": "test build failed: " + first, "tests_discovered": len(tests)}
    public = work / "suite.public.ll"
    if internalize(ir, public, [ENTRY]).returncode == 0:
        ir = public
    # The library may define its own PROGRAM main; the C entry calls the driver.
    entry = work / "entry.c"
    entry.write_text(f"int {ENTRY}(void);\nint main(void) {{ return {ENTRY}(); }}\n", encoding="utf-8")
    natives = compile_natives([(directory / p).resolve() for p in manifest["native"]["sources"]] + [entry],
                              work / "native")
    executable = work / "suite"
    link = subprocess.run([str(LLVM_BIN / "clang"), "-Wno-override-module", str(ir), *natives,
                           str(rusty.runtime_archive()), "-ldl", "-lpthread", "-lm", "-o", str(executable)],
                          capture_output=True, text=True)
    if link.returncode:
        return {"status": "not_run", "reason": "test link failed: " + link.stderr[-300:], "tests_discovered": len(tests)}
    try:
        result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"status": "failed_to_complete", "reason": "test run timed out", "tests_discovered": len(tests)}
    rows = [list(map(int, line.split()[1:])) for line in result.stdout.splitlines() if line.startswith("RESULT ")]
    total = sum(r[1] for r in rows)
    passed = sum(r[2] for r in rows)
    failed = sum(r[3] for r in rows)
    return {
        "status": "run" if result.returncode == 0 else "crashed",
        "returncode": result.returncode,
        "test_programs_discovered": len(tests),
        "test_programs_run": len(runnable),
        "test_programs_not_accepted": sorted(set(tests) - set(runnable)),
        "test_cases": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": round(passed / total, 4) if total else None,
        "stderr": result.stderr[-1000:],
    }


def run_suites(libraries: Path) -> dict:
    results = []
    for suite in SUITES:
        vendor, pattern = suite["library"].split("/", 1)
        matches = sorted(d for v in sorted(libraries.glob(vendor)) if v.is_dir() for d in v.iterdir()
                         if d.is_dir() and fnmatch.fnmatch(d.name, pattern))
        if not matches:
            results.append({**{k: suite[k] for k in ("id", "framework")}, "library": suite["library"],
                            "status": "not_run", "reason": "library not ingested"})
            continue
        for directory in matches:
            entry = {"id": suite["id"], "framework": suite["framework"],
                     "library": f"{directory.parent.name}/{directory.name}"}
            report_path = directory / "compatibility-report.json"
            if not report_path.is_file():
                results.append({**entry, "status": "not_run", "reason": "no intake report"})
                continue
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if suite["runner"] is None:
                results.append({**entry, "status": "not_run", "reason": suite["reason"],
                                 "pous_accepted": report["syntax"]["pous_accepted"], "pous": report["syntax"]["pous"]})
                continue
            with tempfile.TemporaryDirectory(prefix="semantist-unittest-") as work:
                results.append({**entry, **run_unitest(directory, report, Path(work))})
    return {"schema_version": "semantist.library-unit-tests/1.0.0", "suites": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--libraries", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run_suites(args.libraries)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for suite in result["suites"]:
        print(suite["id"], suite["library"], suite["status"], suite.get("passed"), suite.get("test_cases"),
              suite.get("reason", "")[:100])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
