#!/usr/bin/env python3
"""Run the short three-tool comparison smoke profile."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.comparison.run_full_comparison import main  # noqa: E402


DEFAULT_ARGS = ["--experiment", "smoke"]


if __name__ == "__main__":
    raise SystemExit(main(DEFAULT_ARGS + sys.argv[1:], description=__doc__))
