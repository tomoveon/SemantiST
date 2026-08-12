#!/usr/bin/env python3
"""CLI wrapper for deterministic structured seed minimization."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fuzzer.reporting.minimize import FINDING_STATUSES, minimize_seed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimize a confirmed SemantiST structured seed")
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--seed", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", required=True, type=float)
    parser.add_argument("--baseline-status", choices=sorted(FINDING_STATUSES))
    parser.add_argument(
        "--metadata",
        type=Path,
        help="metadata output path; defaults to --output with a .json suffix",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = minimize_seed(
        target=args.target,
        seed=args.seed,
        output=args.output,
        timeout=args.timeout,
        baseline_status=args.baseline_status,
        metadata=args.metadata,
    )
    print(json.dumps(result.to_json(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
