import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from compiler.parser.st import DEFAULT_STRING_LENGTH, StParseError, parse_target, parse_type  # noqa: E402


class StructuredTextParserRegressionTests(unittest.TestCase):
    def test_iec_elementary_time_and_character_types_are_scalar(self):
        expected = {
            "DATE": "int64_t",
            "LDATE": "int64_t",
            "DT": "int64_t",
            "DATE_AND_TIME": "int64_t",
            "LDT": "int64_t",
            "LDATE_AND_TIME": "int64_t",
            "TOD": "int64_t",
            "TIME_OF_DAY": "int64_t",
            "LTOD": "int64_t",
            "LTIME_OF_DAY": "int64_t",
            "TIME": "int64_t",
            "LTIME": "int64_t",
            "WCHAR": "uint16_t",
        }
        for type_name, c_type in expected.items():
            with self.subTest(type_name=type_name):
                spec = parse_type(type_name)
                self.assertEqual(spec.kind, "scalar")
                self.assertEqual(spec.name, type_name)
                self.assertEqual(spec.c_type, c_type)

    def supported_targets(self):
        manifest = json.loads((ROOT / "benchmarks/oscat_basic/manifest.json").read_text())
        yield from manifest["targets"]

    def parsed_supported_targets(self):
        for target in self.supported_targets():
            st_file = ROOT / "benchmarks/oscat_basic" / target["st_file"]
            yield target, parse_target(str(st_file), target["function"])

    def test_supported_targets_have_expected_kinds_and_input_blocks(self):
        parsed = {target["id"]: result for target, result in self.parsed_supported_targets()}

        self.assertEqual(parsed["inc2"].kind, "FUNCTION")
        self.assertEqual(parsed["inc2"].ret_spec.name, "INT")
        self.assertIsNone(parsed["inc2"].outputs)
        self.assertIsNone(parsed["inc2"].state_fields)
        self.assertEqual(
            [(param.name, param.block_kind, param.constant, param.spec.name) for param in parsed["inc2"].params],
            [
                ("X", "VAR_INPUT", False, "INT"),
                ("D", "VAR_INPUT", False, "INT"),
                ("L", "VAR_INPUT", False, "INT"),
                ("U", "VAR_INPUT", False, "INT"),
            ],
        )
        self.assertEqual([param.name for param in parsed["inc2"].ordinary_inputs or []], ["X", "D", "L", "U"])
        self.assertEqual(parsed["inc2"].constant_inputs, [])
        self.assertEqual(parsed["inc2"].inout_fields, [])

        self.assertEqual(
            [param.name for param in parsed["list_get"].params],
            ["SEP", "POS", "LIST"],
        )
        self.assertEqual([param.name for param in parsed["list_get"].inout_fields], ["LIST"])

        string_params = {param.name: param for param in parsed["string_to_buffer"].params}
        self.assertEqual(string_params["STR"].spec.kind, "string")
        self.assertEqual(string_params["STR"].spec.length, DEFAULT_STRING_LENGTH)
        self.assertEqual(string_params["PT"].spec.kind, "pointer")
        self.assertEqual(string_params["PT"].spec.element.kind, "array")
        self.assertEqual(string_params["PT"].spec.element.count, 32768)

        self.assertEqual(parsed["list_next"].kind, "FUNCTION_BLOCK")
        list_params = {param.name: param for param in parsed["list_next"].params}
        self.assertEqual(list_params["LIST"].block_kind, "VAR_IN_OUT")
        self.assertEqual(list_params["LIST"].spec.kind, "string")

        scheduler_params = {param.name: param for param in parsed["scheduler_2"].params}
        for name in ("C0", "C1", "C2", "C3", "O0", "O1", "O2", "O3"):
            self.assertEqual(scheduler_params[name].block_kind, "VAR_INPUT")
            self.assertTrue(scheduler_params[name].constant)
            self.assertEqual(scheduler_params[name].spec.name, "UINT")

    def test_supported_function_block_state_fields_include_modifiers_and_constants(self):
        parsed = {target["id"]: result for target, result in self.parsed_supported_targets()}

        calibrate_state = {param.name: param for param in parsed["calibrate"].state_fields}
        self.assertEqual(calibrate_state["offset"].block_kind, "VAR_RETAIN")
        self.assertFalse(calibrate_state["offset"].constant)
        self.assertEqual(calibrate_state["Scale"].spec.name, "REAL")

        fifo_state = {param.name: param for param in parsed["fifo_16"].state_fields}
        self.assertEqual(fifo_state["n"].block_kind, "VAR")
        self.assertTrue(fifo_state["n"].constant)
        self.assertEqual(fifo_state["fifo"].spec.kind, "array")
        self.assertEqual(fifo_state["fifo"].spec.count, 17)
        self.assertEqual(fifo_state["fifo"].spec.element.name, "DWORD")

    def test_complex_codesys_twincat_declarations(self):
        source = """
FUNCTION_BLOCK FB_COMPLEX
VAR_INPUT
    IN1 : INT;
    ENABLE : BOOL;
END_VAR
VAR_INPUT (* CONSTANT *)
    A, B : STRING(8) := 'seed';
    C : ARRAY[lo..hi] OF POINTER TO BYTE := [0, 1, 2];
END_VAR
VAR_IN_OUT
    IO : REF_TO ARRAY[-1..1] OF INT;
END_VAR
VAR_OUTPUT
    OUT1, OUT2 : WSTRING[4];
END_VAR
VAR
    STATE : DINT;
END_VAR
VAR RETAIN
    RET : REAL;
END_VAR
VAR PERSISTENT
    PERSIST : DINT;
END_VAR
VAR_TEMP
    TEMP : ARRAY[1..2, 0..1] OF BOOL := [TRUE, FALSE, TRUE, FALSE];
END_VAR
VAR CONSTANT
    lo : INT := 1;
    hi : INT := 3;
END_VAR
END_FUNCTION_BLOCK
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "complex.st"
            path.write_text(source, encoding="utf-8")
            target = parse_target(str(path), "FB_COMPLEX")

        params = {param.name: param for param in target.params}
        state = {param.name: param for param in target.state_fields}
        outputs = {param.name: param for param in target.outputs}

        self.assertEqual(target.kind, "FUNCTION_BLOCK")
        self.assertEqual([param.name for param in target.ordinary_inputs], ["IN1", "ENABLE"])
        self.assertEqual([param.name for param in target.constant_inputs], ["A", "B", "C"])
        self.assertEqual([param.name for param in target.inout_fields], ["IO"])
        self.assertEqual([param.name for param in target.outputs], ["OUT1", "OUT2"])
        self.assertEqual([param.name for param in target.persistent_fields], ["STATE", "RET", "PERSIST"])
        self.assertEqual([param.name for param in target.temp_fields], ["TEMP"])
        self.assertEqual([param.name for param in target.constant_fields], ["lo", "hi"])
        self.assertEqual([param.name for param in target.snapshot_fields], ["IO", "OUT1", "OUT2", "STATE", "RET", "PERSIST"])
        self.assertNotIn("TEMP", [param.name for param in target.persistent_fields])
        self.assertNotIn("TEMP", [param.name for param in target.snapshot_fields])
        self.assertNotIn("lo", [param.name for param in target.snapshot_fields])
        self.assertEqual(params["IN1"].block_kind, "VAR_INPUT")
        self.assertFalse(params["IN1"].constant)
        for name in ("A", "B"):
            self.assertEqual(params[name].block_kind, "VAR_INPUT")
            self.assertTrue(params[name].constant)
            self.assertEqual(params[name].spec.kind, "string")
            self.assertEqual(params[name].spec.length, 8)

        self.assertEqual(params["C"].spec.kind, "array")
        self.assertEqual(params["C"].spec.count, 3)
        self.assertEqual(params["C"].spec.element.kind, "pointer")
        self.assertEqual(params["C"].spec.element.element.name, "BYTE")

        self.assertEqual(params["IO"].block_kind, "VAR_IN_OUT")
        self.assertEqual(params["IO"].spec.kind, "pointer")
        self.assertEqual(params["IO"].spec.element.kind, "array")
        self.assertEqual(params["IO"].spec.element.count, 3)

        self.assertEqual(outputs["OUT1"].spec.kind, "string")
        self.assertEqual(outputs["OUT1"].spec.c_type, "uint16_t")
        self.assertEqual(outputs["OUT2"].spec.length, 4)
        self.assertEqual(state["STATE"].block_kind, "VAR")
        self.assertEqual(state["RET"].block_kind, "VAR_RETAIN")
        self.assertEqual(state["PERSIST"].block_kind, "VAR_PERSISTENT")
        self.assertEqual(state["TEMP"].block_kind, "VAR_TEMP")
        self.assertEqual(state["TEMP"].spec.count, 4)
        self.assertTrue(state["lo"].constant)
        self.assertTrue(state["hi"].constant)

    def test_parse_errors_include_target_path_and_nearby_declaration(self):
        source = """
FUNCTION_BLOCK BROKEN
VAR_INPUT
    VALUE : ARRAY[0..3 OF BYTE;
END_VAR
END_FUNCTION_BLOCK
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.st"
            path.write_text(source, encoding="utf-8")
            with self.assertRaises(StParseError) as raised:
                parse_target(str(path), "BROKEN")

        message = str(raised.exception)
        self.assertIn("BROKEN", message)
        self.assertIn("broken.st", message)
        self.assertIn("VALUE", message)
        self.assertIn("near", message)


if __name__ == "__main__":
    unittest.main()
