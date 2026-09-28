#!/usr/bin/env python3
"""Compare independent runs offline, retaining failures and missing measurements."""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.comparison.lib.metrics import CONTRACT, paper_metrics
from experiments.comparison.lib.schema import validate_run_result, write_json


def load_runs(directories: list[Path]) -> list[dict[str, Any]]:
    """Read authoritative per-trial records; never reconstruct missing runs."""
    runs, seen = [], set()
    for directory in directories:
        directory = directory.resolve()
        manifest = json.loads((directory / "experiment-manifest.json").read_text(encoding="utf-8"))
        schedule_path = directory / "schedule.jsonl"
        # Unlike live event tails, a completed schedule must be fully valid JSON.
        schedule = [json.loads(line) for line in schedule_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        expected = {(tool, target["suite"], target["target_id"], trial)
                    for tool in manifest["tools"] for target in manifest["targets"]
                    for trial in range(1, manifest["trial_count"] + 1)}
        actual = {(r["tool"], r["suite"], r["target_id"], r["trial_id"]) for r in schedule}
        if actual != expected or len(schedule) != len(expected):
            raise ValueError(f"incomplete or duplicate schedule: {directory}")
        for row in schedule:
            key = (row["tool"], row["suite"], row["target_id"], row["trial_id"])
            if key in seen:
                raise ValueError(f"duplicate tool/target/trial across inputs: {key}")
            seen.add(key)
            path = directory / "runs" / row["tool"] / row["suite"] / row["target_id"] / f"trial_{int(row['trial_id']):02d}" / "run-result.json"
            if not path.is_file():
                raise ValueError(f"missing run result; finish or repair this run before comparison: {path}")
            run = json.loads(path.read_text(encoding="utf-8"))
            validate_run_result(run)
            if run.get("measurement_contract") != CONTRACT:
                raise ValueError(f"measurement contract absent or incompatible: {path}")
            if any(run.get(k) != row[k] for k in ("run_key", "tool", "suite", "target_id", "trial_id")):
                raise ValueError(f"run identity differs from schedule: {path}")
            if run["run_status"] == "missing":
                raise ValueError(f"unfinished run: {path}")
            run["result_path"] = str(path)
            run["paper_metrics"] = paper_metrics(run)
            runs.append(run)
    return runs


def export_metrics(runs: list[dict[str, Any]], out: Path) -> list[dict[str, Any]]:
    rows = [{**{k: run.get(k) for k in (
        "experiment_id", "run_key", "tool", "suite", "target_id", "trial_id",
        "run_status", "support_status", "online_budget_seconds", "online_elapsed_seconds",
        "per_execution_timeout_ms", "execution_timeout_model", "cpu_slots", "fuzz_workers",
        "initial_seed_provider", "rng_seed_applied", "result_path")}, **paper_metrics(run)} for run in runs]
    out.mkdir(parents=True, exist_ok=True)
    with (out / "metrics.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    write_csv(out / "metrics.csv", rows)
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        if rows:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def compare(directories: list[Path], out: Path) -> dict[str, Any]:
    if any(out.resolve() == directory.resolve() for directory in directories):
        raise ValueError("comparison output must differ from every input directory")
    runs = load_runs(directories)
    tools = sorted({r["tool"] for r in runs})
    if len(tools) < 2:
        raise ValueError("comparison requires at least two different tools")
    by_target = defaultdict(list)
    for run in runs:
        by_target[(run["suite"], run["target_id"])].append(run)
    common, notes = set(), []
    for target, target_runs in sorted(by_target.items()):
        supported = {r["tool"] for r in target_runs if r["support_status"] == "supported"}
        if supported != set(tools):
            notes.append({"target": target, "reason": "not_supported_or_selected_by_all_tools"})
            continue
        # Same checked-in fixture package; native parsers/harness wrappers may differ.
        for field in ("st_file_sha256", "benchmark_fixture_sha256", "online_budget_seconds", "per_execution_timeout_ms"):
            values = {json.dumps(r.get(field), sort_keys=True) for r in target_runs}
            if len(values) != 1 or any(r.get(field) is None for r in target_runs):
                raise ValueError(f"{target}: incompatible {field}")
        trials = [{r["trial_id"] for r in target_runs if r["tool"] == tool} for tool in tools]
        if any(trial_set != trials[0] for trial_set in trials[1:]):
            raise ValueError(f"{target}: tools have different trial sets")
        common.add(target)
        for field in ("execution_model", "execution_timeout_model", "cpu_slots"):
            values = {r[field] for r in target_runs}
            if len(values) > 1:
                notes.append({"target": target, "reason": f"different_{field}", "values": sorted(values),
                              "interpretation": "full-tool comparison; not identical per-execution cost or behavior"})
    # No fallback when the common intersection is empty.
    groups = defaultdict(list)
    for run in runs:
        groups[(run["suite"], run["target_id"], run["tool"], run["execution_model"])].append(run)
    summaries = []
    for (suite, target, tool, model), group in sorted(groups.items()):
        valid = [r["paper_metrics"] for r in group if r["paper_metrics"]["valid_run"]]
        successful = [m for m in valid if m["crash_found"]]
        def median(field):
            values = [m[field] for m in successful if m[field] is not None]
            return statistics.median(values) if values else None
        summaries.append({
            "suite": suite, "target_id": target, "tool": tool, "execution_model": model,
            "common_supported_target": (suite, target) in common,
            "scheduled_trials": len(group), "valid_trials": len(valid),
            "budget_completed_trials": sum(r["run_status"] == "budget_completed" for r in group),
            "early_completed_trials": sum(r["run_status"].startswith("early_completed_") for r in group),
            "failed_or_unsupported_trials": len(group) - len(valid),
            "crash_trials": len(successful), "no_crash_valid_trials": len(valid) - len(successful),
            "success_rate_among_valid_trials": len(successful) / len(valid) if valid else None,
            "missing_first_crash_count_trials": sum(m["first_crash_executions"] is None for m in successful),
            "median_first_crash_executions_successes_only": median("first_crash_executions"),
            "median_first_crash_time_seconds_successes_only": median("first_crash_time_seconds"),
            "replay_confirmed_trials": sum(m["replay_confirmed_crash_found"] is True for m in valid),
            "replay_unknown_trials": sum(m["replay_confirmed_crash_found"] is None for m in valid),
        })
    report = {"measurement_contract": CONTRACT, "tools": tools,
              "common_supported_targets": [list(k) for k in sorted(common)],
              "comparability_notes": notes, "per_target_tool": summaries}
    export_metrics(runs, out)
    write_json(out / "comparison.json", report)
    write_csv(out / "comparison.csv", summaries)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    compare(args.experiment_dir, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
