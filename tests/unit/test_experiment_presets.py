"""Exercise preset selection and output routing without starting containers."""

import io
import json
import runpy
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch, sentinel

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from experiments.comparison import run_complete_comparison, run_smoke_comparison, run_tool
from experiments.comparison import run_full_comparison as runner


SMOKE_TARGETS = [
    "oscat_basic_charname", "icsfuzz_bf_mcpy_1", "scan_cycle_aircraft_oobw_4",
]


@contextmanager
def mocked_host():
    """Keep real manifests and support checks, replacing only host probes."""
    with ExitStack() as stack:
        stack.enter_context(patch.object(runner, "resolve_container_runtime", return_value=("docker", "mocked")))
        stack.enter_context(patch.object(runner, "unavailable_cpus", return_value=[]))
        stack.enter_context(patch.object(runner, "find_icsquartz_root", return_value=None))
        stack.enter_context(patch.object(runner, "git_state", return_value={"commit": "synthetic"}))
        stack.enter_context(patch.object(runner, "container_runtime_version", return_value="mocked"))
        stack.enter_context(patch.object(runner, "container_image_id", return_value="sha256:synthetic"))
        images = stack.enter_context(patch.object(runner, "validate_execution_environment"))
        stack.enter_context(redirect_stdout(io.StringIO()))
        stack.enter_context(redirect_stderr(io.StringIO()))
        yield images


class ExperimentPresetTests(unittest.TestCase):
    def test_presets_preserve_main_defaults_and_choose_representative_smoke_targets(self):
        expected = {
            "main": (runner.DEFAULT_EXPERIMENT_ID, [], 300, 5, "1-8"),
            "smoke": ("comparison_3tools_20s_smoke", SMOKE_TARGETS, 20, 1, "1-2"),
        }
        for preset, values in expected.items():
            with self.subTest(preset=preset):
                args = runner.parse_args(["--experiment", preset])
                self.assertEqual((args.experiment_id, args.target_id, args.fuzz_time, args.trials, args.cpus), values)
                self.assertEqual(args.tools, runner.DEFAULT_TOOLS)
                self.assertFalse(args.serial)
                self.assertTrue(args.require_valid_runs)
                self.assertTrue(args.skip_aggregate)
        self.assertEqual(vars(runner.parse_args([])), vars(runner.parse_args(["--experiment", "main"])))

    def test_explicit_options_replace_smoke_defaults_including_target_list(self):
        options = ["--run-id", "custom", "--fuzz-time", "45", "--fuzz-trials", "2",
                   "--cpus", "2,4", "--target-id", "first", "--target-id", "second",
                   "--tools", "semantist", "--timeout-ms", "250"]
        for argv in (["--experiment", "smoke", *options], [*options, "--experiment", "smoke"]):
            with self.subTest(argv=argv):
                args = runner.parse_args(argv)
                self.assertEqual(args.experiment, "smoke")
                self.assertEqual(args.experiment_id, "custom")
                self.assertEqual(args.target_id, ["first", "second"])
                self.assertEqual((args.fuzz_time, args.trials, args.cpus), (45, 2, "2,4"))
                self.assertEqual(args.tools, "semantist")
                self.assertEqual(args.timeout_ms, 250)
        self.assertEqual(runner.parse_args(["--experiment", "smoke"]).target_id, SMOKE_TARGETS)

    def test_legacy_names_and_entry_presets_resolve_identically(self):
        current = runner.parse_args(["--run-id", "named", "--fuzz-trials", "3"])
        legacy = runner.parse_args(["--experiment-id", "named", "--trials", "3"])
        self.assertEqual(vars(current), vars(legacy))
        for module, preset in ((run_smoke_comparison, "smoke"), (run_complete_comparison, "main")):
            with self.subTest(preset=preset):
                self.assertEqual(vars(runner.parse_args(module.DEFAULT_ARGS)),
                                 vars(runner.parse_args(["--experiment", preset])))
                overridden = runner.parse_args([*module.DEFAULT_ARGS, "--target-id", "chosen", "--trials", "2"])
                self.assertEqual(overridden.target_id, ["chosen"])
                self.assertEqual(overridden.trials, 2)
        selected = runner.parse_args(run_tool.runner_arguments(
            ["--tool", "semantist", "--run-id", "legacy", "--experiment", "smoke"]))
        self.assertEqual(selected.target_id, SMOKE_TARGETS)
        self.assertEqual(selected.tools, "semantist_full")
        self.assertEqual(selected.experiment_id, "legacy")

    def test_concise_help_hides_advanced_options_without_disabling_them(self):
        outputs = {}
        for flag in ("--help", "--help-all"):
            with self.subTest(flag=flag), redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as error:
                    runner.parse_args([flag])
                self.assertEqual(error.exception.code, 0)
                outputs[flag] = output.getvalue()
        for option in ("--experiment", "--run-id", "--fuzz-trials", "--target-id", "--cpus"):
            self.assertIn(option, outputs["--help"])
        for option in ("--timeout-ms", "--semantist-image", "--with-coverage", "--serial"):
            self.assertNotIn(option, outputs["--help"])
            self.assertIn(option, outputs["--help-all"])
        args = runner.parse_args(["--serial", "--timeout-ms", "250", "--semantist-image", "local:test"])
        self.assertTrue(args.serial)
        self.assertEqual(args.timeout_ms, 250)
        self.assertEqual(args.semantist_image, "local:test")

    def test_root_entry_dispatches_to_shared_runner_and_preserves_exit_status(self):
        with patch.object(runner, "main", return_value=17) as main:
            with self.assertRaises(SystemExit) as error:
                runpy.run_path(str(ROOT / "run_experiment.py"), run_name="__main__")
        self.assertEqual(error.exception.code, 17)
        main.assert_called_once()

    def test_real_manifest_preflight_records_resolved_presets_without_running_or_exporting(self):
        cases = [("smoke", [], 3, 9, "1-2"),
                 ("main", ["--fuzz-time", "20", "--fuzz-trials", "1"], 90, 270, "1-8")]
        for preset, overrides, target_count, row_count, cpus in cases:
            with self.subTest(preset=preset), tempfile.TemporaryDirectory() as tmp, mocked_host() as images, \
                    patch.object(runner, "prepare_supported_targets") as prepare, \
                    patch.object(runner, "run_online_matrix") as online, \
                    patch.object(runner, "load_runs") as load, \
                    patch.object(runner, "export_metrics") as export:
                artifact_root = Path(tmp).resolve()
                self.assertEqual(runner.main(["--experiment", preset, "--run-id", "preflight",
                                              "--artifact-root", str(artifact_root), "--preflight-only", *overrides]), 0)
                directory = artifact_root / "preflight"
                manifest = json.loads((directory / "experiment-manifest.json").read_text(encoding="utf-8"))
                environment = json.loads((directory / "environment.json").read_text(encoding="utf-8"))
                schedule = [json.loads(line) for line in (directory / "schedule.jsonl").read_text(encoding="utf-8").splitlines()]
                self.assertEqual(manifest["experiment"], preset)
                self.assertEqual(environment["experiment"], preset)
                self.assertEqual(manifest["target_count"], target_count)
                self.assertEqual(environment["target_count"], target_count)
                self.assertEqual(environment["online_budget_seconds"], 20)
                self.assertEqual(environment["trial_count"], 1)
                self.assertEqual(environment["cpus"], cpus)
                self.assertEqual(len(schedule), row_count)
                self.assertEqual({row["tool"] for row in schedule}, set(runner.DEFAULT_TOOL_ORDER))
                if preset == "smoke":
                    self.assertEqual({row["target_id"] for row in schedule}, set(SMOKE_TARGETS))
                for action in (images, prepare, online, load, export):
                    action.assert_not_called()
                self.assertFalse((directory / "metrics.csv").exists())

    def test_success_exports_once_for_matrix_single_tool_and_legacy_entry(self):
        cases = [([], False), (["--tools", "semantist"], False), (["--tool", "semantist"], True)]
        for selection, legacy in cases:
            with self.subTest(selection=selection, legacy=legacy), tempfile.TemporaryDirectory() as tmp, mocked_host(), \
                    patch.object(runner, "prepare_supported_targets", return_value={}), \
                    patch.object(runner, "run_online_matrix"), \
                    patch.object(runner, "write_raw_runs"), \
                    patch.object(runner, "validate_complete", return_value=[]) as validate, \
                    patch.object(runner, "load_runs", return_value=sentinel.runs) as load, \
                    patch.object(runner, "export_metrics") as export:
                directory = Path(tmp).resolve() / "success"
                events = []
                validate.side_effect = lambda *args, **kwargs: events.append("validate") or []
                export.side_effect = lambda *args, **kwargs: events.append("export")
                arguments = ["--experiment", "smoke", "--run-id", "success", "--artifact-root", tmp,
                             "--target-id", SMOKE_TARGETS[0], *selection]
                self.assertEqual((run_tool.main if legacy else runner.main)(arguments), 0)
                validate.assert_called_once_with(directory, require_valid_runs=True)
                load.assert_called_once_with([directory])
                export.assert_called_once_with(sentinel.runs, directory)
                self.assertEqual(events, ["validate", "export"])

    def test_failed_validation_never_loads_or_exports_metrics(self):
        with tempfile.TemporaryDirectory() as tmp, mocked_host(), \
                patch.object(runner, "prepare_supported_targets", return_value={}), \
                patch.object(runner, "run_online_matrix"), \
                patch.object(runner, "write_raw_runs"), \
                patch.object(runner, "validate_complete", return_value=["native budget incomplete"]), \
                patch.object(runner, "load_runs") as load, \
                patch.object(runner, "export_metrics") as export:
            self.assertEqual(runner.main(["--experiment", "smoke", "--run-id", "failed",
                                          "--artifact-root", tmp, "--target-id", SMOKE_TARGETS[0]]), 1)
            load.assert_not_called()
            export.assert_not_called()


if __name__ == "__main__":
    unittest.main()
