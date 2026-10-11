#!/usr/bin/env python3
"""Validate structural completeness of a comparison experiment directory."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.comparison.lib.metrics import paper_metrics


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"missing file: {path}")
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError(f"{path}:{line_no}: invalid JSON: {error}") from error
    return rows


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"missing file: {path}")
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def validate(experiment_dir: Path, *, require_valid_runs: bool = False) -> list[str]:
    experiment_dir = experiment_dir.resolve()
    errors: list[str] = []
    schedule = read_jsonl(experiment_dir / "schedule.jsonl")
    runs = read_jsonl(experiment_dir / "raw-runs.jsonl")
    support = read_csv(experiment_dir / "support-matrix.csv")

    schedule_keys = [row["run_key"] for row in schedule]
    run_keys = [row["run_key"] for row in runs]
    if len(runs) != len(schedule):
        errors.append(f"raw-runs.jsonl line count {len(runs)} != schedule rows {len(schedule)}")
    duplicated = [key for key, count in Counter(run_keys).items() if count != 1]
    if duplicated:
        errors.append(f"run_key values must appear exactly once; duplicated={duplicated[:10]}")
    missing = sorted(set(schedule_keys) - set(run_keys))
    if missing:
        errors.append(f"missing scheduled run records: {missing[:10]}")
    unexpected = sorted(set(run_keys) - set(schedule_keys))
    if unexpected:
        errors.append(f"unexpected run records: {unexpected[:10]}")

    tools = {row["tool"] for row in schedule}
    targets = {row["target_id"] for row in schedule}
    trial_ids = {int(row["trial_id"]) for row in schedule}
    expected_support_rows = len(tools) * len(targets)
    if len(support) != expected_support_rows:
        errors.append(f"support-matrix.csv rows {len(support)} != {expected_support_rows}")

    missing_status_count = sum(1 for run in runs if run.get("run_status") == "missing")
    if missing_status_count:
        errors.append(f"run_status=missing count must be 0, got {missing_status_count}")

    unsupported_pairs = {
        (row["tool"], row["target_id"])
        for row in support
        if row.get("support_status") == "unsupported"
    }
    expected_unsupported_runs = len(unsupported_pairs) * len(trial_ids)
    actual_unsupported_runs = sum(1 for run in runs if run.get("run_status") == "unsupported")
    if actual_unsupported_runs != expected_unsupported_runs:
        errors.append(
            "unsupported run count "
            f"{actual_unsupported_runs} != unsupported support rows x trials {expected_unsupported_runs}"
        )
    for run in runs:
        status = run.get("run_status")
        if status in {
            "preprocessing_failed",
            "online_start_failed",
            "online_crashed",
            "watchdog_timeout",
            "runner_error",
            "unsupported",
        } and not run.get("failure_reason"):
            errors.append(f"{run.get('run_key')}: {status} missing failure_reason")
        if not require_valid_runs or run.get("support_status") != "supported":
            continue
        if status != "budget_completed":
            errors.append(
                f"{run.get('run_key')}: supported trial did not complete its online budget "
                f"(run_status={status!r})"
            )
        # Exported metrics may predate offline analysis or the acceptance rules.
        paper = paper_metrics(run)
        if paper.get("valid_run") is not True:
            errors.append(f"{run.get('run_key')}: paper_metrics.valid_run is not true")
        executions = paper.get("total_executions_reported")
        if not isinstance(executions, int) or isinstance(executions, bool) or executions <= 0:
            errors.append(
                f"{run.get('run_key')}: total_executions_reported must be a positive integer"
            )
    if require_valid_runs:
        if not tools:
            errors.append("no tools or trials were scheduled")
        valid_tools = {run.get("tool") for run in runs if paper_metrics(run)["valid_run"]}
        for tool in sorted(tools - valid_tools):
            errors.append(f"{tool}: no valid full-budget trial with native executions")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", required=True, type=Path)
    parser.add_argument(
        "--require-valid-runs",
        action="store_true",
        help="also reject supported trials that did not finish the budget with native executions",
    )
    args = parser.parse_args()
    errors = validate(args.experiment_dir, require_valid_runs=args.require_valid_runs)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"complete: {args.experiment_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
