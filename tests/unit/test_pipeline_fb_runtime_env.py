import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fuzzer.pipeline.core import (  # noqa: E402
    Layout,
    build_targets,
    record_build_failure,
    run_campaign,
)


class PipelineFunctionBlockRuntimeEnvTests(unittest.TestCase):
    def test_build_targets_uses_only_semantic_ir_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st_file = root / "target.st"
            st_file.write_text("FUNCTION TEST : INT\nTEST := 0;\nEND_FUNCTION\n", encoding="utf-8")
            layout = Layout.from_run_dir(root / "run", root / "build")
            layout.mkdirs()
            library = root / "library.st"
            stubs = root / "stubs.st"
            compatibility_manifest = root / "compatibility.json"
            args = argparse.Namespace(
                st_file=st_file,
                function="TEST",
                library=library,
                stubs=stubs,
                compatibility_manifest=compatibility_manifest,
                profile="codesys-memory",
                build_timeout=5.0,
            )
            calls: list[tuple[list[str], dict[str, str], float | None]] = []

            def record(command: list[str], env=None, timeout=None) -> None:
                calls.append((command, env or {}, timeout))

            with patch("fuzzer.pipeline.core.run_command", side_effect=record):
                target = build_targets(args, layout)

            self.assertEqual(target, root / "build" / "fuzz_target")
            self.assertEqual(len(calls), 2)
            for _command, env, _timeout in calls:
                self.assertEqual(env["SEMANTIST_SEMANTIC_INSTRUMENTATION"], "ir")
                self.assertEqual(env["SEMANTIST_INPROCESS_REQUIRED"], "1")
                self.assertEqual(env["SEMANTIST_LIBRARY_FILE"], str(library))
                self.assertEqual(env["SEMANTIST_LIBRARY_STUBS"], str(stubs))
                self.assertEqual(
                    env["SEMANTIST_COMPATIBILITY_MANIFEST"], str(compatibility_manifest)
                )
                self.assertEqual(env["SEMANTIST_PROFILE"], "codesys-memory")
                self.assertNotIn("SEMANTIST_SEMANTIC_MANIFEST", env)
                self.assertIn("SEMANTIST_BUILD_DEADLINE_EPOCH_NS", env)
            self.assertGreater(calls[0][2], 0.0)
            self.assertGreater(calls[1][2], 0.0)
            self.assertLessEqual(calls[1][2], calls[0][2])
            self.assertLessEqual(calls[0][2], 5.0)

    def test_build_timeout_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = Layout.from_run_dir(root / "run", root / "build")
            layout.mkdirs()
            args = argparse.Namespace(
                st_file=root / "target.st", function="TEST", library=None, stubs=None,
                compatibility_manifest=None, profile=None, build_timeout=0,
            )
            with self.assertRaisesRegex(ValueError, "build-timeout"):
                build_targets(args, layout)

    def test_campaign_constructor_failure_is_recorded_as_campaign_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = Layout.from_run_dir(root / "run", root / "build")
            layout.mkdirs()
            args = SimpleNamespace(
                function="TEST", st_file=root / "target.st", library=None, stubs=None,
                compatibility_manifest=None, profile=None, fuzz_timeout=600, search_jobs=1,
                seed=1, fuzz_iterations=1, no_symbolic=False, symbolic_initial_only=False,
                symbolic_start="import", no_goal_prefix=False, no_distance=False,
                no_time_guidance=False, no_snapshot=False, no_state_copy=False,
                llm=False, llm_initial_only=False, solver_jobs=1, llm_max_calls=0,
                llm_offline=False, full_fuzz_duration=False, full_hybrid_duration=True,
            )
            args.st_file.write_text("FUNCTION TEST : INT\nEND_FUNCTION\n", encoding="utf-8")
            with patch("fuzzer.pipeline.core.Campaign", side_effect=ValueError("missing frame_fields")) as campaign:
                with self.assertRaises(ValueError):
                    run_campaign(args, layout, root / "target", {"contract_id": "c", "time": {}})
            self.assertEqual(campaign.call_args.kwargs["time_budget_s"], 600)
            self.assertTrue(json.loads((layout.run_dir / "campaign.json").read_text())["full_hybrid_duration"])
            events = [json.loads(line) for line in (layout.events).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["event"], "campaign_failed")
            self.assertEqual(events[-1]["execution_status"], "campaign_failed")
            status = json.loads((layout.run_dir / "campaign-status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "campaign_failed")

    def test_build_failure_is_recorded_before_report_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = Layout.from_run_dir(root / "run", root / "build")
            layout.mkdirs()
            args = SimpleNamespace(
                skip_report=False,
                function="TEST",
                st_file=root / "target.st",
                report_output_dir=None,
                report_replay_limit=0,
                report_replay_timeout=1.0,
                report_no_replay=True,
                report_minimize_findings=False,
            )
            with patch("fuzzer.pipeline.core.write_vulnerability_report", return_value=True) as report:
                self.assertFalse(record_build_failure(args, layout, RuntimeError("codegen exit 17")))
            report.assert_called_once_with(args, layout)
            events = [json.loads(line) for line in layout.events.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["event"], "build_failed")
            status = json.loads((layout.run_dir / "campaign-status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "build_failed")
            self.assertEqual(status["execution_status"]["build"], "failed")

if __name__ == "__main__":
    unittest.main()
