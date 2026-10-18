"""Harness ABI from compiler outputs: synthetic RuSTy IR plus STG models."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from compiler.harness.abi import AbiError, target_abi  # noqa: E402
from compiler.harness.generator import generate_harness, generate_seed, main  # noqa: E402


def symbol(name, type_name, role, offset, *, by_reference=False, constant=False, configuration_source=None):
    return {
        "name": name,
        "type_name": type_name,
        "role": role,
        "by_reference": by_reference,
        "constant": constant,
        "configuration_source": configuration_source,
        "source": {"start_offset": offset},
    }


def iec_type(name, kind, **fields):
    value = {
        "name": name,
        "kind": kind,
        "bit_width": None,
        "signed": None,
        "element_type": None,
        "dimensions": [],
        "string_encoding": None,
        "string_capacity": None,
        "pointer_target": None,
    }
    value.update(fields)
    return value


BASE_TYPES = [
    iec_type("REAL", "float", bit_width=32),
    iec_type("LREAL", "float", bit_width=64),
    iec_type("INT", "integer", bit_width=16, signed=True),
    iec_type("BYTE", "integer", bit_width=8, signed=False),
    iec_type("DATE", "integer", bit_width=64, signed=True),
    iec_type("STRING", "string", bit_width=648, string_encoding="utf8", string_capacity=81),
]


def model(pou, kind, symbols, types=()):
    return {
        "pous": [
            {
                "pou": {"name": pou, "kind": kind},
                "environment": {"symbols": symbols, "types": [*BASE_TYPES, *types]},
            }
        ]
    }


# FUNCTION_BLOCK PID with a nested FB instance, an array whose bound is a
# global constant (STG reports it resolved), a VAR_IN_OUT array and a temp.
FB_IR = """
%PID = type { ptr, float, i16, %INNER, [4 x double], ptr }
%INNER = type { ptr, float, i8 }

define void @PID(ptr %0) {
entry:
  %this = alloca ptr, align 8
  store ptr %0, ptr %this, align 8
  %__vtable = getelementptr inbounds nuw %PID, ptr %0, i32 0, i32 0
  %SET = getelementptr inbounds nuw %PID, ptr %0, i32 0, i32 1
  %MODE = getelementptr inbounds nuw %PID, ptr %0, i32 0, i32 2
  %inner = getelementptr inbounds nuw %PID, ptr %0, i32 0, i32 3
  %hist = getelementptr inbounds nuw %PID, ptr %0, i32 0, i32 4
  %buf = getelementptr inbounds nuw %PID, ptr %0, i32 0, i32 5
  %tmp = alloca float, align 4
  br label %body

body:
  ret void
}
"""


def fb_model(mode_type="INT"):
    return model(
        "PID",
        "function_block",
        [
            symbol("SET", "REAL", "input", 10),
            symbol("MODE", mode_type, "input", 20),
            symbol("buf", "__auto_pointer_to___PID_buf", "in_out", 30, by_reference=True),
            symbol("inner", "INNER", "persistent_state", 40),
            symbol("hist", "__PID_hist", "persistent_state", 50),
            symbol("tmp", "REAL", "temporary", 60),
        ],
        [
            iec_type("DINT", "integer", bit_width=32, signed=True),
            iec_type("INNER", "struct", bit_width=72),
            iec_type("__PID_hist", "array", element_type="LREAL", dimensions=[{"lower": 0, "upper": 3}]),
            iec_type("__PID_buf", "array", element_type="LREAL", dimensions=[{"lower": 1, "upper": 3}]),
            iec_type("__auto_pointer_to___PID_buf", "pointer", pointer_target="__PID_buf"),
        ],
    )


FUNCTION_IR = """
define void @FMT(ptr %0, ptr %1, i16 %2, ptr %3) {
entry:
  %FMT = alloca ptr, align 8
  store ptr %0, ptr %FMT, align 8
  %S = alloca [81 x i8], align 1
  call void @llvm.memcpy.p0.p0.i64(ptr align 1 %S, ptr align 1 %1, i64 80, i1 false)
  %N = alloca i16, align 2
  store i16 %2, ptr %N, align 2
  %PT = alloca ptr, align 8
  store ptr %3, ptr %PT, align 8
  ret void
}
"""

FUNCTION_MODEL = model(
    "FMT",
    "function",
    [
        symbol("FMT", "STRING", "return", 0),
        symbol("S", "STRING", "input", 10),
        symbol("N", "INT", "input", 20),
        symbol("PT", "__FMT_PT", "input", 30),
    ],
    [
        iec_type("__FMT_PT", "pointer", pointer_target="__FMT_PT_"),
        iec_type("__FMT_PT_", "array", element_type="BYTE", dimensions=[{"lower": 0, "upper": 15}]),
    ],
)

SCALAR_IR = """
define i16 @PICK(float %0, i64 %1) {
entry:
  %PICK = alloca i16, align 2
  %default = alloca float, align 4
  store float %0, ptr %default, align 4
  %IDATE = alloca i64, align 8
  store i64 %1, ptr %IDATE, align 8
  ret i16 0
}
"""

SCALAR_MODEL = model(
    "PICK",
    "function",
    [
        symbol("PICK", "INT", "return", 0),
        symbol("default", "REAL", "input", 10),
        symbol("IDATE", "DATE", "input", 20),
    ],
)


class FunctionBlockAbiTests(unittest.TestCase):
    def test_layout_comes_from_ir_and_types_from_stg(self):
        abi = target_abi(fb_model(), FB_IR, "PID")
        self.assertEqual(abi.instance_size, 72)
        fields = {param.name: param for param in abi.state_fields}
        self.assertNotIn("tmp", fields, "VAR_TEMP is not an instance member")
        self.assertEqual([(name, fields[name].offset) for name in ("SET", "MODE", "inner", "hist", "buf")],
                         [("SET", 8), ("MODE", 12), ("inner", 16), ("hist", 32), ("buf", 64)])
        self.assertEqual((fields["inner"].spec.kind, fields["inner"].spec.size), ("opaque", 16))
        self.assertEqual(fields["hist"].spec.count, 4)
        self.assertEqual((fields["buf"].block_kind, fields["buf"].spec.count), ("VAR_IN_OUT", 3))
        self.assertEqual([param.name for param in abi.params], ["SET", "MODE", "buf"])

    def test_type_disagreement_with_ir_is_an_error(self):
        with self.assertRaisesRegex(AbiError, "MODE"):
            target_abi(fb_model(mode_type="DINT"), FB_IR, "PID")

    def test_every_struct_field_needs_a_member_gep(self):
        partial = FB_IR.replace("  %hist = getelementptr inbounds nuw %PID, ptr %0, i32 0, i32 4\n", "")
        with self.assertRaisesRegex(AbiError, "cover every field"):
            target_abi(fb_model(), partial, "PID")

    def test_harness_asserts_offsets_and_seed_lists_cycle_inputs(self):
        abi = target_abi(fb_model(), FB_IR, "PID")
        harness = generate_harness(abi)
        self.assertIn('_Static_assert(sizeof(semantist_fb_state_t) == 72, "PID instance size");', harness)
        self.assertIn('_Static_assert(offsetof(semantist_fb_state_t, f2) == 16, "PID.inner offset");', harness)
        self.assertIn("uint8_t f2[16] __attribute__((aligned(8)));", harness)
        self.assertIn("double f4_storage[3];", harness)
        self.assertEqual(
            generate_seed(abi),
            "buf,ARRAY,0x00\n0.SET,REAL,0.0\n0.MODE,INT,0\n1.SET,REAL,1.0\n1.MODE,INT,0\n",
        )

    @unittest.skipUnless(shutil.which("cc"), "requires a C compiler")
    def test_generated_harness_writes_the_compiled_offsets(self):
        harness = generate_harness(target_abi(fb_model(), FB_IR, "PID"))
        harness += """
void PID__ctor(semantist_fb_state_t *p) { (void)p; }
void PID(semantist_fb_state_t *p) {
    unsigned char *raw = (unsigned char *)p;
    double *buf = *(double **)(raw + 64);
    printf("%g %d %g\\n", *(float *)(raw + 8), *(int16_t *)(raw + 12), buf[2]);
}
"""
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / "harness.c").write_text(harness, encoding="utf-8")
            subprocess.run(["cc", str(directory / "harness.c"), "-o", str(directory / "pid")], check=True)
            seed = "buf,ARRAY,0x" + "00" * 16 + "0000000000000840\n0.SET,REAL,1.5\n0.MODE,INT,7\n"
            run = subprocess.run([str(directory / "pid")], input=seed.encode(), capture_output=True, check=True)
        self.assertEqual(run.stdout.decode(), "1.5 7 3\n")


class FunctionAbiTests(unittest.TestCase):
    def test_arguments_follow_ir_positions(self):
        abi = target_abi(FUNCTION_MODEL, FUNCTION_IR, "FMT")
        self.assertEqual([slot.param.name for slot in abi.call_slots], ["FMT", "S", "N", "PT"])
        self.assertEqual(abi.ret.block_kind, "RETURN")
        self.assertIsNone(abi.ret_by_value)
        harness = generate_harness(abi)
        self.assertIn("extern void FMT(void *, void *, int16_t, void *);", harness)
        self.assertIn("uint8_t result[81];", harness)
        self.assertIn("uint8_t semantist_param_S[81];", harness)
        self.assertIn('fill_pointer_value(&semantist_param_PT_storage, &semantist_param_PT_capacity, "0x00", 16);', harness)
        self.assertIn("FMT(result, semantist_param_S, semantist_param_N, semantist_param_PT_storage);", harness)

    def test_copied_input_must_fit_the_declared_string(self):
        short = FUNCTION_IR.replace("i64 80", "i64 120")
        with self.assertRaisesRegex(AbiError, "copies 120 bytes"):
            target_abi(FUNCTION_MODEL, short, "FMT")

    def test_scalar_inputs_are_passed_by_value_with_escaped_names(self):
        abi = target_abi(SCALAR_MODEL, SCALAR_IR, "PICK")
        harness = generate_harness(abi)
        self.assertIn("extern int16_t PICK(float, int64_t);", harness)
        self.assertIn("float semantist_param_default = 0;", harness)
        self.assertIn('strcmp(name, "default")', harness)
        self.assertIn("semantist_param_IDATE = (int64_t)parse_i64(value);", harness)
        self.assertIn("volatile int16_t result = PICK(semantist_param_default, semantist_param_IDATE);", harness)
        self.assertEqual(generate_seed(abi), "default,REAL,0.0\nIDATE,DATE,0\n")


class GeneratorCliTests(unittest.TestCase):
    def test_verify_rejects_a_harness_built_for_another_layout(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / "target.ll").write_text(FB_IR, encoding="utf-8")
            (directory / "stg-model.json").write_text(json.dumps(fb_model()), encoding="utf-8")
            common = ["PID", "--ir", str(directory / "target.ll"), "--stg-model", str(directory / "stg-model.json")]
            harness = directory / "harness.c"
            self.assertEqual(main([*common, "--out", str(harness)]), 0)
            self.assertEqual(main([*common, "--verify", str(harness)]), 0)
            harness.write_text(harness.read_text().replace("== 72", "== 80"), encoding="utf-8")
            self.assertEqual(main([*common, "--verify", str(harness)]), 1)


if __name__ == "__main__":
    unittest.main()
