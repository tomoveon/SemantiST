#!/usr/bin/env python3
"""Run one SemantiST mechanism ablation with the comparison runner's raw records."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.comparison.ablations import ABLATION_PROFILES  # noqa: E402
from experiments.comparison.run_tool import main as run_tool_main  # noqa: E402


def runner_arguments(argv: list[str]) -> list[str]:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--variant", required=True, choices=ABLATION_PROFILES)
    parser.add_argument("--run-id", required=True)
    args, remaining = parser.parse_known_args(argv)
    forbidden = {"--tool", "--tools", "--semantist-ablation", "--experiment-id"}
    if any(value.split("=", 1)[0] in forbidden for value in remaining):
        parser.error("tool and configuration are selected by --variant")
    return [
        "--tool", "semantist",
        "--run-id", args.run_id,
        "--semantist-ablation", args.variant,
        *remaining,
    ]


def main(argv: list[str] | None = None) -> int:
    return run_tool_main(runner_arguments(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
