"""Ablation entry points only change a declared SemantiST runtime profile."""

from __future__ import annotations

import os
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from experiments.ablations.run_ablation import runner_arguments  # noqa: E402
from experiments.ablations.run_ablation_suite import main as suite_main, suite_arguments  # noqa: E402
from experiments.ablations.run_ablation_smoke import DEFAULT_ARGS, main as smoke_main  # noqa: E402
from experiments.comparison.run_full_comparison import main as comparison_main, parse_args  # noqa: E402


class AblationExperimentTests(unittest.TestCase):
    def test_smoke_inherits_podman_runtime_selection(self):
        prefix, _, forwarded = suite_arguments(DEFAULT_ARGS)
        self.assertNotIn("--container-runtime", forwarded)
        with patch.dict(os.environ, {"CONTAINER_RUNTIME": "podman"}):
            parsed = parse_args([
                "--run-id", f"{prefix}_full", "--tools", "semantist_full", *forwarded,
            ])
        self.assertEqual(parsed.container_runtime, "podman")

    def test_single_run_forwards_only_semantist_profile_and_common_budget(self):
        forwarded = runner_arguments([
            "--variant", "no_semantic_init", "--run-id", "test_init",
            "--target-id", "oscat_basic_inc2", "--fuzz-time", "20",
        ])
        self.assertEqual(forwarded[:6], [
            "--tool", "semantist", "--run-id", "test_init",
            "--semantist-ablation", "no_semantic_init",
        ])
        parsed = parse_args([*forwarded[4:], "--tools", "semantist_full"])
        self.assertEqual(parsed.semantist_ablation, "no_semantic_init")
        self.assertEqual(parsed.fuzz_time, 20)
        self.assertEqual(parsed.target_id, ["oscat_basic_inc2"])

    def test_ablations_cannot_be_mixed_with_baseline_tools(self):
        with self.assertRaisesRegex(SystemExit, "independent run"):
            comparison_main([
                "--semantist-ablation", "no_semantic_init",
                "--tools", "semantist_full,aflplusplus_full",
            ])

    def test_suite_uses_distinct_runs_with_the_same_forwarded_arguments(self):
        prefix, variants, remaining = suite_arguments([
            "--run-prefix", "test_fb", "--variants", "full,no_multicycle_guidance",
            "--target-id", "scan_cycle_smart_oobw_1", "--trials", "3",
        ])
        self.assertEqual((prefix, variants), ("test_fb", ["full", "no_multicycle_guidance"]))
        self.assertEqual(remaining, ["--target-id", "scan_cycle_smart_oobw_1", "--trials", "3"])
        with patch("experiments.ablations.run_ablation_suite.run_ablation_main", return_value=0) as run:
            self.assertEqual(suite_main([
                "--run-prefix", "test_fb", "--variants", "full,no_multicycle_guidance",
                "--trials", "3",
            ]), 0)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], [
            "--variant", "full", "--run-id", "test_fb_full", "--trials", "3",
        ])
        self.assertEqual(run.call_args_list[1].args[0], [
            "--variant", "no_multicycle_guidance", "--run-id",
            "test_fb_no_multicycle_guidance", "--trials", "3",
        ])

    def test_smoke_rejects_a_structurally_complete_but_failed_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for variant in ("full", "no_multicycle_guidance"):
                path = root / f"test_{variant}" / "runs" / "semantist_full" / "suite" / "target" / "trial_01" / "run-result.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({
                    "run_status": "runner_error" if variant == "no_multicycle_guidance" else "budget_completed",
                    "paper_metrics": {"valid_run": variant == "full", "total_executions_reported": 12},
                }), encoding="utf-8")
            with patch("experiments.ablations.run_ablation_smoke.suite_main", return_value=0):
                self.assertEqual(smoke_main([
                    "--run-prefix", "test", "--artifact-root", str(root),
                ]), 1)


if __name__ == "__main__":
    unittest.main()
