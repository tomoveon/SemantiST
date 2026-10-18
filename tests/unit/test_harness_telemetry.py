"""Synthetic wrapper/runtime checks; no fuzzing or benchmark execution."""
import ctypes
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

RUNTIME = ROOT / "compiler/instrumentation/runtime/semantic_coverage.c"
TELEMETRY_SIZE = 16 + 4096 * 8 + 512 * 16


class HarnessTelemetryTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("cc"), "requires Linux C compiler and SysV SHM")
    def test_default_hooks_measure_real_scans_and_abstract_state_without_trace_files(self):
        # FUNCTION_BLOCK COUNTER: VAR_INPUT STEP : DINT; VAR COUNT : DINT.
        step = Param("STEP", scalar_spec("DINT"), "VAR_INPUT", offset=8, index=1)
        count = Param("COUNT", scalar_spec("DINT"), "VAR", offset=12, index=2)
        abi = TargetAbi("FUNCTION_BLOCK", "COUNTER", [step], state_fields=[step, count],
                        snapshot_fields=[count], instance_size=16, instance_align=8)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            harness = generate_harness(abi)
            harness += """
extern void __semantist_semantic_hit(uint32_t);
void COUNTER__ctor(semantist_fb_state_t *inst) { inst->f1 = 0; }
void COUNTER(semantist_fb_state_t *inst) {
    inst->f1 += inst->f0;
    __semantist_semantic_hit(1);
}
"""
            c_file = directory / "harness.c"
            c_file.write_text(harness, encoding="utf-8")
            binary = directory / "counter"
            subprocess.run(["cc", "-O2", str(c_file), str(RUNTIME), "-o", str(binary)], check=True, capture_output=True)
            libc = ctypes.CDLL(None, use_errno=True)
            libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
            libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
            libc.shmat.restype = ctypes.c_void_p
            libc.shmdt.argtypes = [ctypes.c_void_p]
            libc.shmctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            shm_id = libc.shmget(0, TELEMETRY_SIZE, 0o1000 | 0o600)
            self.assertGreaterEqual(shm_id, 0)
            memory = libc.shmat(shm_id, None, 0)
            self.assertNotEqual(memory, ctypes.c_void_p(-1).value)
            try:
                environment = {key: value for key, value in os.environ.items()
                               if not key.startswith("SEMANTIST_")}
                environment["SEMANTIST_TELEMETRY_SHM_ID"] = str(shm_id)
                def execute(text):
                    ctypes.memset(memory, 0, TELEMETRY_SIZE)
                    subprocess.run([str(binary)], input=text.encode(), env=environment, check=True, capture_output=True)
                    return ctypes.string_at(memory, TELEMETRY_SIZE)
                first = execute("0.STEP,DINT,1\n1.STEP,DINT,1\n")
                self.assertEqual(int.from_bytes(first[:4], sys.byteorder), 2)
                self.assertEqual(int.from_bytes(first[4:8], sys.byteorder), 2)
                self.assertEqual(int.from_bytes(first[8:12], sys.byteorder), 2)
                self.assertEqual(int.from_bytes(first[20:24], sys.byteorder), 0)
                self.assertEqual(int.from_bytes(first[28:32], sys.byteorder), 1)
                repeated = execute("3.STEP,DINT,1\n9.STEP,DINT,1\n")
                self.assertEqual(first, repeated, "labels must not masquerade as executed scan counts")
                state_offset = 16 + 4096 * 8
                progressed = execute("0.STEP,DINT,1\n1.STEP,DINT,16\n")
                self.assertEqual(first[16:32], progressed[16:32], "same static probes")
                self.assertNotEqual(first[state_offset:state_offset + 32], progressed[state_offset:state_offset + 32])
                # Magnitude abstraction deliberately coalesces nearby large values.
                same_bucket_a = execute("0.STEP,DINT,100\n")
                same_bucket_b = execute("0.STEP,DINT,101\n")
                self.assertEqual(same_bucket_a[state_offset:state_offset + 16], same_bucket_b[state_offset:state_offset + 16])
                self.assertEqual(sorted(path.name for path in directory.iterdir()), ["counter", "harness.c"])
            finally:
                libc.shmdt(memory)
                libc.shmctl(shm_id, 0, None)


if __name__ == "__main__":
    unittest.main()
