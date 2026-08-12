from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class SemanticIrRuntimeTest(unittest.TestCase):
    def test_shared_bitmap_and_conditional_hazard_probe(self) -> None:
        clang = shutil.which("clang-21") or shutil.which("clang")
        if not clang:
            self.skipTest("clang is required")

        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            driver = directory / "driver.c"
            executable = directory / "driver"
            driver.write_text(
                """
#include "semantic_coverage.h"

int main(void) {
    __semantist_semantic_hit(2);
    __semantist_semantic_hit(2);
    __semantist_semantic_hazard(4, 0);
    __semantist_semantic_hazard(5, 1);
    return 0;
}
""",
                encoding="ascii",
            )
            subprocess.run(
                [
                    clang,
                    "-I",
                        str(ROOT / "compiler" / "instrumentation" / "runtime"),
                    str(driver),
                        str(ROOT / "compiler" / "instrumentation" / "runtime" / "semantic_coverage.c"),
                    "-o",
                    str(executable),
                ],
                check=True,
            )

            libc = ctypes.CDLL(None)
            libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
            libc.shmget.restype = ctypes.c_int
            libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
            libc.shmat.restype = ctypes.c_void_p
            libc.shmdt.argtypes = [ctypes.c_void_p]
            libc.shmctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]

            size = 8
            shmid = libc.shmget(0, size, 0o1000 | 0o600)
            self.assertGreaterEqual(shmid, 0)
            pointer = libc.shmat(shmid, None, 0)
            self.assertNotEqual(pointer, ctypes.c_void_p(-1).value)
            bitmap = (ctypes.c_ubyte * size).from_address(pointer)
            environment = os.environ.copy()
            environment["SEMANTIST_SEMANTIC_SHM_ID"] = str(shmid)
            environment["SEMANTIST_SEMANTIC_MAP_SIZE"] = str(size)
            try:
                subprocess.run([str(executable)], env=environment, check=True)
                self.assertEqual(bitmap[2], 2)
                self.assertEqual(bitmap[4], 0)
                self.assertEqual(bitmap[5], 1)
            finally:
                libc.shmdt(pointer)
                libc.shmctl(shmid, 0, None)


if __name__ == "__main__":
    unittest.main()
