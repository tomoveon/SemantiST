"""Freeze the targeted 100-second development pilot without changing the paper matrix."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from fuzzer.experiments.artifacts import write_json
from fuzzer.experiments.matrix import MANIFEST, ROOT, Job, validate_job

SELECTION = ROOT / "benchmarks/experiments/improvement_pilot.json"
EXPECTED_GROUPS = {
    "baseline_gap_function": 10,
    "baseline_gap_fb": 5,
    "engine_failure": 1,
    "partial_gap": 1,
    "multicycle_regression": 3,
}


def build_matrix() -> dict:
    selection = json.loads(SELECTION.read_text(encoding="utf-8"))
    if selection.get("schema_version") != "semantist.improvement-pilot/1":
        raise ValueError("unexpected improvement pilot schema")
    if selection.get("online_seconds") != 100 or selection.get("seed") != 0:
        raise ValueError("the development pilot must use 100 seconds and seed 0")
    if selection.get("tools") != ["semantist", "aflpp", "icsquartz"]:
        raise ValueError("the development pilot must include all three tools")
    chosen = selection.get("targets") or []
    if len(chosen) != 20 or len({item["id"] for item in chosen}) != 20:
        raise ValueError("the development pilot must contain 20 unique targets")
    if Counter(item["group"] for item in chosen) != EXPECTED_GROUPS:
        raise ValueError("the development pilot target groups changed")

    roster = json.loads(MANIFEST.read_text(encoding="utf-8"))["targets"]
    targets = {item["id"]: item for item in roster}
    jobs = []
    for item in chosen:
        target = targets.get(item["id"])
        if target is None or target["kind"] != item["kind"]:
            raise ValueError(f"pilot target is absent or changed kind: {item['id']}")
        for tool in selection["tools"]:
            job = Job(target, tool, "full", selection["seed"],
                      selection["online_seconds"], ["overall"])
            validate_job(job)
            jobs.append(job.as_dict())
    return {
        "schema_version": "semantist.experiment-matrix/1",
        "pilot_selection": str(SELECTION.relative_to(ROOT)),
        "jobs": jobs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True,
                        help="new directory for the pilot's frozen builds, plans and runs")
    args = parser.parse_args()
    root = args.root.resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"pilot root is not empty: {root}; use a new directory")
    matrix = build_matrix()
    destination = root / "matrix.json"
    write_json(destination, matrix)
    print(f"wrote {destination} ({len(matrix['jobs'])} jobs, 20 targets, 100 seconds each)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
