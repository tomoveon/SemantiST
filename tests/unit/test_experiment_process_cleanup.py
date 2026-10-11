"""The container daemon can outlive its CLI process or a host observer."""

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from experiments.comparison.lib import process as process_helpers


class ExperimentProcessCleanupTests(unittest.TestCase):
    def run_finished_cli(self, root: Path, scan_findings):
        cli = Mock()
        cli.poll.return_value = 0
        cli.returncode = 0
        with patch.object(process_helpers.subprocess, "Popen", return_value=cli), \
                patch.object(process_helpers.shutil, "which", return_value="docker"), \
                patch.object(process_helpers.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout="", stderr=""
                )) as daemon:
            try:
                result = process_helpers.run_with_budget(
                    ["docker", "run", "--rm", "--name", "owned-trial", "test-image"],
                    cwd=root,
                    env=None,
                    stdout_path=root / "stdout.log",
                    stderr_path=root / "stderr.log",
                    budget_seconds=1,
                    poll_interval_ms=10,
                    scan_findings=scan_findings,
                )
            finally:
                commands = [call.args[0] for call in daemon.call_args_list]
                self.assertIn(["docker", "stop", "--time", "2", "owned-trial"], commands)
                self.assertIn(["docker", "rm", "-f", "owned-trial"], commands)
                for call in daemon.call_args_list:
                    self.assertEqual(call.kwargs["timeout"], 10)
            return result

    def test_normal_cli_exit_still_cleans_up_the_named_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_finished_cli(Path(tmp), lambda: [])
        self.assertTrue(result["started"])
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["returncode"], 0)
        self.assertTrue(result["container_stop"]["stop_attempted"])

    def test_observer_failure_cleans_up_even_after_cli_has_exited(self):
        def failed_observer():
            raise RuntimeError("observer failed after CLI exit")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "observer failed after CLI exit"):
                self.run_finished_cli(Path(tmp), failed_observer)

    def test_unconfirmed_cleanup_cannot_return_a_successful_trial(self):
        cli = Mock()
        cli.poll.return_value = 0
        cli.returncode = 0
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(process_helpers.subprocess, "Popen", return_value=cli), \
                    patch.object(process_helpers.shutil, "which", return_value="docker"), \
                    patch.object(process_helpers.subprocess, "run", return_value=SimpleNamespace(
                        returncode=1, stdout="", stderr="cannot connect to daemon"
                    )):
                with self.assertRaisesRegex(RuntimeError, "cleanup could not be confirmed"):
                    process_helpers.run_with_budget(
                        ["docker", "run", "--name", "owned-trial", "test-image"],
                        cwd=root, env=None, stdout_path=root / "stdout.log",
                        stderr_path=root / "stderr.log", budget_seconds=1,
                        poll_interval_ms=10, scan_findings=lambda: [],
                    )

    def test_auto_removed_container_counts_as_cleaned_up(self):
        with patch.object(process_helpers.shutil, "which", return_value="docker"), \
                patch.object(process_helpers.subprocess, "run", return_value=SimpleNamespace(
                    returncode=1, stdout="", stderr="Error: No such container: owned-trial"
                )):
            self.assertTrue(process_helpers.remove_container_quiet("docker", "owned-trial"))

    def test_concurrent_auto_removal_is_confirmed_by_inspection(self):
        responses = [
            SimpleNamespace(returncode=1, stdout="", stderr="removal of container is already in progress"),
            SimpleNamespace(returncode=1, stdout="", stderr="Error: No such container: owned-trial"),
        ]
        with patch.object(process_helpers.shutil, "which", return_value="docker"), \
                patch.object(process_helpers.subprocess, "run", side_effect=responses) as daemon:
            self.assertTrue(process_helpers.remove_container_quiet("docker", "owned-trial"))
            self.assertEqual(daemon.call_args.args[0], ["docker", "container", "inspect", "owned-trial"])
            self.assertEqual(daemon.call_args.kwargs["timeout"], 10)

    def test_missing_cli_closes_both_open_log_handles(self):
        original_open = Path.open
        log_handles = []

        def track_open(path, mode="r", *args, **kwargs):
            handle = original_open(path, mode, *args, **kwargs)
            if mode == "ab":
                log_handles.append(handle)
            return handle

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(Path, "open", track_open), \
                    patch.object(process_helpers.subprocess, "Popen", side_effect=FileNotFoundError("missing CLI")):
                result = process_helpers.run_with_budget(
                    ["missing-cli"],
                    cwd=root,
                    env=None,
                    stdout_path=root / "stdout.log",
                    stderr_path=root / "stderr.log",
                    budget_seconds=1,
                    poll_interval_ms=10,
                    scan_findings=lambda: [],
                )
            self.assertFalse(result["started"])
            self.assertEqual(len(log_handles), 2)
            self.assertTrue(all(handle.closed for handle in log_handles))
            self.assertIn("missing CLI", (root / "stderr.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
