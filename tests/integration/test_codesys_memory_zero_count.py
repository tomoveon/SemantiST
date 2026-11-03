import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNTIMES = (
    ROOT / "benchmarks/external/compatibility/codesys-memory/runtime.c",
    ROOT / "compiler/toolchain/compatibility/profiles/codesys-memory/runtime.c",
)


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_zero_count_memory_operations_accept_null_pointers(runtime: Path, tmp_path: Path) -> None:
    clang = shutil.which("clang-21") or shutil.which("clang")
    if clang is None:
        pytest.skip("clang is required")
    source = tmp_path / "zero_count.c"
    source.write_text(
        """#include <stdint.h>
#include <stddef.h>

uint8_t *__SEMANTIST_MEMSET_CHECKED(uint8_t *, uint64_t, uint32_t, uint32_t);
uint8_t *__SEMANTIST_MEMCPY_CHECKED(uint8_t *, uint64_t, const uint8_t *, uint64_t, uint32_t);
uint8_t *__SEMANTIST_MEMMOVE_CHECKED(uint8_t *, uint64_t, const uint8_t *, uint64_t, uint32_t);

int main(void) {
    uint8_t source[2] = {7, 9};
    uint8_t target[2] = {0, 0};
    if (__SEMANTIST_MEMSET_CHECKED(NULL, 0, 1, 0) != NULL) return 1;
    if (__SEMANTIST_MEMCPY_CHECKED(NULL, 0, NULL, 0, 0) != NULL) return 2;
    if (__SEMANTIST_MEMMOVE_CHECKED(NULL, 0, NULL, 0, 0) != NULL) return 3;
    if (__SEMANTIST_MEMCPY_CHECKED(target, 2, source, 2, 2) != target) return 4;
    if (target[0] != 7 || target[1] != 9) return 5;
    return 0;
}
""",
        encoding="ascii",
    )
    executable = tmp_path / "zero_count"
    subprocess.run(
        [clang, "-O1", "-fsanitize=undefined", "-fno-sanitize-recover=undefined",
         str(source), str(runtime), "-o", str(executable)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run([str(executable)], check=True, capture_output=True, text=True)
