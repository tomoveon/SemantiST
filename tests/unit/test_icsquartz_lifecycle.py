"""ICSQuartz lifecycle contract with an inactive runner during the budget."""

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from experiments.comparison.adapters.base import AdapterError  # noqa: E402
from experiments.comparison.adapters.icsquartz import (  # noqa: E402
    ICSQuartzAdapter, _fuzzer_log_failure, _last_json_line, _native_time_to_find_seconds,
)

MODULE = "experiments.comparison.adapters.icsquartz"


class ICSQuartzLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = SimpleNamespace(
            artifact_root=self.root, experiment_id="campaign", semantist_root=ROOT,
            container_runtime="docker", container_runtime_status="mock", container_platform=None,
            semantist_image="semantist:test", per_execution_timeout_ms=1000,
            online_budget_seconds=20, observer_poll_interval_ms=10, icsquartz_root=None,
        )
        fixtures = self.root / "fixtures"
        fixtures.mkdir()
        for name in ("target.st", "program.st", "harness.c"):
            (fixtures / name).write_text("fixture", encoding="utf-8")
        self.target = SimpleNamespace(
            target_id="demo", suite="icsquartz_icsfuzz", kind="FUNCTION_BLOCK",
            function="FB", st_file=fixtures / "target.st", compatibility_manifest=None,
            fixture=lambda name: fixtures / name,
        )
        self.adapter = ICSQuartzAdapter(self.config)
        self.run_dir = self.adapter.run_dir(self.target, 1)
        self.run_dir.mkdir(parents=True)
        self.prep_dir = self.root / "prepared"
        self.prep_dir.mkdir()
        (self.prep_dir / "metadata.json").write_text(json.dumps({
            "fuzzer_image": "icsquartz:test", "fuzzer_image_id": "sha256:built-target", "scan_cycle": False,
        }), encoding="utf-8")
        self.prep = SimpleNamespace(preprocess_dir=self.prep_dir)
        self.elapsed = 0.0
        self.calls = []
        self.waits = []
        self.errors = {}
        self.returncodes = {}
        self.count = 466
        self.native_log = ""
        self.shutdown_native_log = ""
        self.native_container_logs = ("native stdout", "native stderr")
        self.shutdown_container_logs = ("shutdown stdout", "shutdown stderr")
        self.state = {"Running": False, "ExitCode": 0, "FinishedAt": "2100-01-01T00:00:00Z"}
        self.wait_error = None

    def stopped(self):
        return any(command[1] == "stop" for command, _ in self.calls)

    def fake_wait(self, seconds):
        # Native output arrives while the controller is asleep. It supplies no
        # host first-discovery timestamp, because the runner never observed it.
        self.assertFalse(self.stopped())
        self.assertEqual([command[1] for command, _ in self.calls], ["run"])
        self.waits.append(seconds)
        if self.wait_error is not None:
            raise self.wait_error
        self.elapsed += seconds
        artifacts = self.run_dir / "tool-artifacts"
        if self.count is not None:
            (artifacts / "fuzzer_stats.json").write_text(
                json.dumps({"executions": self.count, "exec_sec": 23.3}) + "\n", encoding="utf-8",
            )
        (artifacts / "fuzzer_log").write_text(self.native_log, encoding="utf-8")

    def fake_subprocess(self, command, **kwargs):
        self.calls.append((command, kwargs))
        self.assertGreater(kwargs.get("timeout", 0), 0, command)
        operation = command[1]
        error_key = ("online_logs" if "--until" in command else "cleanup_logs") if operation == "logs" else operation
        stdout, stderr = "", ""
        if operation == "run":
            stdout = "mock-container"
        elif operation == "stop":
            if self.shutdown_native_log:
                with (self.run_dir / "tool-artifacts" / "fuzzer_log").open("a", encoding="utf-8") as handle:
                    handle.write(self.shutdown_native_log)
        elif operation == "inspect":
            self.assertTrue(self.stopped(), "state inspection must happen after stopping")
            self.assertIn("{{json .State}}", command)
            stdout = json.dumps(self.state)
        elif operation == "logs":
            self.assertTrue(self.stopped(), "log collection must happen after stopping")
            stdout, stderr = self.native_container_logs if "--until" in command else self.shutdown_container_logs
            kwargs["stdout"].write(stdout.encode())
            kwargs["stderr"].write(stderr.encode())
            kwargs["stdout"].flush()
            kwargs["stderr"].flush()
        if error_key in self.errors:
            raise self.errors[error_key]
        returncode = self.returncodes.get(operation, 0)
        if returncode:
            stderr = "synthetic command failure"
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    def run_adapter(self, collection_error=None):
        original_scan = self.adapter.scan_finding_events

        def post_run_scan(*args, **kwargs):
            self.assertTrue(self.stopped(), "findings must be collected after stopping")
            if collection_error is not None:
                raise collection_error
            return original_scan(*args, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(patch(f"{MODULE}.subprocess.run", side_effect=self.fake_subprocess))
            stack.enter_context(patch(f"{MODULE}.time.monotonic_ns", side_effect=lambda: int((1 + self.elapsed) * 1e9)))
            stack.enter_context(patch(f"{MODULE}.threading.Event", return_value=SimpleNamespace(wait=self.fake_wait)))
            stack.enter_context(patch.object(self.adapter, "scan_finding_events", side_effect=post_run_scan))
            return self.adapter.run(self.target, self.run_dir, self.prep, 123, 7)

    def assert_cleanup(self):
        operations = [command[1] for command, _ in self.calls]
        self.assertEqual(operations[-1], "rm")
        self.assertEqual(operations.count("stop"), 1)
        self.assertEqual(operations.count("rm"), 1)
        for operation in ("inspect", "logs"):
            if operation in operations:
                self.assertLess(operations.index("stop"), operations.index(operation))
        return json.loads((self.run_dir / "container-cleanup.json").read_text(encoding="utf-8"))

    def test_sleeps_once_then_stops_before_inspection_and_collection(self):
        result = self.run_adapter()
        self.assertEqual(self.waits, [20])
        self.assertEqual(result["run_status"], "budget_completed")
        self.assertEqual(result["online_elapsed_seconds"], 20)
        self.assertEqual(result["total_executions"], 466)
        self.assertEqual(result["collection_mode"], "post_run")
        self.assertTrue(result["candidate_collection_complete"])
        self.assertEqual(result["total_executions_window"], "native_stop_snapshot_may_include_shutdown_tail")
        self.assertEqual(sum(command[1] == "inspect" for command, _ in self.calls), 1)
        self.assertTrue(result["tool_specific_metrics"]["cleanup_confirmed"])
        self.assertFalse(result["tool_specific_metrics"]["infrastructure_failure"])
        self.assertTrue(self.assert_cleanup()["removed"])

    def test_long_budget_does_not_apply_old_stagnation_watchdog(self):
        self.config.online_budget_seconds = 300
        result = self.run_adapter()
        self.assertEqual(self.waits, [300])
        self.assertEqual(self.elapsed, 300)
        self.assertEqual(result["run_status"], "budget_completed")
        command = json.loads((self.run_dir / "command.json").read_text(encoding="utf-8"))
        self.assertNotIn("execution_progress_timeout_seconds", command)

    def test_missing_count_is_invalid_only_after_full_budget(self):
        self.count = None
        result = self.run_adapter()
        self.assertEqual(self.elapsed, 20)
        self.assertEqual(result["run_status"], "runner_error")
        self.assertEqual(result["failure_reason"]["code"], "native_execution_count_missing")
        self.assertIsNone(result["total_executions"])
        self.assertFalse(result["tool_specific_metrics"]["infrastructure_failure"])
        self.assert_cleanup()

    def test_child_panic_does_not_imply_unrecoverable_failure(self):
        self.native_log = "thread '<unnamed>' panicked at child\n"
        self.assertEqual(self.run_adapter()["run_status"], "budget_completed")

    def test_native_respawner_failure_is_not_valid_completion(self):
        self.native_log = "Fuzzer-respawner: Storing state in crashed fuzzer instance did not work\n"
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "online_crashed")
        self.assertEqual(result["failure_reason"]["code"], "icsquartz_client_fatal_log")
        self.assertEqual(self.elapsed, 20)
        self.assert_cleanup()

    def test_fatal_stderr_is_collected_after_stop_with_time_filter(self):
        self.native_container_logs = ("", "Failed to run launcher: native failure\n")
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "online_crashed")
        self.assertTrue(result["failure_reason"]["log_tail_path"].endswith("container.pre-stop.stderr.log"))

    def test_shutdown_errors_do_not_retroactively_fail_run(self):
        self.shutdown_container_logs = ("", "Failed to run launcher: shutdown\n")
        self.shutdown_native_log = "Failed to run launcher: shutdown\n"
        self.assertEqual(self.run_adapter()["run_status"], "budget_completed")

    def test_early_exit_is_identified_from_post_stop_state(self):
        self.state["FinishedAt"] = "2000-01-01T00:00:00Z"
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "online_crashed")
        self.assertEqual(result["failure_reason"]["code"], "icsquartz_exited_before_budget")
        self.assert_cleanup()

    def test_inspection_failure_is_local_invalid_trial_after_cleanup(self):
        self.errors = {"inspect": subprocess.TimeoutExpired(["docker", "inspect"], 10)}
        result = self.run_adapter()
        self.assertEqual(result["failure_reason"]["code"], "icsquartz_exit_state_unavailable")
        self.assertFalse(result["tool_specific_metrics"]["infrastructure_failure"])
        self.assertTrue(self.assert_cleanup()["removed"])

    def test_log_timeout_preserves_partial_output_time_and_count(self):
        self.errors = {"online_logs": subprocess.TimeoutExpired(["docker", "logs"], 10)}
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "runner_error")
        self.assertEqual(result["failure_reason"]["code"], "icsquartz_online_logs_incomplete")
        self.assertEqual(result["online_elapsed_seconds"], 20)
        self.assertEqual(result["total_executions"], 466)
        self.assertFalse(result["tool_specific_metrics"]["infrastructure_failure"])
        self.assertEqual((self.run_dir / "container.pre-stop.stdout.log").read_text(), "native stdout")
        self.assertTrue(self.assert_cleanup()["removed"])

    def test_optional_shutdown_log_failure_does_not_drop_evidence(self):
        self.errors = {"cleanup_logs": subprocess.TimeoutExpired(["docker", "logs"], 10)}
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "budget_completed")
        self.assertIn("logs_error", self.assert_cleanup())

    def test_stop_and_kill_failures_force_remove_before_collection(self):
        self.returncodes = {"stop": 1, "kill": 1}
        result = self.run_adapter()
        operations = [command[1] for command, _ in self.calls]
        self.assertEqual(operations[-3:], ["stop", "kill", "rm"])
        self.assertNotIn("logs", operations)
        self.assertTrue(result["tool_specific_metrics"]["infrastructure_failure"])
        self.assertTrue(self.assert_cleanup()["removed"])

    def test_removal_failure_is_infrastructure_failure(self):
        self.errors = {"rm": subprocess.TimeoutExpired(["docker", "rm"], 10)}
        result = self.run_adapter()
        self.assertEqual(result["failure_reason"]["code"], "icsquartz_container_cleanup_failed")
        self.assertTrue(result["tool_specific_metrics"]["infrastructure_failure"])
        self.assertFalse(result["tool_specific_metrics"]["cleanup_confirmed"])

    def test_collection_error_does_not_fabricate_empty_success(self):
        result = self.run_adapter(collection_error=RuntimeError("metadata unavailable"))
        self.assertEqual(result["run_status"], "runner_error")
        self.assertFalse(result["candidate_collection_complete"])
        self.assertEqual(result["total_executions"], 466)
        self.assertFalse(result["tool_specific_metrics"]["infrastructure_failure"])

    def test_interrupt_during_wait_still_cleans_up(self):
        self.wait_error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_adapter()
        self.assertTrue(self.assert_cleanup()["removed"])

    def test_start_timeout_is_cleaned_and_recorded(self):
        self.errors = {"run": subprocess.TimeoutExpired(["docker", "run"], 10)}
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "runner_error")
        self.assertEqual(self.waits, [])
        self.assertTrue(self.assert_cleanup()["removed"])

    def test_failed_start_is_not_waited_or_reclassified_as_success(self):
        self.returncodes = {"run": 125}
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "online_start_failed")
        self.assertEqual(self.waits, [])
        self.assert_cleanup()

    def test_native_timestamp_is_separate_from_unknown_host_time(self):
        crash_dir = self.run_dir / "tool-artifacts" / "crashes"
        crash_dir.mkdir(parents=True)
        (crash_dir / "input").write_bytes(b"candidate")
        (crash_dir / ".input.metadata").write_text(json.dumps({
            "executions": 7,
            "metadata": {"map": {"native-type": {"time_to_find": {"secs": 4, "nanos": 500000000}}}},
        }), encoding="utf-8")
        event = self.run_adapter()["candidate_findings"][0]
        self.assertIsNone(event["observed_elapsed_seconds"])
        self.assertIsNone(event["observed_monotonic_ns"])
        self.assertEqual(event["native_time_to_find_seconds"], 4.5)
        self.assertEqual(event["native_time_alignment"], "not_aligned_to_runner_launch")
        self.assertEqual(event["time_source"], "unmeasured_post_run")

    def test_missing_or_ambiguous_native_duration_is_unknown(self):
        self.assertIsNone(_native_time_to_find_seconds({}))
        self.assertIsNone(_native_time_to_find_seconds({"time_to_find": {"secs": 1, "nanos": 1_000_000_000}}))
        self.assertIsNone(_native_time_to_find_seconds({"values": [
            {"time_to_find": {"secs": 1, "nanos": 0}},
            {"time_to_find": {"secs": 2, "nanos": 0}},
        ]}))

    def test_stats_reader_uses_tail_and_ignores_partial_record(self):
        path = self.root / "long-stats.json"
        path.write_bytes(b"old record\n" * 100000 + b'{"executions": 77}\n{"executions":')
        with patch.object(Path, "read_text", side_effect=AssertionError("full stats read forbidden")):
            self.assertEqual(_last_json_line(path), {"executions": 77})

    def test_native_log_prefix_excludes_shutdown_fatal_message(self):
        path = self.root / "native.log"
        prefix = b"online output\n"
        path.write_bytes(prefix + b"Failed to run launcher: induced by stop\n")
        self.assertIsNone(_fuzzer_log_failure(path, unrecoverable_only=True, max_bytes=len(prefix)))
        self.assertIsNotNone(_fuzzer_log_failure(path, unrecoverable_only=True))

    def test_existing_preprocessing_is_preserved_and_rejected(self):
        directory = self.adapter.preprocessing_dir(self.target)
        directory.mkdir(parents=True)
        sentinel = directory / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaises(AdapterError):
            self.adapter.prepare(self.target)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_preprocessing_does_not_require_external_checkout(self):
        self.assertEqual(self.adapter.detect_support(self.target).status, "supported")
        with patch(f"{MODULE}.run_capture", return_value={"returncode": 0, "end_monotonic_ns": 1}) as capture, \
             patch(f"{MODULE}.container_image_id", return_value="sha256:mock"):
            result = self.adapter.prepare(self.target)
        self.assertEqual(result.status, "completed")
        self.assertEqual(capture.call_count, 3)

    def test_preprocessing_requires_recorded_immutable_image_id(self):
        (self.prep_dir / "metadata.json").write_text(json.dumps({"fuzzer_image": "icsquartz:test"}), encoding="utf-8")
        with self.assertRaises(AdapterError):
            self.run_adapter()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
