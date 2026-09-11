#!/usr/bin/env python3
"""Aggregate comparison run records into summary artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


CSV_FIELDS = [
    "tool",
    "suite",
    "target_id",
    "kind",
    "function",
    "trial_id",
    "support_status",
    "preprocessing_status",
    "run_status",
    "finding_found",
    "common_oracle_finding_found",
    "semantic_objective_found",
    "first_common_finding_time_seconds",
    "first_common_finding_executions",
    "total_executions",
    "execs_per_sec",
    "corpus_count",
    "crash_count_raw",
    "timeout_count_raw",
    "online_elapsed_seconds",
    "preprocessing_time_seconds",
    "failure_reason_code",
]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def aggregate_experiment(experiment_dir: Path) -> dict[str, Path]:
    experiment_dir = experiment_dir.resolve()
    runs = read_jsonl(experiment_dir / "raw-runs.jsonl")
    if not runs:
        raise ValueError(f"no run records found: {experiment_dir / 'raw-runs.jsonl'}")
    summaries_dir = experiment_dir / "summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)
    write_raw_csv(experiment_dir / "raw-runs.csv", runs)
    paths = {
        "support": summaries_dir / "support.json",
        "findings_common_oracle": summaries_dir / "findings_common_oracle.json",
        "findings_semantist_semantic": summaries_dir / "findings_semantist_semantic.json",
        "coverage_common": summaries_dir / "coverage_common.json",
        "performance": summaries_dir / "performance.json",
        "stability": summaries_dir / "stability.json",
    }
    write_json(paths["support"], support_summary(runs, experiment_dir / "support-matrix.csv"))
    write_json(paths["findings_common_oracle"], common_finding_summary(runs))
    write_json(paths["findings_semantist_semantic"], semantist_semantic_summary(runs))
    write_json(paths["coverage_common"], coverage_summary(runs))
    write_json(paths["performance"], performance_summary(runs))
    write_json(paths["stability"], stability_summary(runs))
    return paths


def write_raw_csv(path: Path, runs: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for run in runs:
            first = run.get("first_common_finding") or {}
            failure = run.get("failure_reason") or {}
            writer.writerow(
                {
                    "tool": run.get("tool"),
                    "suite": run.get("suite"),
                    "target_id": run.get("target_id"),
                    "kind": run.get("kind"),
                    "function": run.get("function"),
                    "trial_id": run.get("trial_id"),
                    "support_status": run.get("support_status"),
                    "preprocessing_status": run.get("preprocessing_status"),
                    "run_status": run.get("run_status"),
                    "finding_found": run.get("finding_found"),
                    "common_oracle_finding_found": run.get("common_oracle_finding_found"),
                    "semantic_objective_found": run.get("semantic_objective_found"),
                    "first_common_finding_time_seconds": first.get("observed_elapsed_seconds")
                    or first.get("tool_reported_time_seconds"),
                    "first_common_finding_executions": first.get("execution_ordinal"),
                    "total_executions": run.get("total_executions"),
                    "execs_per_sec": run.get("execs_per_sec"),
                    "corpus_count": run.get("corpus_count"),
                    "crash_count_raw": run.get("crash_count_raw"),
                    "timeout_count_raw": run.get("timeout_count_raw"),
                    "online_elapsed_seconds": run.get("online_elapsed_seconds"),
                    "preprocessing_time_seconds": run.get("preprocessing_time_seconds"),
                    "failure_reason_code": failure.get("code"),
                }
            )


def support_summary(runs: list[dict[str, Any]], support_matrix_path: Path) -> dict[str, Any]:
    by_tool_suite = Counter((run["tool"], run["suite"], run["support_status"]) for run in runs)
    unsupported_runs = [run for run in runs if run.get("run_status") == "unsupported"]
    seed_missing = [run for run in runs if run.get("rng_seed_status") != "applied"]
    support_rows = _read_csv(support_matrix_path)
    support_counts = Counter((row["tool"], row["suite"], row["support_status"]) for row in support_rows)
    return {
        "run_record_count": len(runs),
        "tool_count": len({run["tool"] for run in runs}),
        "target_count": len({run["target_id"] for run in runs}),
        "trial_count_max": max(int(run["trial_id"]) for run in runs),
        "support_matrix_rows": len(support_rows),
        "support_matrix_counts": _counter_to_records(support_counts, ["tool", "suite", "support_status"]),
        "run_support_counts": _counter_to_records(by_tool_suite, ["tool", "suite", "support_status"]),
        "unsupported_run_count": len(unsupported_runs),
        "seed_control_missing_run_count": len(seed_missing),
        "common_supported_targets": sorted(
            _common_supported_targets(support_rows, {run["tool"] for run in runs})
        ),
    }


def common_finding_summary(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for (tool, suite), group in sorted(_group_by(runs, "tool", "suite").items()):
        targets = {run["target_id"] for run in group}
        supported = [run for run in group if run.get("support_status") == "supported"]
        finding_runs = [run for run in supported if run.get("common_oracle_finding_found")]
        times = [
            _first_value(run, "observed_elapsed_seconds", "tool_reported_time_seconds")
            for run in finding_runs
        ]
        execs = [_first_value(run, "execution_ordinal") for run in finding_runs]
        result.append(
            {
                "tool": tool,
                "suite": suite,
                "target_count": len(targets),
                "supported_target_count": len({run["target_id"] for run in supported}),
                "trial_count": len(group),
                "supported_trial_count": len(supported),
                "common_oracle_finding_trials": len(finding_runs),
                "common_oracle_finding_targets": len({run["target_id"] for run in finding_runs}),
                "unique_confirmed_faults": len(
                    {fault for run in finding_runs for fault in run.get("unique_confirmed_fault_ids", [])}
                ),
                "median_first_common_finding_time_seconds": _median([t for t in times if t is not None]),
                "median_first_common_finding_executions": _median([e for e in execs if e is not None]),
                "censored_trials": len(
                    [run for run in supported if not run.get("common_oracle_finding_found")]
                ),
            }
        )
    return result


def semantist_semantic_summary(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    semantist_runs = [run for run in runs if run["tool"] == "semantist_full"]
    result = []
    for suite, group in sorted(_group_by(semantist_runs, "suite").items()):
        semantic_runs = [run for run in group if run.get("semantic_objective_found")]
        events = [
            event
            for run in semantic_runs
            for event in run.get("candidate_findings", [])
            if event.get("oracle") == "semantic_objective"
        ]
        phases = Counter(event.get("discovery_phase") for event in events)
        ratios_mapped = []
        ratios_total = []
        state_total = 0
        cycle_total = 0
        for run in group:
            metrics = run.get("tool_specific_metrics") or {}
            if metrics.get("mapped_semantic_coverage_ratio") is not None:
                ratios_mapped.append(metrics["mapped_semantic_coverage_ratio"])
            if metrics.get("total_stg_coverage_ratio") is not None:
                ratios_total.append(metrics["total_stg_coverage_ratio"])
            state_total += int(metrics.get("state_signature_total") or 0)
            cycle_total += int(metrics.get("cycle_ids_observed_total") or 0)
        result.append(
            {
                "suite": suite,
                "target_count": len({run["target_id"] for run in group}),
                "trial_count": len(group),
                "semantic_objective_trials": len(semantic_runs),
                "semantic_objective_targets": len({run["target_id"] for run in semantic_runs}),
                "initial_ingress_findings": phases.get("initial_ingress", 0),
                "semantic_generation_findings": phases.get("semantic_generation", 0),
                "fuzzing_findings": phases.get("fuzzing", 0),
                "mapped_semantic_coverage_ratio_mean": _mean(ratios_mapped),
                "total_stg_coverage_ratio_mean": _mean(ratios_total),
                "state_signature_total": state_total,
                "cycle_ids_observed_total": cycle_total,
            }
        )
    return result


def coverage_summary(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for run in runs:
        coverage = run.get("common_coverage") or {}
        rows.append(
            {
                "tool": run.get("tool"),
                "suite": run.get("suite"),
                "target_id": run.get("target_id"),
                "trial_id": run.get("trial_id"),
                "covered_slots": coverage.get("covered_slots"),
                "covered_slots_sha256": coverage.get("covered_slots_sha256"),
                "total_executions": run.get("total_executions"),
                "execs_per_sec": run.get("execs_per_sec"),
                "corpus_count": run.get("corpus_count"),
                "online_elapsed_seconds": run.get("online_elapsed_seconds"),
                "preprocessing_time_seconds": run.get("preprocessing_time_seconds"),
                "coverage_status": coverage.get("status"),
            }
        )
    return rows


def performance_summary(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for (tool, suite), group in sorted(_group_by(runs, "tool", "suite").items()):
        execs = [run.get("total_executions") for run in group if run.get("total_executions") is not None]
        eps = [run.get("execs_per_sec") for run in group if run.get("execs_per_sec") is not None]
        result.append(
            {
                "tool": tool,
                "suite": suite,
                "run_count": len(group),
                "budget_completed": len([run for run in group if run.get("run_status") == "budget_completed"]),
                "total_executions_median": _median(execs),
                "execs_per_sec_median": _median(eps),
                "online_elapsed_seconds_median": _median(
                    [
                        run.get("online_elapsed_seconds")
                        for run in group
                        if run.get("online_elapsed_seconds") is not None
                    ]
                ),
            }
        )
    return result


def stability_summary(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(
        (
            run.get("tool"),
            run.get("run_status"),
            (run.get("failure_reason") or {}).get("code"),
        )
        for run in runs
    )
    return [
        {
            "tool": tool,
            "run_status": status,
            "failure_reason_code": code,
            "count": count,
        }
        for (tool, status, code), count in sorted(counts.items())
    ]


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _counter_to_records(counter: Counter, fields: list[str]) -> list[dict[str, Any]]:
    records = []
    for key, count in sorted(counter.items()):
        key_tuple = key if isinstance(key, tuple) else (key,)
        record = {field: value for field, value in zip(fields, key_tuple)}
        record["count"] = count
        records.append(record)
    return records


def _common_supported_targets(support_rows: list[dict[str, str]], tools: set[str]) -> set[str]:
    by_target: dict[str, dict[str, str]] = defaultdict(dict)
    for row in support_rows:
        by_target[row["target_id"]][row["tool"]] = row["support_status"]
    return {
        target_id
        for target_id, statuses in by_target.items()
        if tools <= statuses.keys() and all(statuses[tool] == "supported" for tool in tools)
    }


def _group_by(rows: list[dict[str, Any]], *keys: str) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = tuple(row[key_name] for key_name in keys)
        if len(key) == 1:
            key = key[0]
        grouped[key].append(row)
    return grouped


def _first_value(run: dict[str, Any], *keys: str) -> Any:
    first = run.get("first_common_finding") or {}
    for key in keys:
        if first.get(key) is not None:
            return first[key]
    return None


def _median(values: list[Any]) -> Any:
    return statistics.median(values) if values else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", required=True, type=Path)
    args = parser.parse_args()
    paths = aggregate_experiment(args.experiment_dir)
    for name, path in paths.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
