import unittest

from compiler.harness.abi import Param, TargetAbi, TypeSpec, scalar_spec
from compiler.harness.semantist_adapter import testcase_to_raw as encode_raw_frame
from fuzzer.pipeline.model import _SpecView
from fuzzer.corpus.frames import (
    FrameFormatError,
    build_testcase,
    canonicalize,
    fnv1a64,
    parse_testcase,
    parse_time_literal,
    structured_default_for_spec,
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

    def test_raw_bool_storage_byte_and_legacy_hex_integer(self):
        target = abi()
        case = parse_testcase("0.Enable,BOOL,0X03\n0.Value,INT,0x0100\n", target)
        self.assertEqual(case.frames[0].values, {"Enable": "0x03", "Value": "0x0100"})
        self.assertIn("0.Enable,BOOL,0x03", case.canonical())
        raw = encode_raw_frame(case, target)
        self.assertEqual(raw[0], 3)
        self.assertEqual(raw[2:4], b"\x01\x00")
        with self.assertRaises(FrameFormatError):
            parse_testcase("0.Value,INT,0x1\n", target)

    def test_string_hex_uses_decoded_capacity_and_zero_terminator(self):
        field = Param("Text", TypeSpec("string", "STRING", c_type="char", size=1, length=3),
                      "VAR_INPUT", offset=8, index=1)
        target = TargetAbi("FUNCTION", "F", [field], state_fields=[field], instance_size=16)
        case = parse_testcase("0.Text,STRING,0x80ff41\n", target)
        self.assertEqual(encode_raw_frame(case, target)[:4], b"\x80\xffA\x00")
        case = parse_testcase("0.Text,STRING,0x80ff4142\n", target)
        self.assertEqual(encode_raw_frame(case, target)[:4], b"\x80\xffAB")
        for value in ("0x80ff414243", "0xabc", "0xzz"):
            with self.subTest(value=value), self.assertRaises(FrameFormatError):
                parse_testcase(f"0.Text,STRING,{value}\n", target)

    def test_wstring_hex_counts_two_byte_units(self):
        field = Param("Wide", TypeSpec("string", "WSTRING", c_type="uint16_t", size=2, length=2),
                      "VAR_INPUT", offset=8, index=1)
        target = TargetAbi("FUNCTION", "F", [field], state_fields=[field], instance_size=16)
        case = parse_testcase("0.Wide,WSTRING,0x00d841dc\n", target)
        self.assertEqual(encode_raw_frame(case, target)[:6], b"\x00\xd8\x41\xdc\x00\x00")
        case = parse_testcase("0.Wide,WSTRING,0x00d841dc0000\n", target)
        self.assertEqual(encode_raw_frame(case, target)[:6], b"\x00\xd8\x41\xdc\x00\x00")
        with self.assertRaises(FrameFormatError):
            parse_testcase("0.Wide,WSTRING,0x00d841dc00000000\n", target)

    def test_structured_defaults_prime_protocol_controls_without_truncation(self):
        spec = {
            "kind": "opaque", "name": "XML_CONTROL", "byte_size": 24,
            "members": [
                {"name": "COMMAND", "offset": 0, "size": 2,
                 "spec": {"kind": "scalar", "name": "WORD", "size": 2}},
                {"name": "START_POS", "offset": 2, "size": 2,
                 "spec": {"kind": "scalar", "name": "UINT", "size": 2}},
                {"name": "STOP_POS", "offset": 4, "size": 2,
                 "spec": {"kind": "scalar", "name": "UINT", "size": 2}},
            ],
        }
        value = structured_default_for_spec("CTRL", spec)
        self.assertEqual(bytes.fromhex(value[2:])[:6], b"\x01\x80\x00\x00\x03\x00")
        self.assertEqual(len(bytes.fromhex(value[2:])), 24)

    def test_structured_defaults_seed_network_read_and_short_buffer_shape(self):
        short = {
            "kind": "opaque", "name": "NETWORK_BUFFER_SHORT", "byte_size": 10,
            "members": [
                {"name": "SIZE", "offset": 0, "size": 2,
                 "spec": {"kind": "scalar", "name": "UINT", "size": 2}},
                {"name": "BUFFER", "offset": 2, "size": 8,
                 "spec": {"kind": "array", "name": "ARRAY OF BYTE", "byte_size": 8}},
            ],
        }
        value = bytes.fromhex(structured_default_for_spec("R_BUF", short)[2:])
        self.assertEqual(value[:2], b"\x05\x00")
        self.assertEqual(value[2:7], b"A,A\r\n")

        nested = {
            "kind": "opaque", "name": "NET_VAR_DATA", "byte_size": 16,
            "members": [{"name": "R_BUF", "offset": 0, "size": 16,
                         "spec": {"kind": "opaque", "name": "NETWORK_BUFFER", "byte_size": 16,
                                  "members": [
                                      {"name": "SIZE", "offset": 0, "size": 2,
                                       "spec": {"kind": "scalar", "name": "UINT", "size": 2}},
                                      {"name": "BUFFER", "offset": 2, "size": 14,
                                       "spec": {"kind": "array", "name": "ARRAY OF BYTE", "byte_size": 14}},
                                  ]}}],
        }
        nested_bytes = bytes.fromhex(structured_default_for_spec("X", nested)[2:])
        self.assertEqual(nested_bytes[:5], b"\x41\x00\x01\x20\x41")

        net = {
            "kind": "opaque", "name": "NET_VAR_DATA", "byte_size": 8,
            "members": [
                {"name": "STATE", "offset": 0, "size": 1,
                 "spec": {"kind": "scalar", "name": "BYTE", "size": 1}},
                {"name": "ERROR_ID", "offset": 1, "size": 1,
                 "spec": {"kind": "scalar", "name": "BYTE", "size": 1}},
                {"name": "BUF_SIZE", "offset": 2, "size": 2,
                 "spec": {"kind": "scalar", "name": "UINT", "size": 2}},
            ],
        }
        net_bytes = bytes.fromhex(structured_default_for_spec("X", net)[2:])
        self.assertEqual(net_bytes[:4], b"\x02\x00\x80\x00")

    def test_pointer_backing_bytes_accept_planner_spec_view(self):
        pointer = Param("P", _SpecView({"kind": "pointer", "name": "POINTER", "size": 8, "byte_size": 8}),
                        "VAR_INPUT", offset=8, index=1)
        target = TargetAbi("FUNCTION_BLOCK", "FB", [pointer], state_fields=[pointer], instance_size=16)
        self.assertIn(b"0.P,POINTER,0x" + b"ab" * 64,
                      build_testcase(target, [{"P": "0x" + "ab" * 64}]))
        with self.assertRaises(FrameFormatError):
            build_testcase(target, [{"P": "0x" + "ab" * 4097}])


if __name__ == "__main__":
    unittest.main()
