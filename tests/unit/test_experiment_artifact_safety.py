"""A new experiment ID must never replace or reuse earlier artifacts."""

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from experiments.comparison.adapters.aflplusplus import AFLPlusPlusAdapter
from experiments.comparison.adapters.base import AdapterError, BaseAdapter, ExperimentConfig, PreprocessingResult
from experiments.comparison.adapters.icsfuzz import ICSFuzzAdapter
from experiments.comparison.adapters.semantist import SemantiSTAdapter


ROOT = Path(__file__).resolve().parents[2]


def artifact_config(root: Path) -> ExperimentConfig:
    return ExperimentConfig(
        experiment_id="preserved-experiment",
        semantist_root=ROOT,
        workspace_root=ROOT.parent,
        artifact_root=root,
        icsquartz_root=None,
        online_budget_seconds=1,
        per_execution_timeout_ms=1000,
        semantic_task_budget=1000,
        semantist_fb_max_cycles=256,
        semantist_fb_stale_threshold=0,
        semantist_fb_state_trace=False,
        observer_poll_interval_ms=10,
        cpuset=None,
        container_runtime=None,
        container_runtime_status="not needed for artifact safety checks",
        container_platform=None,
        semantist_image="semantist:test",
    )


class ExperimentArtifactSafetyTests(unittest.TestCase):
    def test_existing_preprocessing_is_rejected_even_when_empty(self):
        for adapter_type in (SemantiSTAdapter, AFLPlusPlusAdapter, ICSFuzzAdapter):
            for has_payload in (False, True):
                with self.subTest(adapter=adapter_type.__name__, has_payload=has_payload):
                    with tempfile.TemporaryDirectory() as tmp:
                        adapter = adapter_type(artifact_config(Path(tmp)))
                        target = SimpleNamespace(suite="suite", target_id="target")
                        directory = adapter.preprocessing_dir(target)
                        directory.mkdir(parents=True)
                        marker = directory / "previous-target"
                        if has_payload:
                            marker.write_bytes(b"previous compilation must survive")
                        before = sorted(directory.iterdir())

                        with self.assertRaises(AdapterError) as caught:
                            adapter.prepare(target)

                        self.assertEqual(caught.exception.code, "preprocessing_directory_exists")
                        self.assertIn("new experiment ID", str(caught.exception))
                        self.assertEqual(sorted(directory.iterdir()), before)
                        if has_payload:
                            self.assertEqual(marker.read_bytes(), b"previous compilation must survive")

    def test_existing_trial_is_preserved_by_each_inheriting_adapter(self):
        for adapter_type in (BaseAdapter, SemantiSTAdapter, AFLPlusPlusAdapter, ICSFuzzAdapter):
            for has_payload in (False, True):
                with self.subTest(adapter=adapter_type.__name__, has_payload=has_payload):
                    with tempfile.TemporaryDirectory() as tmp:
                        adapter = adapter_type(artifact_config(Path(tmp)))
                        target = SimpleNamespace(suite="suite", target_id="target")
                        directory = adapter.run_dir(target, 1)
                        directory.mkdir(parents=True)
                        marker = directory / "run-result.json"
                        if has_payload:
                            marker.write_bytes(b'{"run_status":"budget_completed"}\n')
                        before = sorted(directory.iterdir())
                        preprocessing = PreprocessingResult("completed", Path(tmp) / "prep", 0, 0)

                        with self.assertRaises(AdapterError) as caught:
                            adapter.materialize_trial(target, preprocessing, 1)

                        self.assertEqual(caught.exception.code, "trial_directory_exists")
                        self.assertEqual(sorted(directory.iterdir()), before)
                        if has_payload:
                            self.assertEqual(marker.read_bytes(), b'{"run_status":"budget_completed"}\n')

    def test_new_trial_can_be_created_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = BaseAdapter(artifact_config(Path(tmp)))
            target = SimpleNamespace(suite="suite", target_id="target")
            preprocessing = PreprocessingResult("completed", Path(tmp) / "prep", 0, 0)

            directory = adapter.materialize_trial(target, preprocessing, 1)

            for child in ("events", "corpus", "findings", "tool-artifacts", "logs"):
                self.assertTrue((directory / child).is_dir())
            with self.assertRaises(AdapterError):
                adapter.materialize_trial(target, preprocessing, 1)


if __name__ == "__main__":
    unittest.main()
