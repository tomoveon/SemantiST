"""Unit tests for finding deduplication and backward compatibility."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fuzzer.reporting.findings import (  # noqa: E402
    MAX_REPRESENTATIVE_SEEDS,
    FindingDeduplicator,
    FindingRecord,
    FindingSignature,
    _replay_status_from_replay_result,
    build_signature,
)


class BuildSignatureTests(unittest.TestCase):
    """Signature tier selection."""

    def test_semantic_tier_when_target_id_present(self) -> None:
        sig = build_signature(
            function="CTU_DINT",
            kind="semantic_violation",
            semantic_target_id="target::div_zero",
            hazard_kind="division_by_zero",
            source_location="ctu_dint.st:42",
        )
        self.assertEqual(sig.tier, "semantic")
        self.assertIn("target::div_zero", sig.key())
        self.assertIn("division_by_zero", sig.key())

    def test_semantic_tier_when_hazard_kind_present(self) -> None:
        sig = build_signature(
            function="FOO",
            kind="nonzero",
            hazard_kind="buffer_overflow",
        )
        self.assertEqual(sig.tier, "semantic")

    def test_runtime_tier_when_stack_hash_present(self) -> None:
        sig = build_signature(
            function="BAR",
            kind="crash",
            stack_hash="abcd1234",
            exit_kind="crash",
        )
        self.assertEqual(sig.tier, "runtime")
        self.assertIn("abcd1234", sig.key())

    def test_runtime_tier_when_source_location_present(self) -> None:
        sig = build_signature(
            function="BAZ",
            kind="timeout",
            source_location="baz.st:99",
        )
        self.assertEqual(sig.tier, "runtime")
        self.assertIn("baz.st:99", sig.key())

    def test_fallback_tier_when_minimal_info(self) -> None:
        sig = build_signature(
            function="QUX",
            kind="oom",
            seed_content_hash="deadbeef",
            exit_kind="oom",
        )
        self.assertEqual(sig.tier, "fallback")
        self.assertEqual(sig.seed_content_hash, "deadbeef")
        self.assertIn("QUX", sig.key())

    def test_key_is_stable_and_reproducible(self) -> None:
        a = build_signature(
            function="F", kind="crash", stack_hash="abc", exit_kind="crash"
        )
        b = build_signature(
            function="F", kind="crash", stack_hash="abc", exit_kind="crash"
        )
        self.assertEqual(a.key(), b.key())

    def test_different_functions_produce_different_keys(self) -> None:
        a = build_signature(function="F1", kind="crash")
        b = build_signature(function="F2", kind="crash")
        self.assertNotEqual(a.key(), b.key())


class FindingDeduplicatorTests(unittest.TestCase):
    """Core deduplication behavior."""

    def setUp(self) -> None:
        self.dedup = FindingDeduplicator()

    def test_empty_deduplicator_has_zero_counts(self) -> None:
        self.assertEqual(self.dedup.raw_count, 0)
        self.assertEqual(self.dedup.dedup_count, 0)
        self.assertEqual(self.dedup.duplicate_total, 0)

    def test_first_finding_creates_new_record(self) -> None:
        record = self.dedup.add(function="F", kind="crash", exit_kind="crash")
        self.assertEqual(self.dedup.raw_count, 1)
        self.assertEqual(self.dedup.dedup_count, 1)
        self.assertEqual(record.duplicate_count, 0)
        self.assertEqual(record.error_kind, "crash")

    def test_same_signature_increments_duplicate_count(self) -> None:
        self.dedup.add(function="F", kind="crash", exit_kind="crash")
        record = self.dedup.add(function="F", kind="crash", exit_kind="crash")
        self.assertEqual(self.dedup.raw_count, 2)
        self.assertEqual(self.dedup.dedup_count, 1)
        self.assertEqual(record.duplicate_count, 1)

    def test_different_seed_hashes_do_not_split_fallback_runtime_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="semantist-seeds-") as tmp:
            tmp_path = Path(tmp)
            seed_a = tmp_path / "a.seed"
            seed_b = tmp_path / "b.seed"
            seed_a.write_text("A,INT,1\n", encoding="utf-8")
            seed_b.write_text("A,INT,2\n", encoding="utf-8")
            self.dedup.add(function="F", kind="crash", reproducer_path=seed_a)
            record = self.dedup.add(function="F", kind="crash", reproducer_path=seed_b)

        self.assertEqual(self.dedup.raw_count, 2)
        self.assertEqual(self.dedup.dedup_count, 1)
        self.assertEqual(record.duplicate_count, 1)

    def test_different_signatures_create_separate_records(self) -> None:
        self.dedup.add(function="F", kind="crash", exit_kind="crash")
        self.dedup.add(function="F", kind="timeout", exit_kind="timeout")
        self.assertEqual(self.dedup.raw_count, 2)
        self.assertEqual(self.dedup.dedup_count, 2)

    def test_different_functions_are_different_signatures(self) -> None:
        self.dedup.add(function="F1", kind="crash", exit_kind="crash")
        self.dedup.add(function="F2", kind="crash", exit_kind="crash")
        self.assertEqual(self.dedup.dedup_count, 2)

    def test_semantic_and_runtime_same_function_are_separate(self) -> None:
        self.dedup.add(
            function="F", kind="crash",
            semantic_target_id="t1", hazard_kind="div0",
        )
        self.dedup.add(
            function="F", kind="crash",
            exit_kind="crash", stack_hash="abc",
        )
        self.assertEqual(self.dedup.dedup_count, 2)

    def test_representative_seeds_are_capped(self) -> None:
        for i in range(MAX_REPRESENTATIVE_SEEDS + 5):
            self.dedup.add(
                function="F", kind="crash",
                reproducer_path=Path(f"/tmp/seed_{i}.seed"),
            )
        record = self.dedup.records[0]
        self.assertLessEqual(len(record.representative_seeds), MAX_REPRESENTATIVE_SEEDS)

    def test_duplicate_total_is_sum_of_all_duplicates(self) -> None:
        self.dedup.add(function="F1", kind="crash")
        self.dedup.add(function="F1", kind="crash")
        self.dedup.add(function="F1", kind="crash")
        self.dedup.add(function="F2", kind="timeout")
        self.dedup.add(function="F2", kind="timeout")
        # F1: 2 duplicates, F2: 1 duplicate
        self.assertEqual(self.dedup.duplicate_total, 3)

    def test_replay_status_keeps_best(self) -> None:
        self.dedup.add(function="F", kind="crash", replay_status="candidate")
        record = self.dedup.add(function="F", kind="crash", replay_status="confirmed")
        self.assertEqual(record.replay_status, "confirmed")

    def test_ok_replay_is_unstable_not_confirmed(self) -> None:
        self.assertEqual(
            _replay_status_from_replay_result({"status": "ok"}),
            "unstable",
        )

    def test_summary_reflects_all_counts(self) -> None:
        self.dedup.add(function="F", kind="crash", replay_status="confirmed")
        self.dedup.add(function="F", kind="crash", replay_status="confirmed")
        self.dedup.add(function="G", kind="timeout", replay_status="unstable")
        s = self.dedup.summary()
        self.assertEqual(s["raw_count"], 3)
        self.assertEqual(s["deduplicated_count"], 2)
        self.assertEqual(s["confirmed_count"], 1)
        self.assertEqual(s["unstable_count"], 1)
        self.assertEqual(s["duplicate_count"], 1)


class PersistenceAndBackwardCompatTests(unittest.TestCase):
    """JSON round-trip and backward compatibility."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="semantist-test-")
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_round_trip_through_json(self) -> None:
        dedup = FindingDeduplicator()
        dedup.add(
            function="CTU_DINT", kind="crash",
            semantic_target_id="target::1", hazard_kind="div_zero",
            source_location="ctu_dint.st:42",
            replay_status="confirmed",
        )
        # Same signature (all fields match) — should be deduplicated
        dedup.add(
            function="CTU_DINT", kind="crash",
            semantic_target_id="target::1", hazard_kind="div_zero",
            source_location="ctu_dint.st:42",
        )
        dedup.add(
            function="TOF", kind="timeout",
            exit_kind="timeout", stack_hash="ff0011",
        )

        # Persist
        output_dir = self.tmp_path / "deduplicated"
        paths = dedup.persist(output_dir)
        self.assertEqual(len(paths), 2)

        # Reload into fresh deduplicator
        dedup2 = FindingDeduplicator()
        loaded = dedup2.load_legacy_records(output_dir)
        self.assertEqual(loaded, 2)
        self.assertEqual(dedup2.dedup_count, 2)

        # Verify the records match
        s1 = dedup.summary()
        s2 = dedup2.summary()
        self.assertEqual(s1["deduplicated_count"], s2["deduplicated_count"])
        # Raw count won't match exactly since load_legacy doesn't know the original raw count
        self.assertGreaterEqual(dedup2.raw_count, 2)

    def test_old_finding_json_is_not_misinterpreted_as_dedup_record(self) -> None:
        """Legacy finding JSONs should not be loaded as dedup records."""
        legacy = {
            "schema_version": "1.0",
            "source": "fuzzing",
            "kind": "crash",
            "reproducer": "some/path.seed",
            "content_hash": "abc123",
        }
        legacy_dir = self.tmp_path / "legacy_findings"
        legacy_dir.mkdir()
        (legacy_dir / "crash_1.json").write_text(json.dumps(legacy))

        dedup = FindingDeduplicator()
        loaded = dedup.load_legacy_records(legacy_dir)
        # Should not load because schema_version is "1.0", not "finding-record-1.0"
        self.assertEqual(loaded, 0)

    def test_dedup_record_json_is_self_describing(self) -> None:
        """A persisted record can be deserialized without the FindingRecord class."""
        dedup = FindingDeduplicator()
        dedup.add(function="F", kind="crash", replay_status="confirmed")
        output_dir = self.tmp_path / "deduplicated"
        paths = dedup.persist(output_dir)
        self.assertEqual(len(paths), 1)

        raw = json.loads(paths[0].read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], "finding-record-1.0")
        self.assertEqual(raw["error_kind"], "crash")
        self.assertEqual(raw["function"], "F")
        self.assertIn("signature", raw)
        self.assertIn("signature_detail", raw)
        self.assertIn("representative_seeds", raw)
        self.assertIn("duplicate_count", raw)
        self.assertIn("replay_status", raw)

    def test_add_from_finding_json_extracts_fields(self) -> None:
        raw_finding = {
            "function": "TEST_FUNC",
            "kind": "crash",
            "semantic_target_id": "stg::42",
            "hazard_kind": "bounds_check",
            "source_location": "test.st:10",
            "replay": {
                "status": "crash",
                "returncode": -11,
                "stderr_preview": "SEGV on address 0x0",
            },
        }
        dedup = FindingDeduplicator()
        record = dedup.add_from_finding_json(raw_finding, Path("/tmp/test.json"))
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.function, "TEST_FUNC")
        self.assertEqual(record.error_kind, "crash")
        self.assertEqual(record.replay_status, "confirmed")
        self.assertEqual(record.signature.tier, "semantic")


class FindingSignatureDisplayTests(unittest.TestCase):
    """Human-readable display strings."""

    def test_display_includes_key_info(self) -> None:
        sig = FindingSignature(
            function="F", kind="crash", tier="semantic",
            semantic_target_id="t1", hazard_kind="div0", source_location="f.st:1",
        )
        display = sig.display()
        self.assertIn("semantic", display)
        self.assertIn("F", display)
        self.assertIn("div0", display)


if __name__ == "__main__":
    unittest.main()
