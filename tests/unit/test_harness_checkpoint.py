"""Checkpoint/restore must reproduce a suffix bit-for-bit."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from compiler.harness.abi import Param, TargetAbi, scalar_spec  # noqa: E402
from compiler.harness.generator import generate_harness  # noqa: E402
from compiler.harness.v3gen import generate_function_block_harness  # noqa: E402

TARGET = r"""
void ACC__ctor(semantist_fb_state_t *inst) { inst->f0 = 0; inst->f1 = 0; }
void ACC(semantist_fb_state_t *inst) { inst->f1 += inst->f0; }
const struct semantist_state_region __semantist_state_regions[] = {{&total, sizeof(total)}};
const uint64_t __semantist_state_region_count = 1;

static uint8_t after_full[8192];
static uint8_t after_restore[8192];

int main(void) {
    semantist_frame_t frame;
    size_t size = semantist_state_size();
    if (size > sizeof(after_full)) return 2;

    memset(&frame, 0, sizeof(frame));
    semantist_init("");
    frame.f0 = 5;
    semantist_step(&frame, 10000000ULL);
    uint8_t checkpoint[8192];
    semantist_checkpoint(checkpoint);
    frame.f0 = 7;
    semantist_step(&frame, 10000000ULL);
    semantist_checkpoint(after_full);

    memset(&frame, 0, sizeof(frame));
    semantist_init("");
    frame.f0 = 5;
    semantist_step(&frame, 10000000ULL);
    semantist_restore(checkpoint);
    frame.f0 = 7;
    semantist_step(&frame, 10000000ULL);
    semantist_checkpoint(after_restore);
    return memcmp(after_full, after_restore, size) == 0 ? 0 : 1;
}
"""


class CheckpointTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("cc"), "requires a C compiler")
    def test_restore_reproduces_the_suffix(self):
        step = Param("STEP", scalar_spec("DINT"), "VAR_INPUT", offset=8, index=1)
        out = Param("OUT", scalar_spec("DINT"), "VAR_OUTPUT", offset=12, index=2)
        abi = TargetAbi("FUNCTION_BLOCK", "ACC", [step], outputs=[out], state_fields=[step, out],
                        snapshot_fields=[out], instance_size=16, instance_align=8)
        source_text = generate_harness(abi) + "\nstatic int32_t total;\n" + TARGET.replace(
            "const struct semantist_state_region", "const struct semantist_state_region"
        )
        # The region table must see `total`, so declare it before the table.
        source_text = source_text.replace(
            "static int32_t total;\nvoid ACC__ctor",
            "static int32_t total;\nvoid ACC__ctor",
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "checkpoint.c"
            # Place the global before the generated state table.
            source.write_text(source_text, encoding="utf-8")
            binary = Path(directory) / "checkpoint"
            completed = subprocess.run(
                ["cc", "-DSEMANTIST_IN_PROCESS", "-O1", str(source), "-o", str(binary)],
                capture_output=True, text=True,
            )
            if completed.returncode != 0:
                self.skipTest(f"C compiler unavailable for generated harness: {completed.stderr[:200]}")
            run = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)

    @unittest.skipUnless(shutil.which("cc"), "requires a C compiler")
    def test_relocation_rebinds_pointer_to_current_harness_storage(self):
        value = Param("VALUE", scalar_spec("DINT"), "VAR_IN_OUT", offset=8, index=1)
        abi = TargetAbi("FUNCTION_BLOCK", "PTR", [value], state_fields=[value],
                        instance_size=16, instance_align=8)
        target = r"""
void PTR__ctor(semantist_fb_state_t *inst) { inst->f0 = 0; }
void PTR(semantist_fb_state_t *inst) {
    if (inst->f0) *((int32_t *)inst->f0) += 1;
}

int main(void) {
    uint8_t blob[1024];
    semantist_frame_t frame;
    memset(&frame, 0, sizeof(frame));
    if (semantist_init("") != 0) return 2;
    size_t size = semantist_state_size();
    if (size > sizeof(blob)) return 3;
    semantist_checkpoint(blob);

    size_t pointer_offset = offsetof(semantist_harness_state_t, inst)
        + offsetof(semantist_fb_state_t, f0);
    size_t storage_offset = offsetof(semantist_harness_state_t, f0_storage);
    uintptr_t foreign = (uintptr_t)0x123456789abcdef0ULL;
    memcpy(blob + pointer_offset, &foreign, sizeof(foreign));
    struct semantist_reloc relocation = {
        (uint32_t)semantist_state_region_count(), pointer_offset,
        (uint32_t)semantist_state_region_count(), storage_offset,
    };
    if (semantist_import_state(blob, &relocation, 1) != 0) return 4;
    if (semantist_harness_state.inst.f0 != &semantist_harness_state.f0_storage) return 5;
    if (semantist_step(&frame, 10000000ULL) != 0) return 6;
    return semantist_harness_state.f0_storage == 1 ? 0 : 7;
}
"""
        source_text = generate_function_block_harness(abi) + "\n" + target
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "relocation.c"
            source.write_text(source_text, encoding="utf-8")
            binary = Path(directory) / "relocation"
            build = subprocess.run(
                ["cc", "-DSEMANTIST_IN_PROCESS", "-O1", str(source), "-o", str(binary)],
                capture_output=True, text=True,
            )
            self.assertEqual(build.returncode, 0, build.stderr[:1000])
            run = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)

    @unittest.skipUnless(shutil.which("cc"), "requires a C compiler")
    def test_relocation_survives_export_and_import_in_separate_processes(self):
        value = Param("VALUE", scalar_spec("DINT"), "VAR_IN_OUT", offset=8, index=1)
        abi = TargetAbi("FUNCTION_BLOCK", "PTR", [value], state_fields=[value],
                        instance_size=16, instance_align=8)
        target = r'''
void PTR__ctor(semantist_fb_state_t *inst) { inst->f0 = 0; }
void PTR(semantist_fb_state_t *inst) {
    if (inst->f0) *((int32_t *)inst->f0) += 1;
}

int main(int argc, char **argv) {
    if (argc != 2) return 10;
#ifdef EXPORTER
    uint8_t blob[1024];
    if (semantist_init("") != 0) return 11;
    semantist_harness_state.f0_storage = 41;
    size_t size = semantist_state_size();
    if (size > sizeof(blob)) return 12;
    semantist_checkpoint(blob);
    FILE *out = fopen(argv[1], "wb");
    if (!out || fwrite(blob, 1, size, out) != size) return 13;
    fclose(out);
    return 0;
#else
    uint8_t blob[1024];
    FILE *in = fopen(argv[1], "rb");
    if (!in) return 20;
    size_t size = semantist_state_size();
    if (size > sizeof(blob) || fread(blob, 1, size, in) != size) return 21;
    fclose(in);
    if (semantist_init("") != 0) return 22;
    size_t pointer_offset = offsetof(semantist_harness_state_t, inst)
        + offsetof(semantist_fb_state_t, f0);
    size_t storage_offset = offsetof(semantist_harness_state_t, f0_storage);
    struct semantist_reloc relocation = {
        (uint32_t)semantist_state_region_count(), pointer_offset,
        (uint32_t)semantist_state_region_count(), storage_offset,
    };
    if (semantist_import_state(blob, &relocation, 1) != 0) return 23;
    semantist_frame_t frame;
    memset(&frame, 0, sizeof(frame));
    if (semantist_step(&frame, 10000000ULL) != 0) return 24;
    return semantist_harness_state.f0_storage == 42 ? 0 : 25;
#endif
}
'''
        source_text = generate_function_block_harness(abi) + "\n" + target
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "relocation-cross-process.c"
            source.write_text(source_text, encoding="utf-8")
            exporter = Path(directory) / "exporter"
            importer = Path(directory) / "importer"
            for macro, binary in (("EXPORTER", exporter), ("IMPORTER", importer)):
                build = subprocess.run(
                    ["cc", "-DSEMANTIST_IN_PROCESS", f"-D{macro}", "-O1",
                     str(source), "-o", str(binary)],
                    capture_output=True, text=True,
                )
                self.assertEqual(build.returncode, 0, build.stderr[:1000])
            checkpoint = Path(directory) / "checkpoint.bin"
            exported = subprocess.run([str(exporter), str(checkpoint)], capture_output=True, text=True)
            self.assertEqual(exported.returncode, 0, exported.stderr)
            imported = subprocess.run([str(importer), str(checkpoint)], capture_output=True, text=True)
            self.assertEqual(imported.returncode, 0, imported.stderr)


if __name__ == "__main__":
    unittest.main()


CACHE_TARGET = r"""
void ACC__ctor(semantist_fb_state_t *inst) { inst->f0 = 0; inst->f1 = 0; }
void ACC(semantist_fb_state_t *inst) { inst->f1 += inst->f0; }
const struct semantist_state_region __semantist_state_regions[] = {{&total, sizeof(total)}};
const uint64_t __semantist_state_region_count = 1;

static char input[4096];
static uint8_t first[8192];
static uint8_t second[8192];

int main(void) {
    size_t offset = 0;
    for (int frame = 0; frame < 40; frame++) {
        offset += (size_t)snprintf(input + offset, sizeof(input) - offset,
                                   "%d.STEP,DINT,1\n", frame);
    }
    semantist_harness_execute((const uint8_t *)input, offset);
    semantist_checkpoint(first);
    size_t hits1 = semantist_snapshot_hits();
    size_t saved1 = semantist_snapshot_saved_scans();
    semantist_harness_execute((const uint8_t *)input, offset);
    semantist_checkpoint(second);
    size_t hits2 = semantist_snapshot_hits();
    size_t saved2 = semantist_snapshot_saved_scans();
    if (hits1 != 0 || saved1 != 0 || hits2 != 0 || saved2 != 0) return 1;
    if (memcmp(first, second, semantist_state_size()) != 0) return 3;
    return 0;
}
"""


class PrefixCacheTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("cc"), "requires a C compiler")
    def test_text_replay_remains_cold_across_repeated_inputs(self):
        step = Param("STEP", scalar_spec("DINT"), "VAR_INPUT", offset=8, index=1)
        out = Param("OUT", scalar_spec("DINT"), "VAR_OUTPUT", offset=12, index=2)
        abi = TargetAbi("FUNCTION_BLOCK", "ACC", [step], outputs=[out], state_fields=[step, out],
                        snapshot_fields=[out], instance_size=16, instance_align=8)
        source_text = generate_harness(abi) + "\nstatic int32_t total;\n" + CACHE_TARGET
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "cache.c"
            source.write_text(source_text, encoding="utf-8")
            binary = Path(directory) / "cache"
            build = subprocess.run(
                ["cc", "-DSEMANTIST_IN_PROCESS", "-O1", str(source), "-o", str(binary)],
                capture_output=True, text=True,
            )
            self.assertEqual(build.returncode, 0, build.stderr[:500])
            run = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
