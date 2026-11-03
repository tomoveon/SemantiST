import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fuzzer.reporting.report import infer_seed_cycle_metadata  # noqa: E402


class ReportCycleMetadataTests(unittest.TestCase):
    def test_cycle_metadata_is_inferred_from_structured_seed(self) -> None:
        metadata = infer_seed_cycle_metadata(
            "LIST,STRING,;a;b\n0.RST,BOOL,1\n0.SEP,BYTE,59\n2.RST,BOOL,0\n2.SEP,BYTE,59\n"
        )

        self.assertEqual(metadata["target_kind"], "FUNCTION_BLOCK")
        self.assertEqual(metadata["cycle_count"], 2)
        self.assertEqual(metadata["cycle_ids"], [0, 2])

    def test_seed_without_cycle_prefix_is_function_metadata(self) -> None:
        metadata = infer_seed_cycle_metadata("X,INT,0\nD,INT,1\n")

        self.assertEqual(metadata["target_kind"], "FUNCTION")
        self.assertEqual(metadata["cycle_count"], 0)
        self.assertEqual(metadata["cycle_ids"], [])


if __name__ == "__main__":
    unittest.main()
