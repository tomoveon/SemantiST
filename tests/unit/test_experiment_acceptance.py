"""Acceptance regressions using fresh records only; no target or container execution."""

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from experiments.comparison.analysis.aggregate import (
    common_finding_summary, coverage_summary, performance_summary, semantist_semantic_summary,
)
from experiments.comparison.analysis.compare_tools import compare, export_metrics, load_runs
from experiments.comparison.analysis.coverage import merge_coverage_results
from experiments.comparison.analysis.replay import merge_replay_results
from experiments.comparison.analysis.validate_complete import validate
from experiments.comparison.lib.metrics import CONTRACT, paper_metrics
from experiments.comparison.lib.schema import run_result_template, write_json


def run_record(tool="semantist_full", **changes):
    run = run_result_template(
        experiment_id="acceptance", run_key=f"{tool}|target|1", tool=tool,
        target=SimpleNamespace(target_id="target", suite="suite", kind="FUNCTION", function="TEST"),
        trial_id=1, rng_seed_requested=7, rng_seed_applied=7, rng_seed_status="applied",
        online_budget_seconds=20, per_execution_timeout_ms=100,
        execution_timeout_model="per_process", support_status="supported",
    )
    run.update(measurement_contract=CONTRACT, run_status="budget_completed",
               preprocessing_status="completed", online_elapsed_seconds=20.0,
               total_executions=100, cpu_slots=1, st_file_sha256="fixture",
               benchmark_fixture_sha256={"harness.c": "fixture"})
    run.update(changes)
    return run


def save_records(directory, runs):
    directory.mkdir(parents=True, exist_ok=True)
    tools = sorted({run["tool"] for run in runs})
    write_json(directory / "experiment-manifest.json", {
        "tools": tools, "trial_count": 1,
        "configuration_id": next((run["configuration_id"] for run in runs
                                  if run["tool"] == "semantist_full"), "full"),
        "targets": [{"suite": "suite", "target_id": "target"}],
    })
    fields = ("tool", "suite", "target_id", "trial_id", "run_key")
    (directory / "schedule.jsonl").write_text(
        "".join(json.dumps({key: run[key] for key in fields}) + "\n" for run in runs), encoding="utf-8")
    (directory / "raw-runs.jsonl").write_text(
        "".join(json.dumps(run) + "\n" for run in runs), encoding="utf-8")
    with (directory / "support-matrix.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["tool", "target_id", "support_status"])
        writer.writeheader()
        writer.writerows({key: run[key] for key in writer.fieldnames} for run in runs)
    paths = []
    for run in runs:
        path = directory / "runs" / run["tool"] / run["suite"] / run["target_id"] / "trial_01" / "run-result.json"
        write_json(path, run)
        paths.append(path)
    return paths


class ExperimentAcceptanceTests(unittest.TestCase):
    def test_validity_requires_measured_full_budget_and_supported_execution(self):
        self.assertTrue(paper_metrics(run_record())["valid_run"])
        invalid_changes = [
            {"support_status": "unsupported"},
            {"run_status": "early_completed_with_finding"},
            {"run_status": "early_completed_no_finding"},
            {"run_status": "online_crashed"},
            {"run_status": "watchdog_timeout"},
            {"failure_reason": {"code": "client_failed"}},
            {"total_executions": 0}, {"total_executions": None},
            {"total_executions": True}, {"total_executions": 1.5},
            {"online_elapsed_seconds": 19.9}, {"online_elapsed_seconds": None},
            {"online_elapsed_seconds": float("nan")},
            {"online_budget_seconds": 0}, {"online_budget_seconds": None},
        ]
        for changes in invalid_changes:
            with self.subTest(changes=changes):
                self.assertFalse(paper_metrics(run_record(**changes))["valid_run"])

    def test_strict_acceptance_requires_a_valid_trial_for_every_tool(self):
        unsupported = run_record("icsquartz_full", support_status="unsupported",
                                 run_status="unsupported", total_executions=None,
                                 failure_reason={"code": "icsquartz_missing_root"})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for runs in ([unsupported], [run_record(), unsupported]):
                save_records(root, runs)
                self.assertEqual(validate(root), [])
                self.assertTrue(any("icsquartz_full: no valid" in error
                                    for error in validate(root, require_valid_runs=True)))

    def test_strict_acceptance_rejects_empty_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_records(root, [])
            self.assertTrue(validate(root, require_valid_runs=True))

    def test_strict_acceptance_recomputes_stale_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = run_record(total_executions=0, paper_metrics={
                "valid_run": True, "total_executions_reported": 100,
            })
            save_records(root, [run])
            errors = validate(root, require_valid_runs=True)
            self.assertTrue(any("valid_run" in error for error in errors))
            self.assertTrue(any("positive integer" in error for error in errors))
            run.update(total_executions=100, paper_metrics={"valid_run": False})
            save_records(root, [run])
            self.assertEqual(validate(root, require_valid_runs=True), [])

    def test_ablation_can_export_but_cannot_enter_main_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ablation = run_record(configuration_id="no_semantic_init")
            save_records(root / "ablation", [ablation])
            save_records(root / "afl", [run_record("aflplusplus_full")])
            rows = export_metrics(load_runs([root / "ablation"]), root / "export")
            self.assertEqual(rows[0]["configuration_id"], "no_semantic_init")
            with self.assertRaisesRegex(ValueError, "configuration_id=full"):
                compare([root / "ablation", root / "afl"], root / "comparison")

    def test_manifest_configuration_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = save_records(root, [run_record()])
            write_json(paths[0], run_record(configuration_id="no_semantic_init"))
            with self.assertRaisesRegex(ValueError, "configuration_id differs"):
                load_runs([root])

    def test_comparison_preserves_zero_execution_failure_outside_valid_denominator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_records(root / "semantist", [run_record(total_executions=0)])
            save_records(root / "afl", [run_record("aflplusplus_full")])
            report = compare([root / "semantist", root / "afl"], root / "comparison")
            row = next(row for row in report["per_target_tool"] if row["tool"] == "semantist_full")
            self.assertEqual(row["scheduled_trials"], 1)
            self.assertEqual(row["valid_trials"], 0)
            self.assertEqual(row["failed_or_unsupported_trials"], 1)

    def test_legacy_summaries_exclude_failed_early_and_late_discoveries(self):
        event = {"candidate_id": "synthetic", "oracle": "target_signal",
                 "observed_elapsed_seconds": 1, "execution_ordinal": 1}
        runs = [run_record(candidate_findings=[event], common_oracle_finding_found=True)]
        for status in ("online_crashed", "early_completed_with_finding", "early_completed_no_finding"):
            runs.append(run_record(run_status=status, semantic_objective_found=True,
                                   common_oracle_finding_found=True, candidate_findings=[event],
                                   tool_specific_metrics={"state_signature_total": 999}))
        runs.append(run_record(candidate_findings=[dict(event, observed_elapsed_seconds=21)],
                               common_oracle_finding_found=True))
        finding = common_finding_summary(runs)[0]
        self.assertEqual(finding["supported_trial_count"], 2)
        self.assertEqual(finding["common_oracle_finding_trials"], 1)
        self.assertEqual(finding["censored_trials"], 1)
        performance = performance_summary(runs)[0]
        self.assertEqual(performance["run_count"], 2)
        self.assertEqual(performance["excluded_run_count"], 3)
        semantic = semantist_semantic_summary(runs)[0]
        self.assertEqual(semantic["semantic_objective_trials"], 0)
        self.assertEqual(semantic["state_signature_total"], 0)
        self.assertFalse(coverage_summary([runs[1]])[0]["valid_run"])

    def test_offline_replay_and_coverage_cannot_repair_failed_or_early_native_run(self):
        event = {"candidate_id": "synthetic", "oracle": "target_signal",
                 "observed_elapsed_seconds": 1, "execution_ordinal": 1}
        for status in ("online_crashed", "early_completed_with_finding"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                run = run_record(run_status=status, failure_reason={"code": "native_failed"},
                                 candidate_findings=[event], finding_found=True,
                                 common_oracle_finding_found=True, first_common_finding=event,
                                 paper_metrics={"valid_run": True})
                path = save_records(root, [run])[0]
                merge_replay_results(root, [{
                    "run_key": run["run_key"], "candidate_id": event["candidate_id"],
                    "status": "confirmed", "confirmed": True, "returncode": -11,
                    "oracle": "target_signal",
                }])
                merge_coverage_results(root, [{
                    "run_key": run["run_key"], "status": "completed", "covered_slots": 12,
                    "covered_slots_sha256": "synthetic", "conversion_failures": 0,
                }])
                merged = json.loads(path.read_text(encoding="utf-8"))
                for key in ("run_status", "failure_reason", "total_executions", "online_elapsed_seconds"):
                    self.assertEqual(merged[key], run[key])
                self.assertTrue(merged["common_replay"]["confirmed"])
                self.assertEqual(merged["common_coverage"]["status"], "completed")
                self.assertFalse(merged["paper_metrics"]["valid_run"])


if __name__ == "__main__":
    unittest.main()
