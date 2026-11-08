"""Freeze the fixed-seed semantic/raw mutation smoke comparison."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fuzzer.experiments.artifacts import write_json
from fuzzer.experiments.matrix import MANIFEST, ROOT

SELECTION = ROOT / "benchmarks/experiments/mutation_short.json"
MODES = ("semantic-only", "byte-only", "combined")


def build_plan() -> dict:
    selection = json.loads(SELECTION.read_text(encoding="utf-8"))
    if selection.get("schema_version") != "semantist.mutation-short/1":
        raise ValueError("unexpected mutation short-test schema")
    if selection.get("tool") != "semantist" or selection.get("seed") != 0:
        raise ValueError("the mutation short test must use SemantiST seed 0")
    if selection.get("online_seconds") != 10:
        raise ValueError("the mutation short test must use the ten-second smoke budget")
    if selection.get("modes") != list(MODES):
        raise ValueError("the mutation short-test modes changed")
    targets = json.loads(MANIFEST.read_text(encoding="utf-8"))["targets"]
    by_id = {target["id"]: target for target in targets}
    target_ids = selection.get("targets") or []
    if len(target_ids) != 6 or len(set(target_ids)) != 6:
        raise ValueError("the mutation short test must contain six unique targets")
    chosen = []
    for target_id in target_ids:
        target = by_id.get(target_id)
        if target is None or target["kind"] != "FUNCTION_BLOCK":
            raise ValueError(f"short-test target is absent or not a FUNCTION_BLOCK: {target_id}")
        chosen.append(target)
    jobs = []
    for mode in MODES:
        for target in chosen:
            jobs.append({
                "target": target,
                "tool": "semantist",
                "configuration": "no_llm",
                "mutation_mode": mode,
                "semantic_ratio": selection["semantic_ratio"],
                "seed": selection["seed"],
                "online_seconds": selection["online_seconds"],
            })
    return {
        "schema_version": "semantist.mutation-short-plan/1",
        "selection": str(SELECTION.relative_to(ROOT)),
        "jobs": jobs,
        "records": [
            "first finding",
            "native confirmation",
            "executions",
            "scans",
            "throughput",
            "state progress",
            "parser rejects",
            "timeouts",
            "run failures",
            "raw mutation executions",
            "raw mutation retained entries",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True,
                        help="new directory for the frozen short-test plan")
    args = parser.parse_args()
    root = args.root.resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"short-test root is not empty: {root}")
    plan = build_plan()
    write_json(root / "mutation-short-plan.json", plan)
    print(f"wrote {root / 'mutation-short-plan.json'} (18 jobs, seed 0, 10 seconds each)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
