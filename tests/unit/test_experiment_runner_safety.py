"""Focused runner regressions; synthetic adapters and no container execution."""

import csv
import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from experiments.comparison import run_full_comparison as runner
from experiments.comparison import run_tool
from experiments.comparison.adapters.base import BaseAdapter, ExperimentConfig, PreprocessingResult, SupportResult


def target_fixture(root):
    source = root / "target.st"
    source.write_text("FUNCTION TEST : BOOL\nTEST := TRUE;\nEND_FUNCTION\n", encoding="utf-8")
    return SimpleNamespace(
        target_id="synthetic", suite="suite", kind="FUNCTION", function="TEST",
        st_file=source, st_file_sha256="synthetic", benchmark_dir=root,
        fixture_hashes={"target.st": "synthetic"}, compatibility_manifest=None,
        compatibility_manifest_sha256=None, ground_truth_status="unknown_ground_truth",
        raw={key: "synthetic" for key in ("upstream_repo", "upstream_commit", "upstream_path", "upstream_sha256")},
    )


def config_fixture(root):
    return ExperimentConfig(
        experiment_id="batch", semantist_root=ROOT, workspace_root=ROOT.parent,
        artifact_root=root, icsquartz_root=None, online_budget_seconds=20,
        per_execution_timeout_ms=100, semantic_task_budget=1000,
        semantist_fb_max_cycles=256, semantist_fb_stale_threshold=0,
        semantist_fb_state_trace=False, observer_poll_interval_ms=10,
        cpuset="0,1", container_runtime=None, container_runtime_status="synthetic only",
        container_platform=None, semantist_image="unused",
    )


class FailingBatchAdapter(BaseAdapter):
    tool_name = "semantist_full"

    def __init__(self, config, first_batch, *, local_failure=False):
        super().__init__(config)
        self.first_batch = first_batch
        self.local_failure = local_failure
        self.started = []
        self.barrier = threading.Barrier(2, timeout=5)

    def run(self, target, run_dir, preprocessing, rng_seed, cpu):
        trial = int(run_dir.name.rsplit("_", 1)[-1])
        self.started.append(trial)
        if trial in self.first_batch:
            self.barrier.wait()
        if trial == self.first_batch[0]:
            if self.local_failure:
                return {
                    "run_status": "runner_error", "online_elapsed_seconds": 20.0,
                    "total_executions": 100, "candidate_findings": [],
                    "candidate_collection_complete": False,
                    "failure_reason": {"code": "synthetic_collection_failure", "message": "Unreadable log after confirmed cleanup"},
                    "tool_specific_metrics": {"cleanup_confirmed": True, "infrastructure_failure": False},
                }
            raise RuntimeError("synthetic adapter failure")
        return {"run_status": "budget_completed", "online_elapsed_seconds": 20.0,
                "total_executions": 100, "candidate_findings": [],
                "candidate_collection_complete": True,
                "tool_specific_metrics": {"cleanup_confirmed": True, "infrastructure_failure": False}}


class ExperimentRunnerSafetyTests(unittest.TestCase):
    def test_removed_resume_reuse_overwrite_options_are_rejected_by_both_entries(self):
        for option in ("--resume", "--reuse-preprocessing", "--overwrite"):
            for entry in ("matrix", "single"):
                with self.subTest(option=option, entry=entry), redirect_stderr(io.StringIO()), \
                        patch.object(runner, "resolve_container_runtime") as probe:
                    with self.assertRaises(SystemExit) as error:
                        if entry == "matrix":
                            runner.main([option])
                        else:
                            run_tool.main(["--tool", "semantist", "--run-id", "fresh", option])
                    self.assertEqual(error.exception.code, 2)
                    probe.assert_not_called()

    def test_invalid_ids_are_rejected_before_runtime_probe(self):
        for identifier in ("", ".", "..", "../old", "a\\b", "a:b", "a|b", " spaced "):
            for entry in ("matrix", "single"):
                with self.subTest(identifier=identifier, entry=entry), redirect_stderr(io.StringIO()), \
                        patch.object(runner, "resolve_container_runtime") as probe:
                    with self.assertRaises(SystemExit):
                        if entry == "matrix":
                            runner.main(["--experiment-id", identifier])
                        else:
                            run_tool.main(["--tool", "semantist", "--run-id", identifier])
                    probe.assert_not_called()

    def test_invalid_numeric_options_are_rejected_by_argument_parser(self):
        invalid = [
            ("--trials", "0"), ("--fuzz-time", "-1"), ("--timeout-ms", "0"),
            ("--semantic-task-budget", "0"), ("--semantist-fb-max-cycles", "0"),
            ("--semantist-fb-max-cycles", "10001"), ("--semantist-fb-stale-threshold", "-1"),
            ("--analysis-timeout", "0"), ("--analysis-timeout", "nan"),
            ("--analysis-timeout", "inf"),
        ]
        for option, value in invalid:
            with self.subTest(option=option, value=value), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    runner.parse_args([option, value])
                self.assertEqual(error.exception.code, 2)

    def test_obsolete_poll_option_is_retained_but_does_not_enable_online_collection(self):
        for value in ("0", "10", "11", "1000"):
            with self.subTest(value=value):
                args = runner.parse_args(["--observer-poll-interval-ms", value])
                self.assertEqual(args.observer_poll_interval_ms_requested, int(value))
                self.assertEqual(args.observer_poll_interval_ms, 0)

    def test_invalid_target_limit_is_rejected_without_creating_experiment(self):
        for value in ("0", "-1"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = SimpleNamespace(targets=[target_fixture(root)])
                with patch.object(runner, "load_manifest", return_value=manifest), \
                        patch.object(runner, "resolve_container_runtime", return_value=("docker", "mocked")), \
                        patch.object(runner, "unavailable_cpus", return_value=[]), \
                        patch.object(runner, "find_icsquartz_root", return_value=None), \
                        patch.object(runner, "write_environment") as write_environment, \
                        patch.object(runner, "prepare_supported_targets") as prepare:
                    with self.assertRaisesRegex((ValueError, SystemExit), "limit-targets"):
                        runner.main(["--artifact-root", str(root), "--experiment-id", "invalid-limit",
                                     "--tools", "semantist_full", "--cpus", "0", "--preflight-only",
                                     "--limit-targets", value])
                    write_environment.assert_not_called()
                    prepare.assert_not_called()
                self.assertFalse((root / "invalid-limit").exists())

    def test_defaults_keep_analysis_offline_strict_and_cpu_parallel(self):
        for args in (
            runner.parse_args([]),
            runner.parse_args(run_tool.runner_arguments(["--tool", "semantist", "--run-id", "fresh"])),
        ):
            self.assertEqual(args.semantist_fb_max_cycles, 256)
            self.assertFalse(args.semantist_fb_state_trace)
            self.assertFalse(args.with_replay)
            self.assertFalse(args.with_coverage)
            self.assertTrue(args.skip_aggregate)
            self.assertTrue(args.require_valid_runs)
            self.assertFalse(args.serial)
            self.assertEqual(args.observer_poll_interval_ms, 0)
        self.assertTrue(runner.parse_args(run_tool.runner_arguments(
            ["--tool", "semantist", "--run-id", "fresh", "--serial"])).serial)

    def test_existing_directory_is_untouched_and_rejected_before_runtime_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "existing"
            (existing / "nested").mkdir(parents=True)
            (existing / "experiment-manifest.json").write_bytes(b"preserve manifest\x00")
            (existing / "nested" / "run-result.json").write_bytes(b"preserve result\x00")
            before = {str(path.relative_to(existing)): path.read_bytes()
                      for path in existing.rglob("*") if path.is_file()}
            with patch.object(runner, "resolve_container_runtime") as probe, \
                    patch.object(runner, "load_manifest") as load:
                with self.assertRaisesRegex(SystemExit, "already exists"):
                    runner.main(["--artifact-root", str(root), "--experiment-id", "existing"])
                probe.assert_not_called()
                load.assert_not_called()
            after = {str(path.relative_to(existing)): path.read_bytes()
                     for path in existing.rglob("*") if path.is_file()}
            self.assertEqual(after, before)

    def test_unsupported_preflight_retains_diagnostics_and_never_prepares(self):
        for selected in ("icsquartz_full", "semantist_full,icsquartz_full"):
            with self.subTest(tools=selected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = target_fixture(root)
                manifest = SimpleNamespace(targets=[target], path=root / "source-manifest.json",
                                           sha256="synthetic", raw={"synthetic": True})
                with patch.object(runner, "load_manifest", return_value=manifest), \
                        patch.object(runner, "resolve_container_runtime", return_value=("docker", "mocked")), \
                        patch.object(runner, "unavailable_cpus", return_value=[]), \
                        patch.object(runner, "find_icsquartz_root", return_value=None), \
                        patch.object(runner.ADAPTERS["icsquartz_full"], "detect_support", return_value=
                                     SupportResult.unsupported("synthetic_unsupported", "synthetic unsupported target")), \
                        patch.object(runner, "write_environment"), \
                        patch.object(runner, "validate_execution_environment") as images, \
                        patch.object(runner, "prepare_supported_targets") as prepare, \
                        patch.object(runner, "run_online_matrix") as online, redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(SystemExit, "no supported targets for icsquartz_full"):
                        runner.main(["--artifact-root", str(root), "--experiment-id", "preflight",
                                     "--tools", selected, "--cpus", "0", "--trials", "1", "--preflight-only"])
                    images.assert_not_called()
                    prepare.assert_not_called()
                    online.assert_not_called()
                directory = root / "preflight"
                for filename in ("manifest.snapshot.json", "experiment-manifest.json", "schedule.jsonl", "support-matrix.csv"):
                    self.assertTrue((directory / filename).is_file(), filename)
                with (directory / "support-matrix.csv").open(encoding="utf-8", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                unsupported = next(row for row in rows if row["tool"] == "icsquartz_full")
                self.assertEqual(unsupported["support_status"], "unsupported")
                self.assertEqual(unsupported["support_reason"], "synthetic unsupported target")

    def test_unhandled_error_without_cleanup_evidence_stops_later_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = target_fixture(root)
            config = config_fixture(root)
            directory = root / config.experiment_id
            directory.mkdir()
            schedule = runner.write_schedule(directory, config.experiment_id, ["semantist_full"], [target], 3)
            first_batch = [row["trial_id"] for row in schedule[:2]]
            adapter = FailingBatchAdapter(config, first_batch)
            pair = (adapter.tool_name, target.target_id)
            preprocessing = PreprocessingResult("completed", root / "prep", 1, 2)
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "remaining batches were not started"):
                    runner.run_online_matrix(directory, schedule, [target], {adapter.tool_name: adapter},
                                             {pair: SupportResult.supported()}, {pair: preprocessing}, [0, 1])
            self.assertCountEqual(adapter.started, first_batch)
            results = [json.loads((adapter.run_dir(target, trial) / "run-result.json").read_text(encoding="utf-8"))
                       for trial in first_batch]
            self.assertEqual([run["run_status"] for run in results], ["runner_error", "budget_completed"])
            self.assertEqual(results[0]["failure_reason"]["code"], "adapter_unhandled_exception")
            self.assertTrue(results[0]["tool_specific_metrics"]["infrastructure_failure"])
            self.assertFalse(results[0]["tool_specific_metrics"]["cleanup_confirmed"])
            self.assertFalse(results[0]["paper_metrics"]["valid_run"])
            self.assertTrue(results[1]["paper_metrics"]["valid_run"])
            self.assertFalse(adapter.run_dir(target, schedule[2]["trial_id"]).exists())

    def test_local_collection_error_with_confirmed_cleanup_allows_later_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = target_fixture(root)
            config = config_fixture(root)
            directory = root / config.experiment_id
            directory.mkdir()
            schedule = runner.write_schedule(directory, config.experiment_id, ["semantist_full"], [target], 3)
            first_batch = [row["trial_id"] for row in schedule[:2]]
            adapter = FailingBatchAdapter(config, first_batch, local_failure=True)
            pair = (adapter.tool_name, target.target_id)
            preprocessing = PreprocessingResult("completed", root / "prep", 1, 2)
            with redirect_stdout(io.StringIO()):
                runner.run_online_matrix(directory, schedule, [target], {adapter.tool_name: adapter},
                                         {pair: SupportResult.supported()}, {pair: preprocessing}, [0, 1])
            self.assertCountEqual(adapter.started, [row["trial_id"] for row in schedule])
            failed = json.loads((adapter.run_dir(target, first_batch[0]) / "run-result.json").read_text(encoding="utf-8"))
            self.assertEqual(failed["run_status"], "runner_error")
            self.assertEqual(failed["failure_reason"]["code"], "synthetic_collection_failure")
            self.assertEqual(failed["online_elapsed_seconds"], 20)
            self.assertEqual(failed["total_executions"], 100)
            self.assertTrue(failed["tool_specific_metrics"]["cleanup_confirmed"])
            self.assertFalse(failed["tool_specific_metrics"]["infrastructure_failure"])
            self.assertFalse(failed["paper_metrics"]["valid_run"])
            last = json.loads((adapter.run_dir(target, schedule[2]["trial_id"]) / "run-result.json").read_text(encoding="utf-8"))
            self.assertEqual(last["run_status"], "budget_completed")
            self.assertTrue(last["paper_metrics"]["valid_run"])

    def test_single_tool_failure_does_not_load_or_export_metrics(self):
        with patch.object(runner, "main", return_value=1), \
                patch.object(runner, "load_runs") as load, \
                patch.object(runner, "export_metrics") as export:
            self.assertEqual(run_tool.main(["--tool", "semantist", "--run-id", "fresh"]), 1)
            load.assert_not_called()
            export.assert_not_called()


if __name__ == "__main__":
    unittest.main()
