#!/usr/bin/env python3
"""Run one tool independently; collect metrics without replay or coverage jobs."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.comparison import run_full_comparison as runner
from experiments.comparison.analysis.compare_tools import export_metrics, load_runs

TOOLS = {"semantist": "semantist_full", "aflplusplus": "aflplusplus_full",
         "icsquartz": "icsquartz_full", "icsfuzz": "icsfuzz_full"}


def runner_arguments(argv: list[str]) -> list[str]:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--tool", required=True, choices=TOOLS)
    parser.add_argument("--run-id", required=True, help="new output directory name")
    args, remaining = parser.parse_known_args(argv)
    if not args.run_id or args.run_id in {".", ".."} or any(c in args.run_id for c in "/\\:|"):
        parser.error("--run-id must be a directory name, not a path")
    forbidden = {"--tools", "--experiment-id", "--with-replay", "--with-coverage", "--resume", "--overwrite"}
    if any(arg.split("=", 1)[0] in forbidden for arg in remaining):
        parser.error("use a fresh --run-id; tool selection and offline analysis are separate")
    return [*remaining, "--tools", TOOLS[args.tool], "--experiment-id", args.run_id,
            "--serial", "--skip-aggregate", "--validate-complete"]


def main(argv: list[str] | None = None) -> int:
    forwarded = runner_arguments(sys.argv[1:] if argv is None else argv)
    config = runner.parse_args(forwarded)
    result = runner.main(forwarded)
    if not config.preflight_only:
        directory = config.artifact_root.resolve() / config.experiment_id
        export_metrics(load_runs([directory]), directory)
        print(f"[single-tool] metrics: {directory / 'metrics.csv'}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
