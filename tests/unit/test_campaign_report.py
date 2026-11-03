import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fuzzer.reporting.report import ReplayResult, build_report  # noqa: E402


class V3ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="semantist-v3-report-")
        self.run_dir = Path(self.tmp.name)
        self.package = self.run_dir / "findings" / "confirmed" / "F0001"
        self.package.mkdir(parents=True)
        (self.package / "minimized.txt").write_text("0.Enable,BOOL,1\n", encoding="utf-8")
        (self.package / "testcase.txt").write_text("0.Enable,BOOL,0\n", encoding="utf-8")
        (self.package / "finding.json").write_text(
            json.dumps(
                {
                    "finding_id": "F0001",
                    "contract_id": "contract",
                    "function": "FB_MAIN",
                    "oracle": "array_access",
                    "goal": {
                        "runtime_id": 17,
                        "stable_id": None,
                        "source": {"file": "target.st", "line": 42},
                    },
                    "confirmation": "repeatable",
                    "cause_evidence": "semantic runtime hit",
                    "history_evidence": "history_dependent",
                    "provenance": {"source": "fuzz"},
                }
            ),
            encoding="utf-8",
        )
        # These are bundle sidecars, not findings.  The old reporter counted
        # every schema_version-bearing JSON file as an unknown finding.
        (self.package / "execution-contract.json").write_text(
            json.dumps({"schema_version": "semantist.execution-contract/1.0.0"}),
            encoding="utf-8",
        )
        (self.package / "source").mkdir()
        (self.package / "source" / "stg-model.json").write_text(
            json.dumps({"schema_version": "semantist.stg/1.2.0"}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_v3_finding_is_counted_once_and_uses_minimized_reproducer(self) -> None:
        target = self.run_dir / "build" / "fuzz_target"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"placeholder")
        replay = ReplayResult(
            path=str(self.package / "minimized.txt"),
            size=1,
            status="ok",
            returncode=0,
            stderr_preview="",
        )

        with patch("fuzzer.reporting.report.replay_one", return_value=replay) as replay_one:
            report = build_report(self.run_dir, function="FB_MAIN")

        self.assertEqual(report.summary["raw_finding_record_count"], 1)
        self.assertEqual(report.summary["deduplicated_finding_count"], 1)
        self.assertEqual(report.summary["confirmed_finding_count"], 1)
        self.assertEqual(report.summary["status"], "confirmed_high_risk")
        self.assertEqual(len(report.evidence), 1)
        self.assertEqual(report.evidence[0].confidence, "confirmed")
        self.assertEqual(report.evidence[0].kind, "semantic_violation")
        self.assertEqual(replay_one.call_count, 1)
        self.assertEqual(replay_one.call_args.args[1], self.package / "minimized.txt")

    def test_v3_package_does_not_create_generic_unverified_evidence(self) -> None:
        report = build_report(self.run_dir, function="FB_MAIN", replay=False)

        self.assertEqual(report.summary["confirmed_finding_count"], 1)
        self.assertEqual(report.summary["status"], "confirmed_high_risk")
        self.assertEqual(report.corpus["findings_files"], 0)
        self.assertEqual(len(report.evidence), 1)
        self.assertEqual(report.evidence[0].confidence, "confirmed")

    def test_runtime_only_confirmation_with_incomplete_verification_is_inconclusive(self) -> None:
        (self.run_dir / "report.json").write_text(json.dumps({
            "confirmed": 1,
            "source_risk_confirmed_under_model": 0,
            "verification_status": "incomplete",
            "probe_audit": {"status": "INCOMPLETE", "platform_assumptions": []},
        }), encoding="utf-8")
        report = build_report(self.run_dir, function="FB_MAIN", replay=False)
        self.assertEqual(report.summary["confirmed"], 1)
        self.assertEqual(report.summary["status"], "inconclusive")

    def test_campaign_failure_preserves_execution_status_details(self) -> None:
        (self.run_dir / "report.json").write_text(
            json.dumps({
                "candidates": 2,
                "confirmed": 0,
                "support_status": "approximate",
                "execution_status": {
                    "build": "completed",
                    "native": "failed",
                    "symbolic": "UNSUPPORTED",
                },
            }),
            encoding="utf-8",
        )
        (self.run_dir / "events.jsonl").write_text(
            json.dumps({"event": "v3_campaign_failed", "error": "engine exit 9"}) + "\n",
            encoding="utf-8",
        )

        report = build_report(self.run_dir, function="FB_MAIN", replay=False)

        self.assertEqual(report.summary["status"], "campaign_failed")
        self.assertEqual(report.summary["candidates"], 2)
        self.assertEqual(report.summary["confirmed"], 0)
        self.assertEqual(report.summary["execution_status"], {
            "build": "completed",
            "native": "failed",
            "symbolic": "UNSUPPORTED",
            "campaign": "FAILED",
        })
        self.assertEqual(report.summary["failure"]["error"], "engine exit 9")

    def test_build_failure_is_not_reported_as_clean_run(self) -> None:
        (self.run_dir / "events.jsonl").write_text(
            json.dumps({"event": "build_failed", "error": "codegen exit 17"}) + "\n",
            encoding="utf-8",
        )

        report = build_report(self.run_dir, function="FB_MAIN", replay=False)

        self.assertEqual(report.summary["status"], "build_failed")
        self.assertEqual(report.summary["execution_status"], {"campaign": "FAILED"})
        self.assertEqual(report.summary["failure"]["event"], "build_failed")

    def test_unresolved_symbolic_query_is_inconclusive(self) -> None:
        shutil.rmtree(self.package)
        (self.run_dir / "report.json").write_text(
            json.dumps({
                "candidates": 0,
                "confirmed": 0,
                "symbolic_queries": [{
                    "status": "UNKNOWN",
                    "model_scope": {"imprecise": ["SIN__REAL"]},
                }],
                "execution_status": {"build": "completed", "symbolic": "completed"},
            }),
            encoding="utf-8",
        )
        report = build_report(self.run_dir, function="FB_MAIN", replay=False)
        assert report.summary["status"] == "inconclusive"
        assert report.summary["symbolic_queries"][0]["status"] == "UNKNOWN"

    def test_disabled_symbolic_phase_is_inconclusive_without_queries(self) -> None:
        shutil.rmtree(self.package)
        (self.run_dir / "report.json").write_text(
            json.dumps({
                "candidates": 0,
                "confirmed": 0,
                "symbolic_queries": [],
                "execution_status": {
                    "build": "completed",
                    "native": "completed",
                    "symbolic": "UNSUPPORTED",
                    "symbolic_reason": "disabled_by_no_symbolic_ablation",
                },
            }),
            encoding="utf-8",
        )

        report = build_report(self.run_dir, function="FB_MAIN", replay=False)

        self.assertEqual(report.summary["status"], "inconclusive")
        self.assertEqual(report.summary["execution_status"]["symbolic"], "UNSUPPORTED")

    def test_unreplayed_engine_candidate_is_inconclusive(self) -> None:
        shutil.rmtree(self.package)
        replay_results = [{"testcase_file": "findings/candidates/candidate-1.txt",
                           "status": "REPLAY_MISMATCH"}]
        (self.run_dir / "report.json").write_text(json.dumps({
            "candidates": 0, "confirmed": 0, "symbolic_queries": [],
            "native_replay_results": replay_results,
            "execution_status": {"build": "completed", "native": "completed",
                                 "symbolic": "completed"},
        }), encoding="utf-8")

        report = build_report(self.run_dir, function="FB_MAIN", replay=False)

        self.assertEqual(report.summary["status"], "inconclusive")
        self.assertEqual(report.summary["native_replay_results"], replay_results)


if __name__ == "__main__":
    unittest.main()
