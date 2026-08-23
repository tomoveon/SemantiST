import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "compiler/parser/st-to-rusty-converter/scripts"
FIXTURES = ROOT / "tests/fixtures/library_intake"


def configured_path(variable: str, default: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value) if value else default


PLC = configured_path(
    "RUSTY_COMPILER",
    ROOT / "artifacts/rusty-semantic/target/release/plc",
)
RUNTIME = configured_path(
    "SEMANTIST_RUSTY_STDLIB_LIB",
    ROOT / "artifacts/cargo-target/release/libiec61131std.a",
)
LLVM_BIN = configured_path(
    "SEMANTIST_LLVM_BIN",
    Path("/usr/lib/llvm-21/bin"),
)
CLANG = LLVM_BIN / "clang"

pytestmark = pytest.mark.skipif(
    not (PLC.exists() and RUNTIME.exists() and CLANG.exists()),
    reason="SemantiST patched RuSTy compiler, runtime archive, and LLVM 21 are required",
)


def prepare(source: Path, workspace: Path) -> Path:
    subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "prepare_library.py"),
            str(source),
            "--dialect",
            "generic",
            "--output-dir",
            str(workspace),
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return workspace / source.name


def validate(workspace: Path, library: Path, stubs: Path, *pous: str) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(SCRIPTS / "validate_library.py"),
        "--workspace",
        str(workspace),
        "--library",
        str(library),
        "--stubs",
        str(stubs),
    ]
    for pou in pous:
        command.extend(("--pou", pou))
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)


def validate_manifest(workspace: Path, *pous: str) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(SCRIPTS / "validate_library.py"),
        "--workspace",
        str(workspace),
        "--compatibility-manifest",
        str(workspace / "compatibility.json"),
    ]
    for pou in pous:
        command.extend(("--pou", pou))
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)


def test_example_library_builds_multiple_function_and_function_block_targets(tmp_path: Path) -> None:
    workspace = tmp_path / "abc-intake"
    library = prepare(FIXTURES / "abc.st", workspace)
    shutil.copyfile(FIXTURES / "empty-stubs.st", workspace / "stubs.st")

    result = validate(workspace, library, workspace / "stubs.st", "ADD_ONE", "ACCUMULATOR")

    assert result.returncode == 0, result.stdout + result.stderr
    verification = json.loads((workspace / "verification.json").read_text(encoding="utf-8"))
    assert verification["status"] == "passed"
    assert {item["pou"] for item in verification["targets"]} == {"ADD_ONE", "ACCUMULATOR"}
    assert all(item["link"] == "passed" for item in verification["targets"])


def test_compile_success_but_native_link_failure_is_a_blocking_result(tmp_path: Path) -> None:
    source = tmp_path / "missing.st"
    source.write_text(
        "FUNCTION CALL_MISSING : DINT\nCALL_MISSING := VENDOR_IO();\nEND_FUNCTION\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "missing-intake"
    library = prepare(source, workspace)
    (workspace / "stubs.st").write_text(
        "@EXTERNAL FUNCTION VENDOR_IO : DINT\nEND_FUNCTION\n",
        encoding="utf-8",
    )

    result = validate(workspace, library, workspace / "stubs.st", "CALL_MISSING")

    assert result.returncode == 1
    verification = json.loads((workspace / "verification.json").read_text(encoding="utf-8"))
    dependency = json.loads(
        (workspace / "missing-deps/dependency-report.json").read_text(encoding="utf-8")
    )
    assert verification["checks"]["parse_and_typecheck"] == "passed"
    assert verification["checks"]["llvm_ir"] == "passed"
    assert verification["checks"]["native_link"] == "failed"
    assert "VENDOR_IO" in dependency["classifications"]["unresolved"]
    assert dependency["gate"]["status"] == "blocked"


def test_codesys_temporal_adapters_cover_normal_boundary_truncation_and_wrap(tmp_path: Path) -> None:
    workspace = tmp_path / "compat-intake"
    library = prepare(FIXTURES / "compat-boundaries.st", workspace)
    stubs = ROOT / "benchmarks/oscat_basic/source/stubs.st"
    result = validate(workspace, library, stubs, "COMPAT_BOUNDARY_TEST")
    assert result.returncode == 0, result.stdout + result.stderr

    prepared = workspace / "validation/compat_boundary_test/prepared.ll"
    driver = tmp_path / "driver.c"
    driver.write_text(
        "#include <stdint.h>\nextern int32_t COMPAT_BOUNDARY_TEST(void);\n"
        "int main(void) { return COMPAT_BOUNDARY_TEST(); }\n",
        encoding="utf-8",
    )
    executable = tmp_path / "compat-test"
    subprocess.run(
        [
            str(CLANG),
            str(driver),
            str(prepared),
            str(RUNTIME),
            "-ldl",
            "-lpthread",
            "-lm",
            "-o",
            str(executable),
        ],
        cwd=ROOT,
        check=True,
    )
    completed = subprocess.run([str(executable)], check=False)
    assert completed.returncode == 0, f"adapter boundary case {completed.returncode} failed"


def test_multi_file_manifest_builds_function_and_function_block(tmp_path: Path) -> None:
    source = tmp_path / "multi-source"
    source.mkdir()
    (source / "function.st").write_text(
        "FUNCTION ADD_TWO : DINT\nVAR_INPUT X : DINT; END_VAR\nADD_TWO := X + 2;\nEND_FUNCTION\n",
        encoding="utf-8",
    )
    (source / "block.st").write_text(
        "FUNCTION_BLOCK COUNTER\nVAR_INPUT ENABLE : BOOL; END_VAR\nVAR_OUTPUT VALUE : DINT; END_VAR\n"
        "IF ENABLE THEN VALUE := VALUE + 1; END_IF;\nEND_FUNCTION_BLOCK\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "multi-intake"
    prepare(source, workspace)

    result = validate_manifest(workspace, "ADD_TWO", "COUNTER")

    assert result.returncode == 0, result.stdout + result.stderr
    verification = json.loads((workspace / "verification.json").read_text(encoding="utf-8"))
    assert verification["status"] == "passed"
    assert all(item["link"] == "passed" for item in verification["targets"])


def test_manifest_native_provider_is_used_by_validation(tmp_path: Path) -> None:
    source = tmp_path / "vendor.st"
    source.write_text(
        "FUNCTION CALL_VENDOR : DINT\nCALL_VENDOR := VENDOR_VALUE();\nEND_FUNCTION\n",
        encoding="utf-8",
    )
    provider = tmp_path / "provider.c"
    provider.write_text("#include <stdint.h>\nint32_t VENDOR_VALUE(void) { return 7; }\n", encoding="utf-8")
    workspace = tmp_path / "native-intake"
    subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "prepare_library.py"),
            str(source),
            "--dialect",
            "generic",
            "--native-source",
            str(provider),
            "--output-dir",
            str(workspace),
        ],
        cwd=ROOT,
        check=True,
    )
    (workspace / "stubs.st").write_text(
        "@EXTERNAL FUNCTION VENDOR_VALUE : DINT\nEND_FUNCTION\n", encoding="utf-8"
    )

    result = validate_manifest(workspace, "CALL_VENDOR")

    assert result.returncode == 0, result.stdout + result.stderr
    verification = json.loads((workspace / "verification.json").read_text(encoding="utf-8"))
    assert verification["status"] == "passed"
    dependency = json.loads(
        (workspace / "missing-deps/dependency-report.json").read_text(encoding="utf-8")
    )
    assert dependency["inventory"]["native_compatibility"] >= 1
    assert dependency["gate"]["status"] == "passed"


def test_codesys_memory_profile_executes_through_native_runtime(tmp_path: Path) -> None:
    source = tmp_path / "memory.st"
    source.write_text(
        "FUNCTION MEMORY_PROFILE_TEST : BYTE\n"
        "VAR DATA : ARRAY[0..3] OF BYTE; DEST : REF_TO BYTE; END_VAR\n"
        "DEST := REF(DATA[0]);\n"
        "SysMemSet(DEST, 170, 4);\n"
        "MEMORY_PROFILE_TEST := DATA[0];\n"
        "END_FUNCTION\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "memory-intake"
    subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "prepare_library.py"),
            str(source),
            "--dialect",
            "codesys",
            "--profile",
            "codesys-memory",
            "--output-dir",
            str(workspace),
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )

    result = validate_manifest(workspace, "MEMORY_PROFILE_TEST")
    assert result.returncode == 0, result.stdout + result.stderr

    driver = tmp_path / "memory-driver.c"
    driver.write_text(
        "#include <stdint.h>\nextern uint8_t MEMORY_PROFILE_TEST(void);\n"
        "int main(void) { return MEMORY_PROFILE_TEST() == 170 ? 0 : 1; }\n",
        encoding="utf-8",
    )
    executable = tmp_path / "memory-test"
    native_objects = json.loads((workspace / "verification.json").read_text(encoding="utf-8"))[
        "native_objects"
    ]
    subprocess.run(
        [
            str(CLANG),
            str(driver),
            str(workspace / "validation/memory_profile_test/prepared.ll"),
            str(RUNTIME),
            *native_objects,
            "-ldl",
            "-lpthread",
            "-lm",
            "-o",
            str(executable),
        ],
        cwd=ROOT,
        check=True,
    )
    completed = subprocess.run([str(executable)], check=False)
    assert completed.returncode == 0
