import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fuzzer.reporting.minimize import minimize_seed  # noqa: E402


FAKE_TARGET = """#!/usr/bin/env python3
import os
import resource
import signal
import sys

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

values = {}
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    name, _ty, value = line.split(",", 2)
    values[name] = value

mode = values.get("MODE", "")
try:
    count = int(values.get("COUNT", "0"), 0)
except ValueError:
    count = 0
buf = values.get("BUF", "")

if mode.startswith("C") and count > 10 and "ff" in buf.lower():
    print("synthetic crash", file=sys.stderr)
    os.kill(os.getpid(), signal.SIGABRT)
sys.exit(0)
"""

SANITIZER_TARGET = """#!/usr/bin/env python3
import sys

values = {}
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    name, _ty, value = line.split(",", 2)
    values[name] = value

mode = values.get("MODE", "")
try:
    count = int(values.get("COUNT", "0"), 0)
except ValueError:
    count = 0

if count > 10 and mode.startswith("C"):
    print("ERROR: AddressSanitizer: heap-buffer-overflow on address 0x1234", file=sys.stderr)
    print("    #0 0x1111 in target_func /tmp/source.c:42", file=sys.stderr)
    print("    #1 0x2222 in main /tmp/main.c:7", file=sys.stderr)
    sys.exit(1)
if count > 10:
    print("ERROR: AddressSanitizer: stack-buffer-overflow on address 0x5678", file=sys.stderr)
    print("    #0 0x3333 in other_func /tmp/source.c:99", file=sys.stderr)
    sys.exit(1)
sys.exit(0)
"""


class SeedMinimizationTests(unittest.TestCase):
    def test_reducer_removes_irrelevant_records_and_attributes_critical_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            target = tmp_path / "fake_target.py"
            target.write_text(FAKE_TARGET, encoding="utf-8")
            target.chmod(0o755)
            seed = tmp_path / "input.seed"
            seed.write_text(
                textwrap.dedent(
                    """\
                    MODE,STRING,CRASH
                    COUNT,INT,64
                    JUNK,INT,123
                    BUF,POINTER,0x00ff1122
                    JUNK,INT,123
                    """
                ),
                encoding="utf-8",
            )
            output = tmp_path / "minimized.seed"
            metadata = tmp_path / "minimized.json"

            result = minimize_seed(
                target=target,
                seed=seed,
                output=output,
                timeout=1.0,
                baseline_status="crash",
                metadata=metadata,
            )

            minimized = output.read_text(encoding="utf-8")
            self.assertNotIn("JUNK", minimized)
            self.assertIn("MODE,STRING,C\n", minimized)
            self.assertIn("COUNT,INT,16\n", minimized)
            self.assertIn("BUF,POINTER,0xff\n", minimized)
            self.assertEqual(result.original_status, "crash")
            self.assertEqual(result.minimized_status, "crash")
            self.assertEqual(
                sorted(item["name"] for item in result.removed_records),
                ["JUNK", "JUNK"],
            )
            self.assertEqual(set(result.critical_fields), {"MODE", "COUNT", "BUF"})
            self.assertEqual(result.irrelevant_fields, [])
            self.assertTrue(result.signature_match)
            self.assertTrue(metadata.exists())

    def test_reducer_preserves_sanitizer_and_stack_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            target = tmp_path / "sanitizer_target.py"
            target.write_text(SANITIZER_TARGET, encoding="utf-8")
            target.chmod(0o755)
            seed = tmp_path / "input.seed"
            seed.write_text(
                textwrap.dedent(
                    """\
                    MODE,STRING,CRASH
                    COUNT,INT,64
                    """
                ),
                encoding="utf-8",
            )
            output = tmp_path / "minimized.seed"

            result = minimize_seed(
                target=target,
                seed=seed,
                output=output,
                timeout=1.0,
                baseline_status="nonzero",
            )

            minimized = output.read_text(encoding="utf-8")
            self.assertIn("MODE,STRING,C\n", minimized)
            self.assertNotIn("MODE,STRING,\n", minimized)
            self.assertTrue(result.signature_match)
            self.assertEqual(
                result.original_signature["sanitizer_kind"],
                "heap-buffer-overflow",
            )
            self.assertEqual(
                result.minimized_signature["sanitizer_kind"],
                "heap-buffer-overflow",
            )
            self.assertEqual(
                result.original_signature["stack_hash"],
                result.minimized_signature["stack_hash"],
            )


if __name__ == "__main__":
    unittest.main()
