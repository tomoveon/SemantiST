#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FIXTURE="${ROOT}/compiler/stg/tests/fixtures/control_flow.st"
COMPILER="${RUSTY_COMPILER:-${ROOT}/artifacts/rusty-semantic/target/release/plc}"
if [[ ! -x "${COMPILER}" && -x /opt/rusty/target/debug/plc ]]; then
  COMPILER=/opt/rusty/target/debug/plc
fi
if [[ ! -x "${COMPILER}" ]]; then
  echo "[semantic_ir_e2e] missing patched RuSTy compiler" >&2
  exit 77
fi

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

"${ROOT}/compiler/scripts/generate_stg.sh" \
  "${FIXTURE}" \
  --project-root "${ROOT}" \
  --output "${WORK}/stg"

SEMANTIST_STG_CODEGEN_MAP="${WORK}/stg/stg-codegen-map.json" \
  "${COMPILER}" "${FIXTURE}" \
  --ir \
  --single-module \
  --error-format none \
  -O none \
  -o "${WORK}/compiler.ll"

"${ROOT}/compiler/scripts/instrument_stg_ir.sh" \
  "${WORK}/compiler.ll" \
  --output "${WORK}/target.ll" \
  --runtime-ids "${WORK}/stg/stg-runtime-ids.json" \
  --mapping "${WORK}/stg/stg-ir-mapping.json" \
  --diagnostics "${WORK}/stg/stg-ir-diagnostics.json"

SEMANTIST_SEMANTIC_INSTRUMENTATION=ir \
OUT_LL="${WORK}/target.ll" \
PREPARED_LL="${WORK}/control.prepared.ll" \
OUT_OBJ="${WORK}/control.o" \
OUT_BIN="${WORK}/control_target" \
HARNESS_OUT="${WORK}/control_harness.c" \
GENERATED_SEED_DIR="${WORK}/control_seeds" \
  "${ROOT}/compiler/scripts/build_target.sh" "${FIXTURE}" CONTROL_FLOW

SEMANTIST_SEMANTIC_INSTRUMENTATION=ir \
OUT_LL="${WORK}/target.ll" \
PREPARED_LL="${WORK}/empty.prepared.ll" \
OUT_OBJ="${WORK}/empty.o" \
OUT_BIN="${WORK}/empty_target" \
HARNESS_OUT="${WORK}/empty_harness.c" \
GENERATED_SEED_DIR="${WORK}/empty_seeds" \
  "${ROOT}/compiler/scripts/build_target.sh" "${FIXTURE}" EMPTY_BRANCH

SEMANTIST_SEMANTIC_INSTRUMENTATION=ir \
OUT_LL="${WORK}/target.ll" \
PREPARED_LL="${WORK}/fb.prepared.ll" \
OUT_OBJ="${WORK}/fb.o" \
OUT_BIN="${WORK}/fb_target" \
HARNESS_OUT="${WORK}/fb_harness.c" \
GENERATED_SEED_DIR="${WORK}/fb_seeds" \
  "${ROOT}/compiler/scripts/build_target.sh" "${FIXTURE}" STATEFUL_COUNTER

WORK="${WORK}" python3 - <<'PY'
import ctypes
import json
import os
import subprocess
from pathlib import Path

work = Path(os.environ["WORK"])
entries = json.loads((work / "stg/stg-runtime-ids.json").read_text())["entries"]
mapping = json.loads((work / "stg/stg-ir-mapping.json").read_text())
mapped = {entry["runtime_id"] for entry in mapping["mappings"]}
unmapped = [entry for entry in entries if entry["runtime_id"] not in mapped]
assert all(
    entry["kind"] == "hazard_violation" and entry["modeling"] == "conservative"
    for entry in unmapped
)

libc = ctypes.CDLL(None)
libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
libc.shmget.restype = ctypes.c_int
libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
libc.shmat.restype = ctypes.c_void_p
libc.shmdt.argtypes = [ctypes.c_void_p]
libc.shmctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]

size = len(entries)
shmid = libc.shmget(0, size, 0o1000 | 0o600)
assert shmid >= 0
pointer = libc.shmat(shmid, None, 0)
assert pointer != ctypes.c_void_p(-1).value
bitmap = (ctypes.c_ubyte * size).from_address(pointer)
environment = os.environ.copy()
environment["SEMANTIST_SEMANTIC_SHM_ID"] = str(shmid)
environment["SEMANTIST_SEMANTIC_MAP_SIZE"] = str(size)

def run(target: Path, data: bytes) -> dict[int, int]:
    for index in range(size):
        bitmap[index] = 0
    subprocess.run([str(target)], input=data, env=environment, check=True)
    return {index: value for index, value in enumerate(bitmap) if value}

try:
    x1 = run(work / "control_target", b"x,DINT,1\n")
    x3 = run(work / "control_target", b"x,DINT,3\n")
    case_one = next(
        entry["runtime_id"]
        for entry in entries
        if entry["kind"] == "case_outcome"
        and entry["source"]
        and entry["source"]["start_line"] == 27
    )
    case_range = next(
        entry["runtime_id"]
        for entry in entries
        if entry["kind"] == "case_outcome"
        and entry["source"]
        and entry["source"]["start_line"] == 29
    )
    assert case_one in x1 and case_one not in x3
    assert case_range in x3 and case_range not in x1

    empty_true = run(work / "empty_target", b"x,DINT,1\n")
    empty_false = run(work / "empty_target", b"x,DINT,0\n")
    empty_outcomes = {
        entry["role"]: entry["runtime_id"]
        for entry in entries
        if entry["pou"] == "EMPTY_BRANCH"
        and entry["kind"] == "branch_outcome"
    }
    assert empty_outcomes["true"] in empty_true
    assert empty_outcomes["true"] not in empty_false
    assert empty_outcomes["false"] in empty_false
    assert empty_outcomes["false"] not in empty_true
    assert empty_outcomes["else"] in empty_false
    assert empty_outcomes["else"] not in empty_true

    repeat_roles = {
        entry["role"]: entry["runtime_id"]
        for entry in entries
        if entry["pou"] == "CONTROL_FLOW"
        and entry.get("role") in ("loop_continue", "loop_exit", "loop_back")
    }
    for role in ("loop_continue", "loop_exit", "loop_back"):
        assert repeat_roles[role] in x1 | x3

    seed = (work / "fb_seeds/seed1").read_bytes()
    fb = run(work / "fb_target", seed)
    cycle_entry = next(
        entry["runtime_id"] for entry in entries if entry.get("role") == "cycle_entry"
    )
    cycle_exit = next(
        entry["runtime_id"] for entry in entries if entry.get("role") == "cycle_exit"
    )
    assert fb[cycle_entry] == 2
    assert fb[cycle_exit] == 2

    fb_return = next(
        entry["runtime_id"]
        for entry in entries
        if entry["pou"] == "STATEFUL_COUNTER" and entry.get("role") == "return"
    )
    return_mapping = next(
        record for record in mapping["mappings"] if record["runtime_id"] == fb_return
    )
    cycle_mappings = [
        record
        for record in mapping["mappings"]
        if record["runtime_id"] == cycle_exit
    ]
    assert any(
        record["function"] == return_mapping["function"]
        and record["source_block"] == return_mapping["source_block"]
        and record["source_instruction_ordinal"] == return_mapping["source_instruction_ordinal"]
        for record in cycle_mappings
    )

    for_role_counts = {}
    for record in mapping["mappings"]:
        if record.get("role") in ("loop_enter", "loop_exit"):
            for_role_counts.setdefault(record["runtime_id"], 0)
            for_role_counts[record["runtime_id"]] += 1
    assert any(count >= 2 for count in for_role_counts.values())
finally:
    libc.shmdt(pointer)
    libc.shmctl(shmid, 0, None)
PY

echo "[semantic_ir_e2e] passed"
