#!/usr/bin/env python3
"""Short interface smoke for full and multi-cycle-disabled SemantiST profiles."""

from __future__ import annotations

import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.ablations.run_ablation_suite import main as suite_main, suite_arguments  # noqa: E402
from experiments.comparison.run_full_comparison import parse_args  # noqa: E402


DEFAULT_ARGS = [
    "--run-prefix", "ablation_smoke",
    "--variants", "full,no_multicycle_guidance",
    "--target-id", "scan_cycle_smart_oobw_1",
    "--trials", "1",
    "--fuzz-time", "20",
    "--cpus", "1-2",
]


def main(argv: list[str] | None = None) -> int:
    arguments = DEFAULT_ARGS + (sys.argv[1:] if argv is None else argv)
    prefix, variants, forwarded = suite_arguments(arguments)
    if "--preflight-only" in forwarded:
        raise SystemExit("smoke requires online execution, not --preflight-only")
    result = suite_main(arguments)
    if result != 0:
        return result
    config = parse_args(["--tools", "semantist_full", *forwarded])
    for variant in variants:
        root = config.artifact_root.resolve() / f"{prefix}_{variant}" / "runs" / "semantist_full"
        records = sorted(root.rglob("run-result.json"))
        if not records:
            print(f"[ablation-smoke] missing trial records: {root}", file=sys.stderr)
            return 1
        for path in records:
            trial = json.loads(path.read_text(encoding="utf-8"))
            metrics = trial.get("paper_metrics") or {}
            if (
                trial.get("run_status") != "budget_completed"
                or not metrics.get("valid_run")
                or not (metrics.get("total_executions_reported") or 0) > 0
            ):
                print(f"[ablation-smoke] invalid trial: {path}", file=sys.stderr)
                return 1
    print(f"[ablation-smoke] {len(variants)} profiles passed online interface checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
