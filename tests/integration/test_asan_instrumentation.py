"""Exercise ASan in prepared ST-style LLVM IR, not just in the C harness."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

# RuSTy emits ordinary loads/stores without sanitize_address. Keep the actual
# access in a helper so that annotating only the public entry cannot pass.
TARGET_IR = """
define i32 @ASAN_PROBE(ptr %base, i64 %index, i32 %write) {
entry:
  %result = call i32 @asan_probe_helper(ptr %base, i64 %index, i32 %write)
  ret i32 %result
}

define internal i32 @asan_probe_helper(ptr %base, i64 %index, i32 %write) noinline {
entry:
  %address = getelementptr i32, ptr %base, i64 %index
  %is_write = icmp ne i32 %write, 0
  br i1 %is_write, label %store, label %load
store:
  store volatile i32 42, ptr %address, align 4
  ret i32 42
load:
  %value = load volatile i32, ptr %address, align 4
  ret i32 %value
}

define void @ASAN_PROBE__ctor(ptr %base) {
entry:
  store i32 13, ptr %base, align 4
  ret void
}
"""

DRIVER_C = """
#include <stdlib.h>

extern int ASAN_PROBE(int *base, long index, int write);
extern void ASAN_PROBE__ctor(int *base);

int main(int argc, char **argv) {
    if (argc != 3) return 2;
    int *base = malloc(2 * sizeof(*base));
    if (!base) return 3;
    ASAN_PROBE__ctor(base);
    base[1] = 14;
    int write = atoi(argv[2]);
    int result = ASAN_PROBE(base, atol(argv[1]), write);
    free(base);
    return result == (write ? 42 : 13) ? 0 : 4;
}
"""


class AsanInstrumentationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not sys.platform.startswith("linux"):
            raise unittest.SkipTest("runtime ASan regression requires Linux")
        candidates = []
        if os.environ.get("SEMANTIST_LLVM_BIN"):
            candidates.append(Path(os.environ["SEMANTIST_LLVM_BIN"]))
        candidates.append(Path("/usr/lib/llvm-21/bin"))
        for name in ("opt-21", "opt"):
            if executable := shutil.which(name):
                candidates.append(Path(executable).resolve().parent)
        for directory in candidates:
            if not all((directory / name).is_file() for name in ("opt", "clang", "llvm-symbolizer")):
                continue
            version = subprocess.run(
                [str(directory / "opt"), "--version"],
                check=True, capture_output=True, text=True, timeout=30,
            )
            match = re.search(r"\bversion (\d+)\.", version.stdout)
            if match and int(match.group(1)) >= 21:
                cls.llvm_bin = directory
                return
        raise unittest.SkipTest("LLVM 21+ opt, clang and llvm-symbolizer are required")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "target.ll"
        self.prepared = self.directory / "prepared.ll"
        self.source.write_text(TARGET_IR, encoding="ascii")
        self.environment = os.environ.copy()
        # Test defaults independently of a developer's current ablation flags.
        for key in (
            "SEMANTIST_TARGET_SANITIZERS", "SEMANTIST_PUBLIC_API_EXTRA",
            "SEMANTIST_STRIP_GLOBAL_CTORS",
        ):
            self.environment.pop(key, None)
        self.environment["SEMANTIST_ARRAY_BOUNDS_SANITIZER"] = "1"
        self.environment["SEMANTIST_MEMORY_BOUNDS_CHECKS"] = "1"

    def run_checked(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            command, cwd=ROOT, env=self.environment,
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def prepare(self) -> str:
        self.run_checked([
            sys.executable, "-m", "compiler.scripts.prepare_target_ir",
            str(self.source), "ASAN_PROBE", "--output-ll", str(self.prepared),
            "--llvm-bin", str(self.llvm_bin),
        ])
        return self.prepared.read_text(encoding="utf-8")

    def test_prepared_ir_detects_heap_reads_and_writes_in_helper(self) -> None:
        prepared = self.prepare()
        attributes = dict(re.findall(r"^attributes #(\d+) = \{([^\n]*)\}", prepared, re.M))
        for function in ("ASAN_PROBE", "asan_probe_helper", "ASAN_PROBE__ctor"):
            definition = re.search(rf"^define[^\n]*@{function}\([^\n]*", prepared, re.M)
            self.assertIsNotNone(definition, function)
            groups = re.findall(r"#(\d+)", definition.group())
            self.assertTrue(any("sanitize_address" in attributes[group].split() for group in groups), function)

        driver = self.directory / "driver.c"
        executable = self.directory / "probe"
        driver.write_text(DRIVER_C, encoding="ascii")
        self.run_checked([
            str(self.llvm_bin / "clang"), "-O0", "-g", "-fsanitize=address",
            "-fno-omit-frame-pointer", str(self.prepared), str(driver),
            "-o", str(executable),
        ])
        self.environment["ASAN_SYMBOLIZER_PATH"] = str(self.llvm_bin / "llvm-symbolizer")
        self.environment["ASAN_OPTIONS"] = (
            "detect_leaks=0:symbolize=1:halt_on_error=1:abort_on_error=0"
        )
        for write, operation in ((0, "READ"), (1, "WRITE")):
            with self.subTest(operation=operation, index="valid"):
                safe = self.run_checked([str(executable), "0", str(write)])
                self.assertNotIn("ERROR: AddressSanitizer", safe.stderr)
            with self.subTest(operation=operation, index="one-past-allocation"):
                result = subprocess.run(
                    [str(executable), "2", str(write)], env=self.environment,
                    capture_output=True, text=True, timeout=30,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("ERROR: AddressSanitizer: heap-buffer-overflow", result.stderr)
                self.assertIn(f"{operation} of size 4", result.stderr)
                self.assertRegex(result.stderr, r"#0[^\n]*asan_probe_helper")

    def test_explicitly_disabled_asan_does_not_add_attributes(self) -> None:
        for flags in ("-fsanitize=undefined", "-fsanitize=address -fno-sanitize=address"):
            with self.subTest(flags=flags):
                self.environment["SEMANTIST_TARGET_SANITIZERS"] = flags
                prepared = self.prepare()
                self.assertIn("@asan_probe_helper(", prepared)
                self.assertNotIn("sanitize_address", prepared)


if __name__ == "__main__":
    unittest.main()
