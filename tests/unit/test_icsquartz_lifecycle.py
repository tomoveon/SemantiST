"""Exercise the real ICSQuartz adapter lifecycle without containers or targets."""

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
from experiments.comparison.adapters.icsquartz import ICSQuartzAdapter  # noqa: E402
from experiments.comparison.lib.metrics import paper_metrics  # noqa: E402


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
            online_budget_seconds=20, observer_poll_interval_ms=10,
            icsquartz_root=None,
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
        self.set_scan_cycle(False)
        self.prep = SimpleNamespace(preprocess_dir=self.prep_dir)
        self.elapsed = 0.0
        self.calls = []
        self.counter = lambda seconds: 1 + int(seconds // 15) * 100
        self.running = lambda seconds: True
        self.client_log = lambda seconds: ""
        self.container_logs = lambda stopped: ("container diagnostic stdout", "container diagnostic stderr")
        self.errors = {}
        self.returncodes = {}
        self.command_stderr = {}
        self.exit_code = 1

    def set_scan_cycle(self, enabled):
        (self.prep_dir / "metadata.json").write_text(
            json.dumps({"fuzzer_image": "icsquartz:test", "fuzzer_image_id": "sha256:built-target", "scan_cycle": enabled}),
            encoding="utf-8",
        )

    def fake_subprocess(self, command, **kwargs):
        self.calls.append((command, kwargs))
        self.assertGreater(kwargs.get("timeout", 0), 0, command)
        operation = command[1]
        stopped = any(previous[0][1] == "stop" for previous in self.calls)
        error_key = "cleanup_logs" if operation == "logs" and stopped else operation
        if error_key in self.errors:
            raise self.errors[error_key]
        stdout, stderr = "", ""
        if operation == "run":
            stdout = "mock-container"
        elif operation == "inspect":
            if command[3] == "{{.State.Running}}":
                count = self.counter(self.elapsed)
                if count is not None:
                    # Exercise actual counter decoding, including the active client.
                    stats = {"executions": 0, "client_stats": [{"enabled": True, "executions": count}]}
                    (self.run_dir / "tool-artifacts" / "fuzzer_stats.json").write_text(
                        json.dumps(stats) + "\n", encoding="utf-8"
                    )
                (self.run_dir / "tool-artifacts" / "fuzzer_log").write_text(
                    self.client_log(self.elapsed), encoding="utf-8"
                )
                stdout = "true" if self.running(self.elapsed) else "false"
            else:
                stdout = str(self.exit_code)
        elif operation == "logs":
            stdout, stderr = self.container_logs(stopped)
        returncode = self.returncodes.get(operation, 0)
        if returncode:
            stderr = "synthetic command failure"
        stderr = self.command_stderr.get(operation, stderr)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    def fake_sleep(self, seconds):
        # Simulate one-second observation steps; never wait in the test process.
        self.elapsed += max(1.0, seconds)

    def run_adapter(self, observer_error=None):
        with ExitStack() as stack:
            stack.enter_context(patch(f"{MODULE}.subprocess.run", side_effect=self.fake_subprocess))
            stack.enter_context(patch(f"{MODULE}.time.monotonic_ns", side_effect=lambda: int((1 + self.elapsed) * 1e9)))
            stack.enter_context(patch(f"{MODULE}.time.sleep", side_effect=self.fake_sleep))
            if observer_error is not None:
                stack.enter_context(patch.object(self.adapter, "scan_finding_events", side_effect=observer_error))
            return self.adapter.run(self.target, self.run_dir, self.prep, 123, 7)

    def assert_cleanup(self):
        operations = [command[1] for command, _ in self.calls]
        self.assertEqual(operations[-3:], ["stop", "logs", "rm"])
        self.assertEqual(operations.count("rm"), 1)
        return json.loads((self.run_dir / "container-cleanup.json").read_text(encoding="utf-8"))

    def test_progress_at_upstream_report_interval_and_recovered_child_panic_are_valid(self):
        self.client_log = lambda seconds: "thread '<unnamed>' panicked at child\n" if seconds >= 2 else ""
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "budget_completed")
        self.assertEqual(result["total_executions"], 101)
        start_command = self.calls[0][0]
        self.assertIn("sha256:built-target", start_command)
        self.assertNotIn("icsquartz:test", start_command)
        self.assertTrue(self.assert_cleanup()["removed"])
        self.assertTrue((self.run_dir / "container.stderr.log").is_file())
        # Our deliberate SIGTERM must not be mistaken for a native early exit.
        self.assertFalse(any("{{.State.ExitCode}}" in command for command, _ in self.calls))

    def test_slow_mutational_stage_is_given_its_timeout_allowance(self):
        self.config.online_budget_seconds = 140
        self.counter = lambda seconds: 1 if seconds < 130 else 2
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "budget_completed")
        command = json.loads((self.run_dir / "command.json").read_text(encoding="utf-8"))
        self.assertEqual(command["execution_progress_timeout_seconds"], 158)
        self.assert_cleanup()

    def test_stagnation_is_rejected_in_both_scan_and_ordinary_suites(self):
        for scan_cycle in (False, True):
            with self.subTest(scan_cycle=scan_cycle):
                self.set_scan_cycle(scan_cycle)
                self.elapsed, self.calls = 0.0, []
                self.config.online_budget_seconds = 200
                self.counter = lambda seconds: 10
                result = self.run_adapter()
                self.assertEqual(result["run_status"], "watchdog_timeout")
                self.assertEqual(result["failure_reason"]["code"], "icsquartz_execution_stalled")
                self.assertEqual(self.elapsed, 158)
                self.assert_cleanup()

    def test_missing_and_zero_counters_get_startup_grace_then_fail(self):
        for count in (None, 0):
            with self.subTest(count=count):
                self.elapsed, self.calls = 0.0, []
                self.config.online_budget_seconds = 60
                self.counter = lambda seconds: count
                result = self.run_adapter()
                self.assertEqual(result["failure_reason"]["code"], "icsquartz_execution_count_missing")
                self.assertEqual(self.elapsed, 40)
                self.assert_cleanup()

    def test_short_budget_still_requires_a_positive_count(self):
        self.config.online_budget_seconds = 5
        self.counter = lambda seconds: None
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "watchdog_timeout")
        self.assertEqual(self.elapsed, 5)
        self.assert_cleanup()

    def test_unrecoverable_respawner_at_budget_is_not_success(self):
        self.client_log = lambda seconds: (
            "Fuzzer-respawner: Storing state in crashed fuzzer instance did not work, no point to spawn the next client!\n"
            if seconds >= 18 else ""
        )
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "online_crashed")
        self.assertEqual(result["failure_reason"]["code"], "icsquartz_client_fatal_log")
        self.assertEqual(self.elapsed, 20)
        self.assert_cleanup()

    def test_unrecoverable_error_only_on_container_stderr_is_not_success(self):
        self.container_logs = lambda stopped: ("", "Failed to run launcher: unrecoverable failure\n")
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "online_crashed")
        self.assertEqual(result["failure_reason"]["code"], "icsquartz_client_fatal_log")
        self.assertTrue(result["failure_reason"]["log_tail_path"].endswith("container.pre-stop.stderr.log"))
        self.assert_cleanup()

    def test_logs_created_by_our_stop_do_not_retroactively_fail_the_run(self):
        self.container_logs = lambda stopped: ("", "Failed to run launcher: stopping\n" if stopped else "")
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "budget_completed")
        self.assert_cleanup()

    def test_log_collection_and_shutdown_executions_are_only_diagnostics(self):
        def logs(stopped):
            self.elapsed += 3
            (self.run_dir / "tool-artifacts" / "fuzzer_stats.json").write_text(
                json.dumps({"executions": 999999, "exec_sec": 50000}) + "\n", encoding="utf-8"
            )
            return "diagnostic stdout", "diagnostic stderr"
        self.container_logs = logs
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "budget_completed")
        self.assertEqual(result["online_elapsed_seconds"], 20)
        self.assertEqual(result["cleanup_inclusive_elapsed_seconds"], 26)
        self.assertEqual(result["total_executions"], 101)
        self.assertAlmostEqual(result["execs_per_sec"], 101 / 20)
        self.assertEqual(result["tool_specific_metrics"]["shutdown_total_executions"], 999999)
        self.assertEqual(result["tool_specific_metrics"]["shutdown_execs_per_sec"], 50000)
        self.assertEqual(result["total_executions_window"], "last_native_report_observed_within_online_budget")
        paper = paper_metrics({**result, "support_status": "supported", "online_budget_seconds": 20})
        self.assertTrue(paper["valid_run"])
        self.assertEqual(paper["total_executions_reported"], 101)
        self.assertAlmostEqual(paper["execs_per_sec"], 101 / 20)
        self.assertEqual(paper["total_executions_window"], result["total_executions_window"])
        self.assert_cleanup()

    def test_first_counter_observed_after_budget_cannot_make_a_valid_run(self):
        def delayed_counter(seconds):
            if seconds >= 20:
                self.elapsed += 2  # A delayed final inspect/read finishes past the deadline.
                return 1000
            return None
        self.counter = delayed_counter
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "watchdog_timeout")
        self.assertIsNone(result["total_executions"])
        self.assertIsNone(result["execs_per_sec"])
        self.assertEqual(result["tool_specific_metrics"]["shutdown_total_executions"], 1000)
        paper = paper_metrics({**result, "support_status": "supported", "online_budget_seconds": 20})
        self.assertFalse(paper["valid_run"])
        self.assertIsNone(paper["total_executions_reported"])
        self.assert_cleanup()

    def test_container_exit_at_budget_boundary_is_not_our_termination(self):
        self.running = lambda seconds: seconds < 20
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "online_crashed")
        self.assert_cleanup()

    def test_clean_early_exit_is_not_a_completed_fuzz_loop(self):
        self.running = lambda seconds: seconds < 2
        self.exit_code = 0
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "online_crashed")
        self.assert_cleanup()

    def test_observer_error_and_interrupt_preserve_original_exception_and_cleanup(self):
        for error in (OSError("observer error"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                self.calls = []
                with self.assertRaises(type(error)) as raised:
                    self.run_adapter(observer_error=error)
                self.assertIs(raised.exception, error)
                self.assertTrue(self.assert_cleanup()["removed"])

    def test_start_and_inspect_timeouts_also_cleanup(self):
        for operation in ("run", "inspect"):
            with self.subTest(operation=operation):
                self.calls = []
                error = subprocess.TimeoutExpired(["docker", operation], 10)
                self.errors = {operation: error}
                with self.assertRaises(subprocess.TimeoutExpired) as raised:
                    self.run_adapter()
                self.assertIs(raised.exception, error)
                self.assertTrue(self.assert_cleanup()["removed"])

    def test_stop_and_log_failures_do_not_prevent_force_removal(self):
        self.errors = {operation: subprocess.TimeoutExpired(["docker", operation], 10) for operation in ("stop", "cleanup_logs")}
        result = self.run_adapter()
        cleanup = self.assert_cleanup()
        self.assertTrue(cleanup["removed"])
        self.assertIn("stop_error", cleanup)
        self.assertIn("logs_error", cleanup)
        self.assertEqual(result["run_status"], "budget_completed")

    def test_removal_nonzero_or_timeout_cannot_be_valid(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                self.elapsed, self.calls = 0.0, []
                self.errors = {"rm": subprocess.TimeoutExpired(["docker", "rm"], 10)} if timeout else {}
                self.returncodes = {} if timeout else {"rm": 1}
                result = self.run_adapter()
                self.assertEqual(result["run_status"], "runner_error")
                self.assertEqual(result["failure_reason"]["code"], "icsquartz_container_cleanup_failed")
                self.assertFalse(self.assert_cleanup()["removed"])

    def test_cleanup_failure_does_not_replace_original_observer_error(self):
        original = OSError("original observer failure")
        self.errors = {"rm": subprocess.TimeoutExpired(["docker", "rm"], 10)}
        with self.assertRaises(OSError) as raised:
            self.run_adapter(observer_error=original)
        self.assertIs(raised.exception, original)
        self.assertFalse(self.assert_cleanup()["removed"])

    def test_already_removed_container_is_confirmed_cleanup(self):
        self.returncodes = {"rm": 1}
        self.command_stderr = {"rm": "Error response from daemon: No such container: mock"}
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "budget_completed")
        self.assertTrue(self.assert_cleanup()["removed"])

    def test_failed_start_is_cleaned_and_remains_failed(self):
        self.returncodes = {"run": 125}
        result = self.run_adapter()
        self.assertEqual(result["run_status"], "online_start_failed")
        self.assert_cleanup()

    def test_existing_preprocessing_is_preserved_and_rejected(self):
        directory = self.adapter.preprocessing_dir(self.target)
        directory.mkdir(parents=True)
        sentinel = directory / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaises(AdapterError):
            self.adapter.prepare(self.target)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_support_and_preprocessing_do_not_require_external_checkout(self):
        self.assertEqual(self.adapter.detect_support(self.target).status, "supported")
        with patch(f"{MODULE}.run_capture", return_value={"returncode": 0, "end_monotonic_ns": 1}) as capture, \
             patch(f"{MODULE}.container_image_id", return_value="sha256:mock"):
            result = self.adapter.prepare(self.target)
        self.assertEqual(result.status, "completed")
        self.assertEqual(capture.call_count, 3)
        self.assertTrue(all(call.kwargs["cwd"] == ROOT for call in capture.call_args_list))

    def test_preprocessing_image_id_failure_is_not_success(self):
        with patch(f"{MODULE}.run_capture", return_value={"returncode": 0, "end_monotonic_ns": 1}), \
             patch(f"{MODULE}.container_image_id", return_value=None):
            result = self.adapter.prepare(self.target)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure_reason["code"], "icsquartz_target_image_id_missing")

    def test_missing_recorded_image_id_does_not_fall_back_to_mutable_tag(self):
        (self.prep_dir / "metadata.json").write_text(json.dumps({"fuzzer_image": "icsquartz:test"}), encoding="utf-8")
        with self.assertRaises(AdapterError):
            self.run_adapter()
        self.assertEqual(self.calls, [])

    def test_long_target_names_do_not_hide_experiment_identity_in_container_names(self):
        self.target.target_id = "target_" + "long_name_" * 12
        names = []
        for experiment_id in ("campaign_a", "campaign_b"):
            self.config.experiment_id = experiment_id
            self.elapsed, self.calls = 0.0, []
            self.run_adapter()
            command = self.calls[0][0]
            names.append(command[command.index("--name") + 1])
        self.assertNotEqual(*names)


if __name__ == "__main__":
    unittest.main()
