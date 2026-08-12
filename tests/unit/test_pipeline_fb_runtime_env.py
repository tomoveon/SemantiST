import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fuzzer.pipeline.core import Layout, apply_fb_runtime_env, build_targets  # noqa: E402


class PipelineFunctionBlockRuntimeEnvTests(unittest.TestCase):
    def test_fb_runtime_args_map_to_environment(self) -> None:
        env = {}
        args = argparse.Namespace(
            fb_max_cycles=5,
            fb_stale_threshold=1,
            fb_state_trace=True,
            fb_state_trace_file=Path("/tmp/fb-trace.log"),
        )

        apply_fb_runtime_env(args, env)

        self.assertEqual(env["SEMANTIST_FB_MAX_CYCLES"], "5")
        self.assertEqual(env["SEMANTIST_FB_STALE_THRESHOLD"], "1")
        self.assertEqual(env["SEMANTIST_FB_STATE_TRACE"], "1")
        self.assertEqual(env["SEMANTIST_FB_STATE_TRACE_FILE"], "/tmp/fb-trace.log")

    def test_fb_trace_file_is_ignored_unless_trace_is_enabled(self) -> None:
        env = {}
        args = argparse.Namespace(
            fb_max_cycles=None,
            fb_stale_threshold=None,
            fb_state_trace=False,
            fb_state_trace_file=Path("/tmp/fb-trace.log"),
        )

        apply_fb_runtime_env(args, env)

        self.assertEqual(env, {})

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
            )
            calls: list[tuple[list[str], dict[str, str]]] = []

            def record(command: list[str], env=None, timeout=None) -> None:
                del timeout
                calls.append((command, env or {}))

            with patch("fuzzer.pipeline.core.run_command", side_effect=record):
                target = build_targets(args, layout)

            self.assertEqual(target, root / "build" / "fuzz_target")
            self.assertEqual(len(calls), 2)
            for _command, env in calls:
                self.assertEqual(env["SEMANTIST_SEMANTIC_INSTRUMENTATION"], "ir")
                self.assertEqual(env["SEMANTIST_LIBRARY_FILE"], str(library))
                self.assertEqual(env["SEMANTIST_LIBRARY_STUBS"], str(stubs))
                self.assertEqual(
                    env["SEMANTIST_COMPATIBILITY_MANIFEST"], str(compatibility_manifest)
                )
                self.assertNotIn("SEMANTIST_SEMANTIC_MANIFEST", env)


if __name__ == "__main__":
    unittest.main()
