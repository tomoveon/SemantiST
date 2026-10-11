#!/usr/bin/env python3
"""Run a SemantiST experiment preset with optional tools, targets and budgets."""

from experiments.comparison.run_full_comparison import main


if __name__ == "__main__":
    raise SystemExit(main(description=__doc__))
