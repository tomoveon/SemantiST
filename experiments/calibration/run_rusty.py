#!/usr/bin/env python3
"""Run the platform calibration cases on RuSTy and fill the record sheet.

Run inside the SemantiST image from the repository root:

    podman run --rm -v "$PWD:/repo:Z" -w /repo -e PYTHONPATH=/repo IMAGE \
        python3 experiments/calibration/run_rusty.py

The cases are compiled together with the OSCAT compatibility sources and with
the sanitizers of the comparison experiment, so the values are what the
fuzzer observes for OSCAT targets. -O0 keeps runtime behaviour (for example a
trapping division) from being hidden by constant folding.
"""

from __future__ import annotations

import argparse
import csv
import os
import signal
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SOURCES = [HERE / "codesys" / "calib_cases.st", HERE / "codesys" / "calib_oscat.st"]
COMPATIBILITY = ROOT / "benchmarks" / "oscat_basic" / "source" / "compatibility.json"
RUNNABLE_KINDS = {"int", "real", "str", "oscat"}
# experiments/comparison/adapters/semantist.py
SANITIZERS = "-fsanitize=address -fno-omit-frame-pointer"


def build(work: Path) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    source = work / "calib_all.st"
    source.write_text("\n".join(path.read_text(encoding="utf-8") for path in SOURCES), encoding="utf-8")
    project_ll = work / "calib.ll"
    prepared_ll = work / "calib.prepared.ll"
    binary = work / "rusty_driver"
    llvm_bin = os.environ.get("SEMANTIST_LLVM_BIN", "/usr/lib/llvm-21/bin")
    steps = [
        [sys.executable, "-m", "compiler.scripts.compile_st_project", str(source), "CALIB_INT",
         "--plc", os.environ["RUSTY_COMPILER"],
         "--stdlib-glob", os.environ["SEMANTIST_RUSTY_STDLIB_GLOB"],
         "--compatibility-manifest", str(COMPATIBILITY),
         "--work-dir", str(work / "project"), "--out-ll", str(project_ll)],
        [sys.executable, "-m", "compiler.scripts.prepare_target_ir", str(project_ll), "CALIB_INT",
         "--public-api-extra", "CALIB_REAL,CALIB_STR,CALIB_OSCAT_STR",
         "--output-ll", str(prepared_ll), "--llvm-bin", llvm_bin],
        [f"{llvm_bin}/clang", "-O0", "-g", *SANITIZERS.split(), str(prepared_ll),
         str(HERE / "rusty_driver.c"), os.environ["SEMANTIST_RUSTY_STDLIB_LIB"],
         "-ldl", "-lpthread", "-lm", "-o", str(binary)],
    ]
    env = {**os.environ, "SEMANTIST_TARGET_SANITIZERS": SANITIZERS}
    for step in steps:
        subprocess.run(step, cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
    return binary


def observe(binary: Path, kind: str, case_id: str) -> str:
    try:
        run = subprocess.run([str(binary), kind, case_id], capture_output=True, text=True,
                             errors="replace", timeout=10)
    except subprocess.TimeoutExpired:
        return "超时"
    panic = next((line for line in run.stderr.splitlines() if "panicked at" in line), None)
    if panic is not None:
        message = run.stderr.split(panic, 1)[1].strip().splitlines()
        return f"panic: {message[0] if message else panic}"
    report = next((line for line in run.stderr.splitlines() if "ERROR: AddressSanitizer:" in line), None)
    if report is not None:
        return "ASan: " + report.split("ERROR: AddressSanitizer:", 1)[1].split(" on ")[0].strip()
    if run.returncode < 0:
        return f"崩溃: {signal.Signals(-run.returncode).name}"
    if run.returncode != 0:
        return f"退出码 {run.returncode}"
    return run.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work-dir", type=Path, default=ROOT / "artifacts" / "calibration" / "rusty")
    parser.add_argument("--record-sheet", type=Path, default=HERE / "record_sheet.csv")
    args = parser.parse_args()

    binary = build(args.work_dir)
    with (HERE / "cases.csv").open(encoding="utf-8", newline="") as handle:
        cases = list(csv.DictReader(handle))
    with args.record_sheet.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "case", "trap", "expect_codesys", "rusty", "codesys", "note", "relates"])
        for case in cases:
            if case["kind"] in RUNNABLE_KINDS:
                rusty = observe(binary, case["kind"], case["id"])
            else:
                rusty = "仅 CODESYS"
            writer.writerow([case["id"], case["case"], case["trap"], case["expect_codesys"],
                             rusty, "", "", case["relates"]])
            print(f"{case['id']:>5}  {rusty}")
    print(f"wrote {args.record_sheet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
