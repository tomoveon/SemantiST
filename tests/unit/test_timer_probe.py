import unittest

from fuzzer.cli.timer_probe import compare_rows


class TimerProbeTests(unittest.TestCase):
    def test_compare_rows_reports_trailing_trace(self):
        left = [{"n": "1", "q0": "0"}]
        right = [{"n": "1", "q0": "0"}, {"n": "2", "q0": "1"}]
        self.assertEqual(compare_rows(left, right), [{"scan": 2, "missing": "rusty_gt"}])

    def test_compare_rows_keeps_field_differences(self):
        left = [{"n": "1", "q0": "0"}]
        right = [{"n": "1", "q0": "1"}]
        self.assertEqual(compare_rows(left, right), [{"scan": 1, "q0": ("0", "1")}])


if __name__ == "__main__":
    unittest.main()
