#!/usr/bin/env python3
"""Write the ExecutionBundle documents for one built target."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from compiler.harness.abi import AbiError, load_target_abi
from compiler.harness.contract import write_bundle_documents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("function")
    parser.add_argument("--ir", required=True, type=Path)
    parser.add_argument("--state-ir", required=True, type=Path)
    parser.add_argument("--stg-model", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path, help="bundle directory")
    parser.add_argument("--st-file", required=True, type=Path)
    parser.add_argument("--target-binary", required=True, type=Path)
    parser.add_argument("--compatibility-manifest", type=Path)
    parser.add_argument("--scan-period-ns", type=int, default=10_000_000)
    parser.add_argument("--dt-range-ns", nargs=2, type=int)
    parser.add_argument("--timer-semantics", choices=("rusty_gt", "iec_ge"), default="iec_ge")
    parser.add_argument("--loop-backedges-per-scan", type=int, default=1_000_000)
    parser.add_argument("--clock-origin-ns", type=int, default=0)
    parser.add_argument("--symbolic", choices=("supported", "unsupported"), default="unsupported")
    parser.add_argument("--symbolic-reason", default="not built")
    args = parser.parse_args(argv)
    try:
        abi = load_target_abi(args.stg_model, args.ir, args.function)
    except AbiError as error:
        print(f"[bundle] {args.function}: {error}")
        return 1
    paths = write_bundle_documents(
        args.out,
        abi,
        args.state_ir,
        st_file=args.st_file,
        function=args.function,
        target_binary=args.target_binary,
        compatibility_manifest=args.compatibility_manifest,
        scan_period_ns=args.scan_period_ns,
        dt_range_ns=tuple(args.dt_range_ns) if args.dt_range_ns else None,
        timer_semantics=args.timer_semantics,
        loop_backedges_per_scan=args.loop_backedges_per_scan,
        clock_origin_ns=args.clock_origin_ns,
        symbolic=args.symbolic,
        symbolic_reason=args.symbolic_reason,
    )
    print(f"[bundle] wrote {paths['contract']}, {paths['abi']}, {paths['state_layout']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
