import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from compiler.harness.abi import Param, TargetAbi, scalar_spec
from compiler.harness.contract import abi_json
from compiler.harness.generator import generate_harness
from compiler.symbolic.driver import generate_driver, symbolic_scope
from compiler.symbolic.solver import _field_from_objects


@pytest.mark.skipif(shutil.which("cc") is None, reason="requires a C compiler")
@pytest.mark.parametrize("symbolic,expected", [
    (False, "1 7 11\n2 9 33\n3 9 63\n"),
    (True, "1 7 11\n2 9 33\n3 42 63\n"),
])
def test_klee_driver_replays_shared_inout_and_each_prefix_delta_t(symbolic: bool, expected: str) -> None:
    step = Param("STEP", scalar_spec("INT"), "VAR_INPUT", offset=8, index=1)
    counter = Param("Counter", scalar_spec("INT"), "VAR_IN_OUT", offset=16, index=2)
    abi = TargetAbi("FUNCTION_BLOCK", "ACC", [step, counter], state_fields=[step, counter],
                    instance_size=24, instance_align=8)
    target = generate_harness(abi) + """
void ACC__ctor(semantist_fb_state_t *p) { (void)p; }
void ACC(semantist_fb_state_t *p) {
    printf("%d %d %llu\\n", p->f0, *(int16_t *)p->f1,
           (unsigned long long)__semantist_vclock_ns());
}
"""
    driver = generate_driver(
        abi_json(abi), goal_id=1, dt_ns=30,
        base_frame={"STEP": "3", "Counter": "9"},
        symbolic_fields=["Counter"] if symbolic else [],
        shared_records="Counter,INT,7\n",
        prefix_frames=[{"STEP": "1"}, {"STEP": "2", "Counter": "9"}],
        prefix_dt_ns=[11, 22], window=1,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "klee").mkdir()
        (root / "klee" / "klee.h").write_text(
            "#include <stddef.h>\n"
            "static inline void klee_make_symbolic(void *p, size_t n, const char *s) {"
            "if (s && s[0]=='C') {unsigned char *b=p; for(size_t i=0;i<n;i++) b[i]=i?0:42;}}\n"
            "static inline void klee_assume(int c) {(void)c;}\n",
            encoding="utf-8",
        )
        (root / "target.c").write_text(target, encoding="utf-8")
        (root / "driver.c").write_text(driver, encoding="utf-8")
        binary = root / "run"
        build = subprocess.run(["cc", "-w", "-DSEMANTIST_IN_PROCESS", "-I", str(root),
                                str(root / "target.c"), str(root / "driver.c"), "-o", str(binary)],
                               capture_output=True, text=True)
        assert build.returncode == 0, build.stderr
        run = subprocess.run([str(binary)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert run.stdout == expected


def test_klee_driver_emits_snapshot_relocations() -> None:
    abi = TargetAbi("FUNCTION", "F", [Param("X", scalar_spec("INT"), "VAR_INPUT")])
    driver = generate_driver(
        abi_json(abi), goal_id=3, dt_ns=10, state_bytes=b"\x00" * 16,
        relocations=[{"region": 0, "offset": 0, "target_region": 1, "target_offset": 8}],
    )
    assert "initial_relocs[]" in driver
    assert "{0,0,1,8}" in driver
    assert "semantist_import_state(initial_state, initial_relocs, 1UL)" in driver


def test_ktest_decode_uses_unsigned_type_name_when_c_type_is_missing() -> None:
    abi = {
        "frame_fields": [
            {"name": "U", "spec": {"kind": "scalar", "name": "UINT", "size": 2}},
            {"name": "L", "spec": {"kind": "scalar", "name": "ULINT", "size": 8}},
        ],
        "shared_fields": [],
    }
    values = _field_from_objects(
        abi,
        {"U": b"\xff\xff", "L": b"\xff\xff\xff\xff\xff\xff\xff\xff"},
        {},
    )
    assert values == {"U": "65535", "L": "18446744073709551615"}


def test_symbolic_scope_and_driver_cover_float_string_pointer_and_real_array() -> None:
    abi = {
        "frame": {
            "size": 128,
            "fields": [
                {"name": "R", "c_name": "f0", "offset": 0, "size": 4,
                 "spec": {"kind": "scalar", "name": "REAL", "size": 4, "byte_size": 4}},
                {"name": "Text", "c_name": "f1", "offset": 4, "size": 6,
                 "spec": {"kind": "string", "name": "STRING", "size": 1, "length": 5, "byte_size": 6}},
                {"name": "Wide", "c_name": "f2", "offset": 10, "size": 8,
                 "spec": {"kind": "string", "name": "WSTRING", "size": 2, "length": 3, "byte_size": 8}},
                {"name": "P", "c_name": "f3", "offset": 18, "size": 32,
                 "spec": {"kind": "pointer", "name": "REF_TO BYTE", "byte_size": 8,
                          "element": {"kind": "scalar", "name": "BYTE", "size": 1, "byte_size": 1}}},
                {"name": "A", "c_name": "f4", "offset": 50, "size": 16,
                 "spec": {"kind": "array", "name": "ARRAY OF REAL", "count": 2, "byte_size": 16,
                          "element": {"kind": "scalar", "name": "REAL", "size": 8, "byte_size": 8}}},
            ],
        },
        "frame_fields": [
            {"name": "R", "spec": {"kind": "scalar", "name": "REAL", "size": 4, "byte_size": 4}},
            {"name": "Text", "spec": {"kind": "string", "name": "STRING", "size": 1, "length": 5, "byte_size": 6}},
            {"name": "Wide", "spec": {"kind": "string", "name": "WSTRING", "size": 2, "length": 3, "byte_size": 8}},
            {"name": "P", "spec": {"kind": "pointer", "name": "REF_TO BYTE", "byte_size": 8,
                                      "element": {"kind": "scalar", "name": "BYTE", "size": 1, "byte_size": 1}}},
            {"name": "A", "spec": {"kind": "array", "name": "ARRAY OF REAL", "count": 2, "byte_size": 16,
                                      "element": {"kind": "scalar", "name": "REAL", "size": 8, "byte_size": 8}}},
        ],
        "shared_fields": [],
    }
    supported, concretized = symbolic_scope(abi, None)
    assert supported == ["R", "Text", "Wide", "P", "A"]
    assert concretized == []
    driver = generate_driver(abi, goal_id=1, dt_ns=10, base_frame={"P": "0x00"})
    assert 'klee_make_symbolic(sym_0_f0, 4, "R")' in driver
    assert 'klee_make_symbolic(sym_0_f1, 6, "Text")' in driver
    assert 'klee_make_symbolic(sym_0_f2, 8, "Wide")' in driver
    assert 'klee_make_symbolic(sym_0_f3, 1, "P")' in driver
    assert 'klee_make_symbolic(sym_0_f4_0, 8, "A[0]")' in driver
    assert 'klee_assume(sym_0_f1[0] == 0 ||' in driver


def test_pointer_frame_wire_size_matches_generated_backing_array() -> None:
    pointer = Param(
        "P", scalar_spec("BYTE"), "VAR_INPUT", offset=0, index=1,
    )
    pointer.spec.kind = "pointer"
    pointer.spec.name = "REF_TO BYTE"
    pointer.spec.element = scalar_spec("BYTE")
    document = abi_json(TargetAbi("FUNCTION", "P", [pointer]))
    assert document["frame"]["fields"][0]["size"] == 4096
    assert document["frame"]["size"] == 4096
