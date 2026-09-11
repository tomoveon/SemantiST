#!/usr/bin/env python3
"""Run the short four-tool comparison smoke profile."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.comparison.run_full_comparison import main  # noqa: E402


DEFAULT_ARGS = [
    "--experiment-id",
    "comparison_4tools_20s_smoke",
    "--target-id",
    "oscat_basic_charname",
    "--target-id",
    "icsfuzz_bf_mcpy_1",
    "--target-id",
    "scan_cycle_aircraft_oobw_4",
    "--trials",
    "1",
    "--fuzz-time",
    "20",
    "--tools",
    "semantist,aflplusplus,icsquartz,structuredfuzzer",
    "--container-runtime",
    "auto",
    "--cpus",
    "1-2",
    "--with-replay",
    "--with-coverage",
    "--validate-complete",
]


if __name__ == "__main__":
    raise SystemExit(main(DEFAULT_ARGS + sys.argv[1:], description=__doc__))
