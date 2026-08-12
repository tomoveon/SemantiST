#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FIXTURE="${ROOT}/compiler/stg/tests/fixtures/semantic_ir_hazards.st"
COMPILER="${RUSTY_COMPILER:-${ROOT}/artifacts/rusty-semantic/target/release/plc}"
if [[ ! -x "${COMPILER}" && -x /opt/rusty/target/debug/plc ]]; then
  COMPILER=/opt/rusty/target/debug/plc
fi
if [[ ! -x "${COMPILER}" ]]; then
  echo "[semantic_ir_hazard_e2e] missing patched RuSTy compiler" >&2
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
PREPARED_LL="${WORK}/target.prepared.ll" \
OUT_OBJ="${WORK}/target.o" \
OUT_BIN="${WORK}/fuzz_target" \
HARNESS_OUT="${WORK}/harness.c" \
GENERATED_SEED_DIR="${WORK}/seeds" \
  "${ROOT}/compiler/scripts/build_target.sh" "${FIXTURE}" SEMANTIC_HAZARDS

WORK="${WORK}" python3 - <<'PY'
import ctypes
import json
import os
import subprocess
from pathlib import Path

work = Path(os.environ["WORK"])
entries = json.loads((work / "stg/stg-runtime-ids.json").read_text())["entries"]
mapping = json.loads((work / "stg/stg-ir-mapping.json").read_text())
assert mapping["statistics"]["unmapped_target_count"] == 0

violations = {}
for entry in entries:
    if entry["kind"] == "hazard_violation":
        violations.setdefault(entry["hazard"], set()).add(entry["runtime_id"])
assert {"arithmetic_boundary", "division", "array_access"} <= violations.keys()

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

def run(a: int, b: int, index: int) -> dict[int, int]:
    for offset in range(size):
        bitmap[offset] = 0
    data = f"a,DINT,{a}\nb,DINT,{b}\nindex,DINT,{index}\n".encode()
    subprocess.run(
        [str(work / "fuzz_target")],
        input=data,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=2,
        check=False,
    )
    return {offset: value for offset, value in enumerate(bitmap) if value}

try:
    safe = run(1, 1, 0)
    overflow = run(2147483647, 1, 0)
    division = run(1, 0, 0)
    array = run(1, 1, 4)
    assert not (violations["arithmetic_boundary"] & safe.keys())
    assert violations["arithmetic_boundary"] & overflow.keys()
    assert not (violations["division"] & safe.keys())
    assert violations["division"] & division.keys()
    assert not (violations["array_access"] & safe.keys())
    assert violations["array_access"] & array.keys()
finally:
    libc.shmdt(pointer)
    libc.shmctl(shmid, 0, None)
PY

echo "[semantic_ir_hazard_e2e] passed"
