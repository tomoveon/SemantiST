"""semantist-timer-probe: run the CODESYS timer scenarios through RuSTy."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ST_FILE = ROOT / "tests" / "timer_semantics" / "timer_probe.st"
FIELDS = ["n", "scenario", "inX", "q0", "et0", "q50", "et50", "raw50",
          "qOf0", "qOf50", "etOf50", "qTp", "etTp", "qTrig"]


def compare_rows(left: list[dict[str, str]], right: list[dict[str, str]]) -> list[dict[str, object]]:
    """Compare both traces without dropping a trailing scan from either side."""
    differences: list[dict[str, object]] = []
    for index in range(max(len(left), len(right))):
        left_row = left[index] if index < len(left) else None
        right_row = right[index] if index < len(right) else None
        if left_row is None or right_row is None:
            differences.append({
                "scan": index + 1,
                "missing": "rusty_gt" if left_row is None else "iec_ge",
            })
            continue
        changed = {
            field: (left_row.get(field), right_row.get(field))
            for field in FIELDS
            if left_row.get(field) != right_row.get(field)
        }
        if changed:
            differences.append({"scan": index + 1, **changed})
    return differences


def build(semantics: str, run_dir: Path) -> Path:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["SEMANTIST_TIMER_SEMANTICS"] = "1" if semantics == "iec_ge" else "0"
    env["SEMANTIST_TIMER_SEMANTICS_NAME"] = semantics
    command = [
        sys.executable, "-m", "fuzzer.pipeline.core",
        "--mode", "hybrid", "--st-file", str(ST_FILE), "--function", "TIMER_PROBE",
        "--run-dir", str(run_dir), "--build-only",
    ]
    subprocess.run(command, cwd=ROOT, env=env, check=True, capture_output=True)
    target = ROOT / "artifacts" / "build" / "targets" / "timer_probe" / "fuzz_target"
    if not target.exists():
        raise SystemExit(f"timer probe target missing: {target}")
    return target


def run_semantics(semantics: str, run_dir: Path, frames: int) -> Path:
    target = build(semantics, run_dir)
    bundle = run_dir / "bundle"
    abi = json.loads((bundle / "abi.json").read_text(encoding="utf-8"))
    from fuzzer.corpus.frames import build_testcase
    from fuzzer.pipeline.probe import probe
    from fuzzer.pipeline.model import TargetModel, _AbiLike

    model = TargetModel.load(run_dir, bundle)
    frame = model.default_frame()
    frame["bRun"] = "1"
    testcase = build_testcase(_AbiLike(model), [frame] * frames)
    observation = probe(target, testcase, work_dir=run_dir / "probe", timeout_s=30.0)
    output = run_dir / f"{semantics}.csv"
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["scan", *FIELDS])
        for index, state in enumerate(observation.states):
            writer.writerow([index + 1, *[state.get(name) for name in FIELDS]])
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="all")
    parser.add_argument("--timer-semantics", choices=("both", "rusty_gt", "iec_ge"), default="both")
    parser.add_argument("--frames", type=int, default=1500)
    parser.add_argument("--out", type=Path, default=ROOT / "artifacts" / "timer-probe")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    semantics = ["rusty_gt", "iec_ge"] if args.timer_semantics == "both" else [args.timer_semantics]
    written = []
    for item in semantics:
        run_dir = args.out / f"run-{item}"
        output = run_semantics(item, run_dir, args.frames)
        destination = args.out / f"{item}.csv"
        shutil.copy2(output, destination)
        written.append(destination)
        print(f"[timer-probe] wrote {destination}")
    if len(written) == 2:
        rows = {}
        for path in written:
            with path.open(encoding="utf-8") as stream:
                rows[path.stem] = list(csv.DictReader(stream))
        differences = compare_rows(rows["rusty_gt"], rows["iec_ge"])
        (args.out / "diff.json").write_text(json.dumps(differences[:200], indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"[timer-probe] {len(differences)} differing scans; first: {differences[:3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
