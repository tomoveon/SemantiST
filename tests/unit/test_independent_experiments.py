"""Fresh synthetic fixtures only: no historical experiment artifacts."""
import json
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from experiments.comparison.adapters import ADAPTERS
from experiments.comparison.adapters.base import ExperimentConfig
from experiments.comparison.analysis.compare_tools import compare, load_runs
from experiments.comparison.analysis.validate_complete import validate as validate_complete
from experiments.comparison.analysis.replay import merge_replay_results, replay_seed
from experiments.comparison.lib.metrics import (
    CONTRACT, FindingCollectionError, FindingObserver, paper_metrics,
    post_run_events, read_events, reported_executions,
)
from experiments.comparison.lib.process import run_with_budget
from experiments.comparison.lib import process as process_module
from experiments.comparison.lib.schema import run_result_template, write_json
from experiments.comparison.run_tool import runner_arguments
from experiments.comparison.run_full_comparison import parse_args


def config(root):
    return ExperimentConfig(
        experiment_id="fresh", semantist_root=ROOT, workspace_root=ROOT.parent,
        artifact_root=root, icsquartz_root=ROOT.parent / "ICSQuartz", online_budget_seconds=10,
        per_execution_timeout_ms=1000, semantic_task_budget=1000,
        semantist_fb_max_cycles=10000, semantist_fb_stale_threshold=0,
        semantist_fb_state_trace=True, observer_poll_interval_ms=10, cpuset=None,
        container_runtime="docker", container_runtime_status="test fixture only",
        container_platform=None, semantist_image="test")


def crash(candidate="first", count=7, elapsed=0.2, **extra):
    return dict(candidate_id=candidate, oracle="target_signal", execution_ordinal=count,
                execution_count_source="synthetic_test", observed_elapsed_seconds=elapsed,
                artifact_path=f"inputs/{candidate}", **extra)


def run_record(tool="semantist_full", events=None, target_id="target", *, post_run=False):
    result = run_result_template(
        experiment_id=tool, run_key=f"{tool}|{tool}|suite|{target_id}|trial_01", tool=tool,
        target=SimpleNamespace(suite="suite", target_id=target_id, kind="FUNCTION", function="TEST"),
        trial_id=1, rng_seed_requested=1, rng_seed_applied=1, rng_seed_status="applied",
        online_budget_seconds=10, per_execution_timeout_ms=1000,
        execution_timeout_model="per_process", support_status="supported")
    # Existing timing tests exercise the historical observed-time contract.
    # New snapshots explicitly opt in and never inherit synthetic host times.
    if not post_run:
        result.pop("collection_mode")
        result["observer_poll_interval_ms"] = 10
    result.update(measurement_contract=CONTRACT if post_run else "semantist.independent-tool-metrics/1",
                  candidate_collection_complete=True, run_status="budget_completed",
                  st_file_sha256="synthetic_source", benchmark_fixture_sha256={"harness.c": "synthetic_harness"},
                  cpu_slots=1, online_elapsed_seconds=10, total_executions=100)
    events = post_run_events(events or []) if post_run else events
    result["candidate_findings"] = events or []
    result["finding_found"] = bool(events)
    result["common_oracle_finding_found"] = bool(events)
    result["first_common_finding"] = events[0] if events else None
    return result


def save_independent(directory, run):
    write_json(directory / "experiment-manifest.json", {
        "tools": [run["tool"]], "trial_count": 1,
        "targets": [{"suite": run["suite"], "target_id": run["target_id"]}]})
    row = {k: run[k] for k in ("tool", "suite", "target_id", "trial_id", "run_key")}
    (directory / "schedule.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    result = directory / "runs" / run["tool"] / run["suite"] / run["target_id"] / "trial_01" / "run-result.json"
    write_json(result, run)
    return result


class IndependentExperimentTests(unittest.TestCase):
    def test_first_crash_associates_counter_time_and_input(self):
        result = paper_metrics(run_record(events=[crash("a", 40, .1), crash("z", 7, .2)]))
        self.assertEqual(result["first_crash_executions"], 7)
        self.assertEqual(result["first_crash_time_seconds"], .2)
        self.assertEqual(result["first_crash_input"], "inputs/z")
        self.assertEqual(result["execs_per_sec"], 10)
        self.assertIsNone(result["replay_confirmed_crash_found"])

    def test_missing_count_not_guessed_and_zero_preserved(self):
        result = paper_metrics(run_record(events=[crash(count=None)]))
        self.assertIsNone(result["first_crash_executions"])
        self.assertEqual(result["first_crash_count_status"], "unavailable")
        self.assertEqual(paper_metrics(run_record(events=[crash(count=0)]))["first_crash_executions"], 0)
        self.assertIsNone(paper_metrics(run_record())["first_crash_time_seconds"])

    def test_non_crashes_and_late_artifacts_do_not_count(self):
        events = [crash(elapsed=11), dict(crash("semantic"), oracle="semantic_objective"),
                  dict(crash("hang"), oracle="testcase_timeout")]
        result = paper_metrics(run_record(events=events))
        self.assertFalse(result["crash_found"])
        self.assertEqual(result["late_crash_artifact_count"], 1)

    def test_missing_timestamp_is_not_a_budgeted_discovery(self):
        result = paper_metrics(run_record(events=[crash(elapsed=None)]))
        self.assertFalse(result["crash_found"])
        self.assertEqual(result["unmeasured_crash_artifact_count"], 1)

    def test_post_run_snapshot_counts_crashes_without_fabricated_host_timing(self):
        result = paper_metrics(run_record(events=[crash(elapsed=None)], post_run=True))
        self.assertTrue(result["valid_run"])
        self.assertTrue(result["crash_found"])
        self.assertEqual(result["crash_artifact_count"], 1)
        self.assertIsNone(result["late_crash_artifact_count"])
        self.assertIsNone(result["first_crash_time_seconds"])
        self.assertEqual(result["first_crash_executions"], 7)
        self.assertEqual(result["contract"], CONTRACT)
        self.assertEqual(result["collection_mode"], "post_run")

    def test_incomplete_empty_post_run_collection_is_unknown_not_no_crash(self):
        run = run_record(post_run=True)
        run["candidate_collection_complete"] = False
        result = paper_metrics(run)
        self.assertFalse(result["valid_run"])
        self.assertIsNone(result["crash_found"])
        self.assertIsNone(result["crash_artifact_count"])

    def test_post_run_preserves_native_time_without_using_it_as_host_time(self):
        result = paper_metrics(run_record(events=[crash(
            native_time_to_find_seconds=3.5, native_time_origin="libafl_state_start_time",
        )], post_run=True))
        self.assertIsNone(result["first_crash_time_seconds"])
        self.assertEqual(result["first_crash_native_time_seconds"], 3.5)
        self.assertEqual(result["first_crash_native_time_origin"], "libafl_state_start_time")

    def test_reported_total_is_read_not_inferred_from_speed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "new-stdout.log"
            path.write_text("executions: 3, exec/sec: 800\nexecutions: 120, exec/sec: 12\n", encoding="utf-8")
            self.assertEqual(reported_executions(path), 120)
            path.write_text("executions: 1.2M, exec/sec: 90000\n", encoding="utf-8")
            self.assertIsNone(reported_executions(path))

    def test_late_metadata_updates_without_retiming(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "measurements.jsonl"
            observer = FindingObserver(time.monotonic_ns(), journal)
            observer.observe([crash(count=None)])
            first = dict(observer.events[0])
            observer.observe([crash(count=42)], after_budget=True)
            self.assertEqual(observer.events[0]["execution_ordinal"], 42)
            self.assertEqual(observer.events[0]["observed_monotonic_ns"], first["observed_monotonic_ns"])
            self.assertFalse(observer.events[0]["observed_after_budget"])
            self.assertEqual(len(read_events(journal)), 2)
            observer.observe([dict(crash(count=None), execution_count_source="unavailable")])
            self.assertEqual(observer.events[0]["execution_count_source"], "synthetic_test")

    def test_real_subprocess_evidence_is_collected_after_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "new-crash"
            result = run_with_budget(
                [sys.executable, "-c", "from pathlib import Path; Path('new-crash').write_bytes(b'x')"],
                cwd=root, env=None, stdout_path=root / "stdout", stderr_path=root / "stderr",
                budget_seconds=2, poll_interval_ms=10,
                scan_findings=lambda: [crash()] if marker.exists() else [])
            self.assertEqual(result["returncode"], 0)
            self.assertEqual(len(result["events"]), 1)
            self.assertTrue((root / "post-run-findings.json").is_file())
            self.assertFalse((root / "measurement-events.jsonl").exists())
            self.assertIsNone(result["events"][0]["observed_elapsed_seconds"])
            self.assertTrue(result["collection_complete"])
            self.assertTrue(result["cleanup_confirmed"])

    def test_post_run_collection_error_preserves_stopped_lifecycle_and_readable_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(
                "experiments.comparison.lib.process._terminate_process",
                wraps=process_module._terminate_process,
            ) as terminate:
                result = run_with_budget(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    cwd=root, env=None, stdout_path=root / "stdout",
                    stderr_path=root / "stderr", budget_seconds=2,
                    poll_interval_ms=10,
                    scan_findings=lambda: (_ for _ in ()).throw(
                        FindingCollectionError("post-run parse failure", [crash()])
                    ),
                )
            terminate.assert_called_once()
            self.assertTrue(result["timed_out"])
            self.assertGreaterEqual(result["elapsed_seconds"], 2)
            self.assertTrue(result["cleanup_confirmed"])
            self.assertFalse(result["infrastructure_failure"])
            self.assertFalse(result["collection_complete"])
            self.assertIn("post-run parse failure", result["collection_error"])
            self.assertEqual(result["events"][0]["candidate_id"], "first")
            self.assertIsNone(result["events"][0]["observed_elapsed_seconds"])
            persisted = json.loads((root / "process-result.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["elapsed_seconds"], result["elapsed_seconds"])
            self.assertTrue(persisted["cleanup_confirmed"])

    def test_semantist_post_run_scanner_reports_missing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = ADAPTERS["semantist_full"](config(Path(tmp)))
            target = SimpleNamespace(target_id="target", suite="suite")
            run_dir = adapter.run_dir(target, 1)
            report = run_dir / "findings" / "fuzzing" / "report.json"
            report.parent.mkdir(parents=True)
            report.write_text('{"kind":"asan","executions":1}', encoding="utf-8")
            with patch.object(Path, "read_text", side_effect=FileNotFoundError):
                with self.assertRaises(FindingCollectionError) as failure:
                    adapter.scan_finding_events(target, run_dir)
            self.assertEqual(failure.exception.events, [])

    def test_semantist_collection_error_preserves_other_readable_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = ADAPTERS["semantist_full"](config(Path(tmp)))
            target = SimpleNamespace(target_id="target", suite="suite")
            run_dir = adapter.run_dir(target, 1)
            folder = run_dir / "findings" / "fuzzing"
            folder.mkdir(parents=True)
            (folder / "good.json").write_text('{"kind":"asan","executions":1}', encoding="utf-8")
            (folder / "partial.json").write_text('{"kind":', encoding="utf-8")
            with self.assertRaises(FindingCollectionError) as failure:
                adapter.scan_finding_events(target, run_dir)
            self.assertEqual(len(failure.exception.events), 1)
            self.assertEqual(failure.exception.events[0]["execution_ordinal"], 1)

    def test_native_metadata_not_misidentified_as_crash(self):
        for tool, relative in (("icsquartz_full", "tool-artifacts/crashes"),):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as tmp:
                adapter = ADAPTERS[tool](config(Path(tmp)))
                target = SimpleNamespace(target_id="target", suite="suite")
                run_dir = adapter.run_dir(target, 1)
                folder = run_dir / relative
                folder.mkdir(parents=True)
                (folder / "seed").write_bytes(b"test")
                (folder / ".seed.metadata").write_text('{"executions": 123}', encoding="utf-8")
                (folder / ".seed.lock").write_bytes(b"")
                events = adapter.scan_finding_events(target, run_dir)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["execution_ordinal"], 123)
                (folder / "same-input").write_bytes(b"test")
                (folder / ".same-input.metadata").write_text('{"executions": 234}', encoding="utf-8")
                repeated = adapter.scan_finding_events(target, run_dir)
                self.assertEqual(len({e["candidate_id"] for e in repeated}), 2)
                (folder / ".seed.metadata").write_text('{"executions":', encoding="utf-8")
                partial = next(e for e in adapter.scan_finding_events(target, run_dir) if e["artifact_path"].endswith("/seed"))
                self.assertIsNone(partial["execution_ordinal"])

    def test_semantist_new_report_contains_count_and_real_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = ADAPTERS["semantist_full"](config(Path(tmp)))
            target = SimpleNamespace(target_id="target", suite="suite")
            run_dir = adapter.run_dir(target, 1)
            seed = run_dir / "findings" / "initial" / "hash.seed"
            seed.parent.mkdir(parents=True)
            seed.write_bytes(b"X,INT,1\n")
            report = dict(kind="asan", executions=17, content_hash="hash", source="initial",
                          reproducer="/artifacts/" + seed.relative_to(Path(tmp)).as_posix())
            write_json(seed.with_suffix(".json"), report)
            with patch("experiments.comparison.adapters.semantist.read_events", side_effect=AssertionError("unnecessary event log read")):
                event = adapter.scan_finding_events(target, run_dir)[0]
            self.assertEqual(event["execution_ordinal"], 17)
            self.assertEqual(event["execution_count_source"], "finding_record")
            self.assertIsNone(event["artifact_path"])
            self.assertTrue(event["reproducer_artifact_path"].endswith("hash.seed"))
            del report["executions"]
            write_json(seed.with_suffix(".json"), report)
            self.assertIsNone(adapter.scan_finding_events(target, run_dir)[0]["execution_ordinal"])
            (run_dir / "events.jsonl").write_text(json.dumps(dict(
                event="runtime_finding", content_hash="hash", source="initial", executions=19)) + "\n", encoding="utf-8")
            self.assertEqual(adapter.scan_finding_events(target, run_dir)[0]["execution_ordinal"], 19)

    def test_replay_does_not_confirm_arbitrary_failure_or_runtime_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.write_bytes(b"input")
            for code, text in ((1, ""), (137, ""), (-15, ""), (1, "ASan runtime does not come first"),
                               (1, "Warning: Malformed line detected"), (1, "Sanitizer enabled")):
                with self.subTest(code=code, text=text), patch("subprocess.run", return_value=SimpleNamespace(
                        returncode=code, stdout=b"", stderr=text.encode())):
                    self.assertFalse(replay_seed(root, root / "target", seed, 1)["confirmed"])

    def test_unverified_replay_preserves_native_measurements(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = run_record(events=[crash()])
            path = save_independent(root, run)
            merge_replay_results(root, [dict(run_key=run["run_key"], candidate_id="first",
                                            status="conversion_unsupported", confirmed=False, returncode=None)])
            updated = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(updated["common_oracle_finding_found"])
            self.assertEqual(updated["paper_metrics"]["first_crash_executions"], 7)
            self.assertIsNone(updated["paper_metrics"]["replay_confirmed_crash_found"])
            self.assertEqual(updated["unique_confirmed_fault_ids"], [])

    def test_compare_independent_outputs_and_no_crash_is_not_zero_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b = root / "a", root / "b"
            save_independent(a, run_record(events=[crash()]))
            save_independent(b, run_record("aflplusplus_full"))
            result = compare([a, b], root / "out")
            self.assertEqual(result["common_supported_targets"], [["suite", "target"]])
            row = next(r for r in result["per_target_tool"] if r["tool"] == "aflplusplus_full")
            self.assertEqual(row["success_rate_among_valid_trials"], 0)
            self.assertIsNone(row["median_first_crash_time_seconds_successes_only"])
            self.assertTrue((root / "out" / "metrics.csv").is_file())

    def test_compare_rejects_mismatches_duplicates_and_missing_trials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b = root / "a", root / "b"
            save_independent(a, run_record())
            other = run_record("aflplusplus_full")
            other["online_budget_seconds"] = 20
            path = save_independent(b, other)
            with self.assertRaisesRegex(ValueError, "online_budget_seconds"):
                compare([a, b], root / "out")
            other["online_budget_seconds"] = 10
            other["rng_seed_requested"] = 999
            save_independent(b, other)
            with self.assertRaisesRegex(ValueError, "paired RNG seeds"):
                compare([a, b], root / "out")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                load_runs([a, a])
            path.unlink()
            with self.assertRaisesRegex(ValueError, "missing run result"):
                load_runs([b])

    def test_empty_intersection_stays_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b = root / "a", root / "b"
            save_independent(a, run_record())
            save_independent(b, run_record("aflplusplus_full", target_id="another"))
            report = compare([a, b], root / "out")
            self.assertEqual(report["common_supported_targets"], [])

    def test_failed_trial_is_reported_and_excluded_from_success_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b = root / "a", root / "b"
            save_independent(a, run_record())
            failed = run_record("aflplusplus_full", events=[crash()])
            failed.update(run_status="online_crashed", failure_reason={"code": "test", "message": "test"})
            save_independent(b, failed)
            row = next(r for r in compare([a, b], root / "out")["per_target_tool"] if r["tool"] == "aflplusplus_full")
            self.assertEqual(row["failed_or_unsupported_trials"], 1)
            self.assertEqual(row["crash_trials"], 0)
            self.assertIsNone(row["success_rate_among_valid_trials"])

    def test_strict_completion_rejects_failed_or_unmeasured_supported_trial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            failed = run_record()
            failed.update(
                run_status="runner_error",
                failure_reason={"code": "test", "message": "test"},
            )
            failed["paper_metrics"] = paper_metrics(failed)
            (root / "schedule.jsonl").write_text(
                json.dumps({
                    "run_key": failed["run_key"],
                    "tool": failed["tool"],
                    "target_id": failed["target_id"],
                    "trial_id": failed["trial_id"],
                }) + "\n",
                encoding="utf-8",
            )
            (root / "raw-runs.jsonl").write_text(json.dumps(failed) + "\n", encoding="utf-8")
            (root / "support-matrix.csv").write_text(
                "tool,target_id,support_status\nsemantist_full,target,supported\n",
                encoding="utf-8",
            )
            self.assertEqual(validate_complete(root), [])
            errors = validate_complete(root, require_valid_runs=True)
            self.assertTrue(any("did not complete" in error for error in errors))
            self.assertTrue(any("valid_run" in error for error in errors))

            completed = run_record()
            completed["total_executions"] = None
            completed["paper_metrics"] = paper_metrics(completed)
            (root / "raw-runs.jsonl").write_text(json.dumps(completed) + "\n", encoding="utf-8")
            errors = validate_complete(root, require_valid_runs=True)
            self.assertTrue(any("total_executions_reported" in error for error in errors))

    def test_single_tool_entry_keeps_analysis_offline(self):
        args = parse_args(runner_arguments(["--tool", "semantist", "--run-id", "new", "--fuzz-time", "12"]))
        self.assertEqual(args.tools, "semantist_full")
        self.assertEqual(args.fuzz_time, 12)
        self.assertFalse(args.serial)
        self.assertTrue(args.skip_aggregate)
        self.assertTrue(args.require_valid_runs)
        self.assertFalse(args.with_replay)
        self.assertFalse(args.with_coverage)
        with self.assertRaises(SystemExit), patch("sys.stderr", new=io.StringIO()):
            runner_arguments(["--tool", "semantist", "--run-id", "../old"])


if __name__ == "__main__":
    unittest.main()
