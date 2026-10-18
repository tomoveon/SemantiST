"""Target state reset for persistent and in-process execution."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from compiler.harness.abi import Param, TargetAbi, scalar_spec  # noqa: E402
from compiler.harness.generator import generate_harness  # noqa: E402
from compiler.harness.state_table import StateTableError, append_state_table, writable_globals  # noqa: E402

IR = r"""
%FB = type { ptr, i32 }
@TOTAL = internal global i32 0, align 4
@"quoted.name" = internal global [4 x i8] zeroinitializer, align 1
@PACKED = dso_local global <{ i8, i32 }> <{ i8 1, i32 2 }>, align 1
@__vtable_FB_instance = internal global %FB zeroinitializer, align 8
@LIMIT = internal constant i32 7, align 4
@EXTERN = external global i32, align 4
@LOCAL = internal thread_local global i32 0, align 4
@llvm.global_ctors = appending global [0 x { i32, ptr, ptr }] zeroinitializer
@NESTED = internal global { [2 x %FB], i64 } zeroinitializer, align 8
"""


class StateTableTests(unittest.TestCase):
    def test_only_writable_storage_defined_here_is_recorded(self):
        regions = {region.name: region.llvm_type for region in writable_globals(IR)}
        self.assertEqual(regions, {
            "@TOTAL": "i32",
            '@"quoted.name"': "[4 x i8]",
            "@PACKED": "<{ i8, i32 }>",
            "@__vtable_FB_instance": "%FB",
            "@NESTED": "{ [2 x %FB], i64 }",
        })

    def test_table_sizes_each_region_by_its_ir_type(self):
        text, count = append_state_table(IR)
        self.assertEqual(count, 5)
        self.assertIn("@__semantist_state_region_count = constant i64 5", text)
        self.assertIn(
            "{ ptr, i64 } { ptr @TOTAL, i64 ptrtoint (ptr getelementptr (i32, ptr null, i32 1) to i64) }", text
        )
        with self.assertRaises(StateTableError):
            append_state_table(text)

    def test_module_without_writable_globals_gets_an_empty_table(self):
        text, count = append_state_table("@LIMIT = internal constant i32 7\n")
        self.assertEqual(count, 0)
        self.assertIn("[0 x { ptr, i64 }] zeroinitializer", text)

    @unittest.skipUnless(shutil.which("clang"), "requires clang for LLVM IR")
    def test_generated_table_compiles(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "state.ll"
            source.write_text(append_state_table(IR)[0], encoding="utf-8")
            subprocess.run(["clang", "-c", str(source), "-o", str(Path(directory) / "state.o")], check=True)


# FUNCTION_BLOCK ACCUMULATE whose step also adds into a module global.
TARGET_C = r"""
static int32_t total;
void ACCUMULATE__ctor(semantist_fb_state_t *inst) { inst->f1 = 0; }
void ACCUMULATE(semantist_fb_state_t *inst) { total += inst->f0; inst->f1 = total; }
const struct semantist_state_region __semantist_state_regions[] = {{&total, sizeof(total)}};
const uint64_t __semantist_state_region_count = REGIONS;

int main(void) {
    static const char input[] = "0.STEP,DINT,5\n1.STEP,DINT,2\n";
    for (int run = 0; run < 3; run++) {
        semantist_harness_execute((const uint8_t *)input, sizeof(input) - 1);
    }
    return 0;
}
"""


class HarnessStateResetTests(unittest.TestCase):
    def run_in_one_process(self, regions: int) -> list[str]:
        step = Param("STEP", scalar_spec("DINT"), "VAR_INPUT", offset=8, index=1)
        out = Param("OUT", scalar_spec("DINT"), "VAR_OUTPUT", offset=12, index=2)
        abi = TargetAbi("FUNCTION_BLOCK", "ACCUMULATE", [step], outputs=[out], state_fields=[step, out],
                        snapshot_fields=[out], instance_size=16, instance_align=8)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "harness.c"
            source.write_text(generate_harness(abi) + TARGET_C.replace("REGIONS", str(regions)), encoding="utf-8")
            binary = directory / "accumulate"
            subprocess.run(["cc", "-DSEMANTIST_IN_PROCESS", "-O1", str(source), "-o", str(binary)],
                           check=True, capture_output=True)
            trace = directory / "outputs.txt"
            environment = {key: value for key, value in os.environ.items() if not key.startswith("SEMANTIST_")}
            environment["SEMANTIST_HARNESS_OUTPUT_TRACE"] = str(trace)
            subprocess.run([str(binary)], env=environment, check=True)
            return trace.read_text(encoding="utf-8").splitlines()

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("cc"), "requires Linux C compiler")
    def test_each_input_starts_from_the_state_before_the_first_input(self):
        lines = self.run_in_one_process(1)
        # STEP=5 then STEP=2: OUT is 5, then 7, for every repetition.
        self.assertEqual(lines, ["0500000005000000|0200000007000000|"] * 3)

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("cc"), "requires Linux C compiler")
    def test_without_recorded_regions_the_global_leaks_into_later_inputs(self):
        lines = self.run_in_one_process(0)
        self.assertEqual(lines[0], "0500000005000000|0200000007000000|")
        self.assertEqual(lines[1], "050000000c000000|020000000e000000|")

    def test_mode_dependent_code_is_excluded_from_edge_coverage(self):
        step = Param("STEP", scalar_spec("DINT"), "VAR_INPUT", offset=8, index=1)
        harness = generate_harness(TargetAbi("FUNCTION_BLOCK", "P", [step], state_fields=[step],
                                             instance_size=16, instance_align=8))
        for function in ("int main(", "int semantist_harness_execute(", "static void semantist_state_reset(",
                         "static void semantist_fb_runtime_init(", "static uint8_t *read_all("):
            line = next(line for line in harness.splitlines() if function in line)
            self.assertTrue(line.startswith("SEMANTIST_DRIVER "), line)
        self.assertIn("static int semantist_run_input(char *data) {\n    semantist_state_reset();", harness)

    def test_persistent_loop_needs_the_forkserver_request_and_a_resettable_target(self):
        step = Param("STEP", scalar_spec("DINT"), "VAR_INPUT", offset=8, index=1)
        harness = generate_harness(TargetAbi("FUNCTION_BLOCK", "P", [step], state_fields=[step],
                                             instance_size=16, instance_align=8))
        self.assertIn("int __afl_sharedmem_fuzzing = 1;", harness)
        self.assertIn('if (getenv("__AFL_PERSISTENT") && __afl_persistent_loop && semantist_state_resettable()) {',
                      harness)
        loop = harness[harness.index("while (__afl_persistent_loop("):]
        self.assertLess(loop.index("__afl_area_ptr[0] = 0;"), loop.index("semantist_harness_execute("))


if __name__ == "__main__":
    unittest.main()
