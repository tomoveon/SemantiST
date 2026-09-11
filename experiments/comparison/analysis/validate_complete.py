#!/usr/bin/env python3
"""Validate structural completeness of a comparison experiment directory."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


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


def validate(experiment_dir: Path) -> list[str]:
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
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", required=True, type=Path)
    args = parser.parse_args()
    errors = validate(args.experiment_dir)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"complete: {args.experiment_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
