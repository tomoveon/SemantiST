#!/usr/bin/env python3
"""Run the complete four-tool, 90-target comparison profile."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.comparison.run_full_comparison import main  # noqa: E402


DEFAULT_ARGS = [
    "--experiment-id",
    "comparison_4tools_90targets_300s_5trials",
    "--trials",
    "5",
    "--fuzz-time",
    "300",
    "--tools",
    "semantist,aflplusplus,icsquartz,structuredfuzzer",
    "--container-runtime",
    "auto",
    "--cpus",
    "1-8",
    "--with-replay",
    "--with-coverage",
    "--validate-complete",
]


if __name__ == "__main__":
    raise SystemExit(main(DEFAULT_ARGS + sys.argv[1:], description=__doc__))
