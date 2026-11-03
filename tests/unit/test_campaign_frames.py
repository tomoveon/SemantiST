import unittest

from compiler.harness.abi import Param, TargetAbi, TypeSpec, scalar_spec
from fuzzer.corpus.frames import (
    FrameFormatError,
    build_testcase,
    canonicalize,
    fnv1a64,
    parse_testcase,
    parse_time_literal,
)


def abi():
    return TargetAbi(
        "FUNCTION_BLOCK",
        "FB",
        [
            Param("Enable", scalar_spec("BOOL"), "VAR_INPUT", offset=8, index=1),
            Param("Value", scalar_spec("INT"), "VAR_INPUT", offset=10, index=2),
        ],
        state_fields=[
            Param("Enable", scalar_spec("BOOL"), "VAR_INPUT", offset=8, index=1),
            Param("Value", scalar_spec("INT"), "VAR_INPUT", offset=10, index=2),
        ],
        instance_size=24,
    )


class FrameTests(unittest.TestCase):
    def test_canonical_expands_omitted_fields(self):
        text = "0.Enable,BOOL,1\n1.Value,INT,7\n"
        case = parse_testcase(text, abi())
        self.assertEqual([frame.values for frame in case.frames], [
            {"Enable": "1", "Value": "0"},
            {"Enable": "1", "Value": "7"},
        ])
        canonical = canonicalize(text, abi()).decode()
        self.assertIn("1.Enable,BOOL,1\n1.Value,INT,7\n", canonical)

    def test_non_consecutive_frames_are_rejected(self):
        with self.assertRaises(FrameFormatError):
            parse_testcase("0.Enable,BOOL,1\n2.Value,INT,1\n", abi())

    def test_type_token_must_match_abi(self):
        with self.assertRaises(FrameFormatError):
            parse_testcase("0.Enable,INT,1\n", abi())

    def test_empty_type_token_is_accepted_for_rust_canonical_input(self):
        case = parse_testcase("0.Enable,,1\n0.Value,,7\n", abi())
        self.assertEqual(case.frames[0].values, {"Enable": "1", "Value": "7"})

    def test_time_literal(self):
        self.assertEqual(parse_time_literal("T#500ms"), 500_000_000)
        self.assertEqual(parse_time_literal("T#1s"), 1_000_000_000)

    def test_hash_is_fnv1a64(self):
        self.assertEqual(fnv1a64(b""), 14695981039346656037)
        self.assertEqual(fnv1a64(b"a"), 0xAF63DC4C8601EC8C)

    def test_prefix_hashes_chain(self):
        case = parse_testcase("0.Enable,BOOL,1\n1.Value,INT,7\n", abi())
        hashes = case.prefix_hashes()
        self.assertEqual(len(hashes), 2)
        self.assertNotEqual(hashes[0], hashes[1])

    def test_none_omits_a_field_for_in_out_overrides(self):
        data = build_testcase(abi(), [{"Enable": "1", "Value": None}]).decode()
        self.assertIn("0.Enable,BOOL,1", data)
        self.assertNotIn("0.Value", data)

    def test_build_writes_every_field(self):
        data = build_testcase(abi(), [{"Enable": "1"}, {"Value": "9"}]).decode()
        self.assertIn("0.Value,INT,0", data)
        self.assertIn("1.Enable,BOOL,1", data)

    def test_inout_only_target_keeps_shared_initial_value_and_per_frame_dt(self):
        inout = Param("Counter", scalar_spec("UINT"), "VAR_IN_OUT", offset=8, index=1)
        target = TargetAbi("FUNCTION_BLOCK", "FB", [inout], state_fields=[inout], instance_size=16)
        data = build_testcase(target, [{"Counter": "65535"}, {"Counter": "0"}],
                              shared={"Counter": "7"}, frame_dt_ns=[10, 25], include_dt=True)
        self.assertIn(b"Counter,UINT,7\n", data)
        self.assertIn(b"0.Counter,UINT,65535\n", data)
        self.assertIn(b"1.Counter,UINT,0\n", data)
        parsed = parse_testcase(data, target)
        self.assertEqual([frame.values["Counter"] for frame in parsed.frames], ["65535", "0"])
        self.assertEqual([frame.dt_ns for frame in parsed.frames], [10, 25])

    def test_unsigned_values_do_not_wrap_at_python_frame_boundary(self):
        for name, accepted, rejected in (
            ("UINT", "65535", ("-1", "65536")),
            ("ULINT", str((1 << 64) - 1), ("-1", str(1 << 64))),
        ):
            with self.subTest(name=name):
                field = Param("U", scalar_spec(name), "VAR_INPUT", offset=8, index=1)
                target = TargetAbi("FUNCTION_BLOCK", "FB", [field], state_fields=[field], instance_size=16)
                self.assertEqual(parse_testcase(f"0.U,{name},{accepted}\n", target).frames[0].values["U"], accepted)
                for value in rejected:
                    with self.assertRaises(FrameFormatError):
                        parse_testcase(f"0.U,{name},{value}\n", target)
                    with self.assertRaises(FrameFormatError):
                        build_testcase(target, [{"U": value}])

    def test_unsigned_type_name_is_authoritative_when_c_type_is_missing(self):
        field = Param("U", scalar_spec("UINT"), "VAR_INPUT", offset=8, index=1)
        field.spec.c_type = None
        target = TargetAbi("FUNCTION_BLOCK", "FB", [field], state_fields=[field], instance_size=16)
        self.assertEqual(parse_testcase("0.U,UINT,65535\n", target).frames[0].values["U"], "65535")
        with self.assertRaises(FrameFormatError):
            parse_testcase("0.U,UINT,-1\n", target)

    def test_string_capacity_is_shared_by_parse_and_build(self):
        field = Param("Text", TypeSpec("string", "STRING", c_type="char", size=1, length=3),
                      "VAR_INPUT", offset=8, index=1)
        target = TargetAbi("FUNCTION", "F", [field], state_fields=[field], instance_size=16)
        self.assertEqual(parse_testcase("0.Text,STRING,ABC\n", target).frames[0].values["Text"], "ABC")
        with self.assertRaises(FrameFormatError):
            parse_testcase("0.Text,STRING,ABCD\n", target)
        with self.assertRaises(FrameFormatError):
            build_testcase(target, [{"Text": "ABCD"}])

    def test_wstring_capacity_counts_utf16_code_units(self):
        field = Param("Wide", TypeSpec("string", "WSTRING", c_type="uint16_t", size=2, length=2),
                      "VAR_INPUT", offset=8, index=1)
        target = TargetAbi("FUNCTION", "F", [field], state_fields=[field], instance_size=16)
        self.assertEqual(parse_testcase("0.Wide,WSTRING,AB\n", target).frames[0].values["Wide"], "AB")
        with self.assertRaises(FrameFormatError):
            build_testcase(target, [{"Wide": "\U0001f600A"}])


if __name__ == "__main__":
    unittest.main()
