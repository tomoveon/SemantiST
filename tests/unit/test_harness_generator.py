import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from compiler.harness.generator import generate_harness  # noqa: E402
from compiler.parser.st import parse_target  # noqa: E402


class HarnessGeneratorTests(unittest.TestCase):
    def test_c_keyword_parameter_is_escaped_but_seed_name_is_preserved(self) -> None:
        source = """
FUNCTION KEYWORD_INPUT : REAL
VAR_INPUT
    default : REAL;
END_VAR
KEYWORD_INPUT := default;
END_FUNCTION
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keyword.st"
            path.write_text(source, encoding="utf-8")
            target = parse_target(str(path), "KEYWORD_INPUT")

        harness = generate_harness(
            target.name,
            target.ret_spec,
            target.params,
            target.total_size,
        )

        self.assertIn("float semantist_param_default = 0;", harness)
        self.assertIn('strcmp(name, "default")', harness)
        self.assertIn("KEYWORD_INPUT(semantist_param_default)", harness)
        self.assertNotIn("float default = 0;", harness)

    def test_date_input_is_generated_as_scalar_not_pointer(self) -> None:
        source = """
FUNCTION DATE_INPUT : INT
VAR_INPUT
    IDATE : DATE;
END_VAR
DATE_INPUT := 0;
END_FUNCTION
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "date_input.st"
            path.write_text(source, encoding="utf-8")
            target = parse_target(str(path), "DATE_INPUT")

        harness = generate_harness(
            target.name,
            target.ret_spec,
            target.params,
            target.total_size,
        )

        self.assertIn("extern int16_t DATE_INPUT(int64_t);", harness)
        self.assertIn("int64_t semantist_param_IDATE = 0;", harness)
        self.assertIn("semantist_param_IDATE = (int64_t)parse_i64(value);", harness)
        self.assertIn("DATE_INPUT(semantist_param_IDATE)", harness)
        self.assertNotIn("fill_pointer_value(&semantist_param_IDATE_storage", harness)


if __name__ == "__main__":
    unittest.main()
