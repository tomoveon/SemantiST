#!/usr/bin/env python3
"""Generate the RuSTy PLC_PRG wrapper for all benchmark targets.

Usage:
    python3 benchmarks/generate_benchmark_files.py [--suite SUITE] [--id ID] [--dry-run]

Writes program.st next to each existing target.st. The harnesses
(harness.c, semantist_harness.c, semantist.seed) need the compiled IR and are
generated afterwards by benchmarks/generate_harness.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.lib.generator import ROOT, build_program_st, load_targets
from benchmarks.lib.st_extract import extract_signature


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", action="append", default=[])
    parser.add_argument("--id", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    targets = load_targets()
    if args.suite:
        targets = [t for t in targets if t.suite in args.suite]
    if args.id:
        targets = [t for t in targets if t.id in args.id or t.function in args.id]

    report: dict[str, object] = {"targets": []}
    failures = 0
    for t in targets:
        entry: dict[str, object] = {
            "suite": t.suite,
            "id": t.id,
            "function": t.function,
            "kind": t.kind,
            "target_sha256": sha256(Path(t.st_file)),
        }
        try:
            sig = extract_signature(t.st_file, t.function)
            program_st = build_program_st(sig)
            entry["program_st"] = "ok"
            if not args.dry_run:
                (t.directory / "program.st").write_text(program_st, encoding="utf-8", newline="\n")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            entry["error"] = str(exc)
            entry["program_st"] = "error"

        report["targets"].append(entry)
        status = entry.get("program_st", "?")
        print(f"[gen] {t.suite}::{t.id} program.st={status}", flush=True)

    out = ROOT / "artifacts" / "benchmarks" / "generation-report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"[gen] wrote {out}")
    print(f"[gen] total={len(targets)} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
