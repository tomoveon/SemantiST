"""Comparison logic of tests/integration/execution_consistency.py."""
import importlib.util
from pathlib import Path
import random
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "execution_consistency", ROOT / "tests" / "integration" / "execution_consistency.py"
)
checker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = checker  # dataclasses resolve their module by name
SPEC.loader.exec_module(checker)


def row(name: str, exit_kind: str = "ok", edges=None) -> dict:
    return {
        "input": name, "exit_kind": exit_kind, "edges": edges or [[5, 1]], "semantic": [],
        "telemetry": {"scan_count": 1, "event_count": 0, "state_count": 0, "flags": 4, "events": [], "states": []},
    }


class ExecutionConsistencyTests(unittest.TestCase):
    def test_failing_inputs_consume_no_output_line(self):
        records = checker.records_from_dump([row("a"), row("b", "crash"), row("c")], ["A|", "C|"])
        self.assertEqual([record.output for record in records], ["A|", None, "C|"])

    def test_every_observed_channel_is_compared(self):
        reference = checker.records_from_dump([row("a")], ["01|"])[0]
        same = checker.records_from_dump([row("a")], ["01|"])[0]
        self.assertEqual(checker.compare(reference, same), [])
        other = checker.records_from_dump([row("a", edges=[[5, 2]])], ["02|"])[0]
        self.assertEqual(checker.compare(reference, other), ["edges", "outputs"])
        crashed = checker.records_from_dump([row("a", "crash")], [])[0]
        self.assertEqual(checker.compare(reference, crashed), ["exit_kind", "outputs"])

    def test_generated_batch_is_reproducible_and_reaches_the_cycle_bound(self):
        base = "LIMIT,INT,0\n0.EN,BOOL,0\n0.X,REAL,0.0\n1.EN,BOOL,1\n1.X,REAL,1.0\n"
        first = checker.generated_inputs(base, 10, random.Random("seed"))
        again = checker.generated_inputs(base, 10, random.Random("seed"))
        self.assertEqual(first, again)
        cycles = [max(int(line.split(".", 1)[0]) for line in text.decode().splitlines() if "." in line.split(",")[0])
                  for text in first]
        self.assertEqual(cycles[0], 255)
        self.assertTrue(all(text.decode().startswith("LIMIT,INT,") for text in first))

    def test_function_inputs_keep_their_shared_records(self):
        inputs = checker.generated_inputs("X,DINT,0\nD,DINT,1\n", 3, random.Random(1))
        for text in inputs:
            names = [line.split(",")[0] for line in text.decode().splitlines()]
            self.assertEqual(names, ["X", "D"])


if __name__ == "__main__":
    unittest.main()
