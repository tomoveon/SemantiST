import shutil
import subprocess
from pathlib import Path

import pytest

from compiler.harness.abi import CallSlot, LLVMType, Param, TargetAbi, scalar_spec
from compiler.harness.v3gen import generate_function_harness


def test_function_native_driver_executes_each_frame(tmp_path: Path) -> None:
    clang = shutil.which("clang-21") or shutil.which("clang")
    if clang is None:
        pytest.skip("clang is required")
    value = Param("X", scalar_spec("INT"), "VAR_INPUT", index=0)
    abi = TargetAbi("FUNCTION", "FRAME_PROBE", [value],
                    call_slots=[CallSlot(value, LLVMType("int", bits=16))])
    source = tmp_path / "frame_probe.c"
    source.write_text(
        generate_function_harness(abi) + "\n"
        "static int calls;\n"
        "void FRAME_PROBE(int16_t X) { calls++; if (X == 7) exit(calls == 2 ? 17 : 31); }\n",
        encoding="ascii",
    )
    executable = tmp_path / "frame_probe"
    subprocess.run([clang, "-O1", str(source), "-o", str(executable)],
                   check=True, capture_output=True, text=True)
    testcase = tmp_path / "two_frames.txt"
    testcase.write_text("0.X,INT,1\n1.X,INT,7\n", encoding="ascii")

    result = subprocess.run([str(executable), str(testcase)], capture_output=True, text=True)

    assert result.returncode == 17
