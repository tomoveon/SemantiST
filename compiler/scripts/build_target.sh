#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# Pipeline builds export one absolute deadline for compile, native link, and
# symbolic phases. Keep the symbolic child from restarting a full default
# budget after earlier compilation work has consumed part of it.
build_remaining_seconds() {
  if [[ -z "${SEMANTIST_BUILD_DEADLINE_EPOCH_NS:-}" ]]; then
    printf '%s\n' "${SEMANTIST_BUILD_TIME_BUDGET_S:-120}"
    return
  fi
  python3 - "${SEMANTIST_BUILD_DEADLINE_EPOCH_NS}" <<'PY'
import sys
import time

deadline = int(sys.argv[1])
remaining = (deadline - time.time_ns()) / 1_000_000_000
print(f"{max(0.01, remaining):.6f}")
PY
}

ST_FILE="${1:-${ST_FILE:-benchmarks/oscat/targets/oscat_basic_inc2/target.st}}"
ST_FUNCTION="${2:-${ST_FUNCTION:-$(basename "$ST_FILE" .st)}}"
TARGET_NAME="$(echo "$ST_FUNCTION" | tr '[:upper:]' '[:lower:]')"
SEMANTIST_ARTIFACT_DIR="${SEMANTIST_ARTIFACT_DIR:-artifacts}"
SEMANTIST_BUILD_ARTIFACT_DIR="${SEMANTIST_BUILD_ARTIFACT_DIR:-${SEMANTIST_ARTIFACT_DIR}/build}"
OUT_LL="${OUT_LL:-${SEMANTIST_BUILD_ARTIFACT_DIR}/ir/${TARGET_NAME}.ll}"
OUT_OBJ="${OUT_OBJ:-${SEMANTIST_BUILD_ARTIFACT_DIR}/objects/${TARGET_NAME}.o}"
OUT_BIN="${OUT_BIN:-${SEMANTIST_BUILD_ARTIFACT_DIR}/targets/fuzz_target}"
HARNESS_OUT="${HARNESS_OUT:-${SEMANTIST_BUILD_ARTIFACT_DIR}/harness/harness.c}"
PREPARED_LL="${PREPARED_LL:-${OUT_LL%.ll}.prepared.ll}"
STATE_LL="${STATE_LL:-${PREPARED_LL%.ll}.state.ll}"

SEMANTIC_MODE="${SEMANTIST_SEMANTIC_INSTRUMENTATION:-ir}"
if [[ "$SEMANTIC_MODE" != "ir" && "$SEMANTIC_MODE" != "off" ]]; then
  echo "[build_target] SEMANTIST_SEMANTIC_INSTRUMENTATION must be ir or off" >&2
  exit 2
fi
if [[ "${SEMANTIC_MODE}" == "ir" ]]; then
  SEMANTIST_LLVM_BIN="${SEMANTIST_SEMANTIC_LLVM_BIN:-/usr/lib/llvm-21/bin}"
else
  SEMANTIST_LLVM_BIN="${SEMANTIST_LLVM_BIN:-/usr/lib/llvm-21/bin}"
fi
LLVM_CLANG="${SEMANTIST_LLVM_BIN}/clang"
if [[ ! -x "$LLVM_CLANG" ]]; then
  echo "[build_target] missing clang in SEMANTIST_LLVM_BIN=$SEMANTIST_LLVM_BIN" >&2
  exit 1
fi
AFL_RUNTIME="${SEMANTIST_AFL_RUNTIME:-/usr/local/lib/afl/afl-compiler-rt.o}"
if [[ ! -r "$AFL_RUNTIME" ]]; then
  echo "[build_target] missing AFL runtime: $AFL_RUNTIME" >&2
  exit 1
fi
RUSTY_STDLIB_MANIFEST="${SEMANTIST_RUSTY_STDLIB_MANIFEST:-$ROOT/artifacts/rusty-semantic/libs/stdlib/Cargo.toml}"
RUSTY_STDLIB_LIB="${SEMANTIST_RUSTY_STDLIB_LIB:-${CARGO_TARGET_DIR:-${SEMANTIST_ARTIFACT_DIR}/cargo-target}/release/libiec61131std.a}"
if [[ -n "${SEMANTIST_RUSTY_STDLIB_VCLOCK_LIB:-}" ]]; then
  if [[ -r "${SEMANTIST_RUSTY_STDLIB_VCLOCK_LIB}" ]]; then
    RUSTY_STDLIB_LIB="${SEMANTIST_RUSTY_STDLIB_VCLOCK_LIB}"
    echo "[build_target] using virtual-clock standard library: $RUSTY_STDLIB_LIB"
  else
    echo "[build_target] SEMANTIST_RUSTY_STDLIB_VCLOCK_LIB is not readable: $SEMANTIST_RUSTY_STDLIB_VCLOCK_LIB" >&2
    exit 1
  fi
fi
if [[ ! -r "$RUSTY_STDLIB_LIB" ]]; then
  if [[ ! -r "$RUSTY_STDLIB_MANIFEST" ]]; then
    echo "[build_target] missing RuSTy standard library manifest: $RUSTY_STDLIB_MANIFEST" >&2
    exit 1
  fi
  echo "[build_target] building RuSTy IEC standard library"
  CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-${SEMANTIST_ARTIFACT_DIR}/cargo-target}" \
    cargo build --manifest-path "$RUSTY_STDLIB_MANIFEST" --release
fi
read -r -a AFL_COVERAGE_FLAGS <<< "${SEMANTIST_AFL_COVERAGE_FLAGS:--fsanitize-coverage=trace-pc-guard}"
read -r -a TARGET_SANITIZER_FLAGS <<< "${SEMANTIST_TARGET_SANITIZERS:--fsanitize=address,undefined -fno-omit-frame-pointer}"

echo "[build_target] using clang: $LLVM_CLANG"
echo "[build_target] using LLVM toolchain: $SEMANTIST_LLVM_BIN"
echo "[build_target] using AFL runtime: $AFL_RUNTIME"
echo "[build_target] using RuSTy IEC standard library: $RUSTY_STDLIB_LIB"
if [[ ${#TARGET_SANITIZER_FLAGS[@]} -gt 0 ]]; then
  echo "[build_target] using target sanitizers: ${TARGET_SANITIZER_FLAGS[*]}"
fi

# The harness ABI comes from this build's IR and STG model, never from the ST text.
STG_MODEL="${SEMANTIST_STG_DIR:-${SEMANTIST_BUILD_ARTIFACT_DIR}/stg/${TARGET_NAME}}/stg-model.json"
if [[ ! -r "$STG_MODEL" ]]; then
  echo "[build_target] missing STG model $STG_MODEL; run compile_st.sh with SEMANTIST_SEMANTIC_INSTRUMENTATION=ir" >&2
  exit 1
fi
GENERATED_SEED_DIR="${GENERATED_SEED_DIR:-${SEMANTIST_BUILD_ARTIFACT_DIR}/corpus/${TARGET_NAME}/seeds}"
STRING_BYTES="any"
if [[ -n "${SEMANTIST_COMPATIBILITY_MANIFEST:-}" ]]; then
  # The dialect's execution settings decide which bytes STRING inputs may hold.
  STRING_BYTES="$(python3 -c 'import sys; from compiler.toolchain.compatibility import load_project; print(load_project(sys.argv[1]).string_bytes)' "$SEMANTIST_COMPATIBILITY_MANIFEST")"
fi
python3 -m compiler.harness.generator "$ST_FUNCTION" --ir "$OUT_LL" --stg-model "$STG_MODEL" \
  --out "$HARNESS_OUT" --seed-out "$GENERATED_SEED_DIR/seed1" --string-bytes "$STRING_BYTES" \
  --frame-header-out "$(dirname "$HARNESS_OUT")/semantist_frame.h"
PREPARE_ARGS=(
  "$OUT_LL"
  "$ST_FUNCTION"
  --output-ll "$PREPARED_LL"
  --llvm-bin "$SEMANTIST_LLVM_BIN"
)
python3 -m compiler.scripts.prepare_target_ir "${PREPARE_ARGS[@]}"
LOOPS_LL="${PREPARED_LL%.ll}.loops.ll"
python3 -m compiler.instrumentation.loop_budget "$PREPARED_LL" --output "$LOOPS_LL"
PREPARED_LL="$LOOPS_LL"

mkdir -p "$(dirname "$OUT_OBJ")" "$(dirname "$OUT_BIN")"
# The harness restores the target's writable globals before every input, so a
# persistent or in-process loop sees the state of a fresh process. Only globals
# still writable after this compile's own optimization are recorded.
OPTIMIZED_LL="${STATE_LL%.ll}.optimized.ll"
"$LLVM_CLANG" -O1 "${AFL_COVERAGE_FLAGS[@]}" "${TARGET_SANITIZER_FLAGS[@]}" -S -emit-llvm "$PREPARED_LL" -o "$OPTIMIZED_LL"
python3 -m compiler.harness.state_table "$PREPARED_LL" --optimized "$OPTIMIZED_LL" --output "$STATE_LL"
echo "[build_target] compiling LLVM IR to AFL-compatible instrumented object"
"$LLVM_CLANG" -g -O1 "${AFL_COVERAGE_FLAGS[@]}" "${TARGET_SANITIZER_FLAGS[@]}" -c "$STATE_LL" -o "$OUT_OBJ"

LINK_OBJECTS=(
  "$HARNESS_OUT"
  "$OUT_OBJ"
  "$AFL_RUNTIME"
  "$RUSTY_STDLIB_LIB"
)
if [[ -n "${SEMANTIST_COMPATIBILITY_MANIFEST:-}" ]]; then
  COMPATIBILITY_BUILD_DIR="${SEMANTIST_COMPATIBILITY_BUILD_DIR:-${SEMANTIST_BUILD_ARTIFACT_DIR}/compatibility/${TARGET_NAME}}"
  mkdir -p "$COMPATIBILITY_BUILD_DIR"
  COMPATIBILITY_LINK_FILE="$COMPATIBILITY_BUILD_DIR/link-inputs.txt"
  COMPATIBILITY_ARGS=(
    --manifest "${SEMANTIST_COMPATIBILITY_MANIFEST}"
    --build-dir "$COMPATIBILITY_BUILD_DIR"
    --clang "$LLVM_CLANG"
  )
  for flag in "${TARGET_SANITIZER_FLAGS[@]}"; do
    COMPATIBILITY_ARGS+=("--compile-arg=$flag")
  done
  python3 -m compiler.toolchain.compatibility.native \
    "${COMPATIBILITY_ARGS[@]}" > "$COMPATIBILITY_LINK_FILE"
  mapfile -t COMPATIBILITY_LINK_INPUTS < "$COMPATIBILITY_LINK_FILE"
  LINK_OBJECTS+=("${COMPATIBILITY_LINK_INPUTS[@]}")
fi
if [[ "${SEMANTIC_MODE}" == "ir" ]]; then
  SEMANTIC_RUNTIME_OBJ="${OUT_OBJ%.o}.semantic-runtime.o"
  "$LLVM_CLANG" -g -O2 -c compiler/instrumentation/runtime/semantic_coverage.c -o "$SEMANTIC_RUNTIME_OBJ"
  LINK_OBJECTS+=("$SEMANTIC_RUNTIME_OBJ")
fi

CONTRACT_DEFINES=(
  "-DSEMANTIST_SCAN_PERIOD_NS=${SEMANTIST_SCAN_PERIOD_NS:-10000000}ULL"
  "-DSEMANTIST_TIMER_SEMANTICS=${SEMANTIST_TIMER_SEMANTICS:-1}"
  "-DSEMANTIST_CLOCK_ORIGIN_NS=${SEMANTIST_CLOCK_ORIGIN_NS:-0}ULL"
  "-DSEMANTIST_LOOP_BACKEDGES_PER_SCAN=${SEMANTIST_LOOP_BACKEDGES_PER_SCAN:-1000000}ULL"
)
"$LLVM_CLANG" -g -O1 "${CONTRACT_DEFINES[@]}" "${AFL_COVERAGE_FLAGS[@]}" "${TARGET_SANITIZER_FLAGS[@]}" "${LINK_OBJECTS[@]}" -ldl -lpthread -lm -o "$OUT_BIN"

# The in-process fuzzer links the same harness, target object and libraries
# with the engine library instead of the AFL++ runtime; the engine provides
# coverage guards and the semantic buffers (see fuzzer/engine/src/inprocess.rs).
# The image normally contains a release archive, but using an archive older
# than the checked-out engine silently drops deadline, task-budget, or cleanup
# fixes. Refresh the default archive when engine/workspace sources are newer;
# callers that explicitly provide SEMANTIST_INPROCESS_LIB own that artifact.
ENGINE_TARGET_DIR="${CARGO_TARGET_DIR:-${SEMANTIST_ARTIFACT_DIR}/cargo-target}"
ENGINE_REQUIRED="${SEMANTIST_INPROCESS_REQUIRED:-0}"
if [[ -z "${SEMANTIST_INPROCESS_LIB:-}" && ( "$ENGINE_REQUIRED" == "1" || -r "${ENGINE_TARGET_DIR}/release/libsemantist_inprocess.a" ) ]]; then
  DEFAULT_INPROCESS_LIB="${ENGINE_TARGET_DIR}/release/libsemantist_inprocess.a"
  if [[ ! -r "$DEFAULT_INPROCESS_LIB" ]] ||
     find Cargo.toml Cargo.lock fuzzer/engine -type f -newer "$DEFAULT_INPROCESS_LIB" -print -quit | grep -q .; then
    echo "[build_target] rebuilding SemantiST in-process engine"
    CARGO_TARGET_DIR="$ENGINE_TARGET_DIR" cargo build --manifest-path "$ROOT/Cargo.toml" \
      --release --locked -p semantist --lib --jobs "${SEMANTIST_ENGINE_BUILD_JOBS:-1}"
  fi
  if [[ -r "$DEFAULT_INPROCESS_LIB" ]]; then
    SEMANTIST_INPROCESS_LIB="$DEFAULT_INPROCESS_LIB"
  elif [[ "$ENGINE_REQUIRED" == "1" ]]; then
    echo "[build_target] required in-process engine archive was not produced: $DEFAULT_INPROCESS_LIB" >&2
    exit 1
  fi
fi
if [[ -n "${SEMANTIST_INPROCESS_LIB:-}" ]]; then
  if [[ ! -r "$SEMANTIST_INPROCESS_LIB" ]]; then
    echo "[build_target] missing in-process engine library: $SEMANTIST_INPROCESS_LIB" >&2
    exit 1
  fi
  if [[ "${SEMANTIC_MODE}" != "ir" ]]; then
    echo "[build_target] in-process fuzzing requires SEMANTIST_SEMANTIC_INSTRUMENTATION=ir" >&2
    exit 2
  fi
  INPROCESS_BIN="${INPROCESS_BIN:-$(dirname "$OUT_BIN")/semantist-inprocess}"
  INPROCESS_RUNTIME_OBJ="${OUT_OBJ%.o}.semantic-runtime-inprocess.o"
  "$LLVM_CLANG" -g -O2 -DSEMANTIST_IN_PROCESS -c compiler/instrumentation/runtime/semantic_coverage.c -o "$INPROCESS_RUNTIME_OBJ"
  INPROCESS_OBJECTS=()
  for object in "${LINK_OBJECTS[@]}"; do
    if [[ "$object" != "$AFL_RUNTIME" && "$object" != "${SEMANTIC_RUNTIME_OBJ:-}" ]]; then
      INPROCESS_OBJECTS+=("$object")
    fi
  done
  # The engine and the RuSTy standard library are both Rust static libraries
  # from the same toolchain and both define Rust's allocator shim. The engine
  # comes first so its definition, which keeps Rust allocations out of ASan's
  # allocator, serves both. LibAFL symbolizes a backtrace for every crash it
  # catches; without debug sections that takes milliseconds, not tens of them.
  # Sanitizer reports use module offsets (symbolize=0), and replay uses
  # $OUT_BIN, which keeps its debug information.
  "$LLVM_CLANG" -g -O1 -DSEMANTIST_IN_PROCESS "${CONTRACT_DEFINES[@]}" "${AFL_COVERAGE_FLAGS[@]}" "${TARGET_SANITIZER_FLAGS[@]}" \
    "$SEMANTIST_INPROCESS_LIB" "${INPROCESS_OBJECTS[@]}" "$INPROCESS_RUNTIME_OBJ" \
    -Wl,--allow-multiple-definition -Wl,--strip-debug -lgcc_s -lutil -lrt -lpthread -lm -ldl -o "$INPROCESS_BIN"
  echo "[build_target] wrote $INPROCESS_BIN"
fi

BUNDLE_DOC_DIR="${SEMANTIST_BUNDLE_DIR:-$(dirname "$OUT_BIN")/bundle}"
mkdir -p "$BUNDLE_DOC_DIR"
BUNDLE_ARGS=(
  "$ST_FUNCTION"
  --ir "$OUT_LL"
  --state-ir "$STATE_LL"
  --stg-model "$STG_MODEL"
  --out "$BUNDLE_DOC_DIR"
  --st-file "$ST_FILE"
  --target-binary "$OUT_BIN"
  --scan-period-ns "${SEMANTIST_SCAN_PERIOD_NS:-10000000}"
  --timer-semantics "${SEMANTIST_TIMER_SEMANTICS_NAME:-iec_ge}"
  --loop-backedges-per-scan "${SEMANTIST_LOOP_BACKEDGES_PER_SCAN:-1000000}"
  --clock-origin-ns "${SEMANTIST_CLOCK_ORIGIN_NS:-0}"
)
if [[ -n "${SEMANTIST_COMPATIBILITY_MANIFEST:-}" ]]; then
  BUNDLE_ARGS+=(--compatibility-manifest "$SEMANTIST_COMPATIBILITY_MANIFEST")
fi
python3 -m compiler.harness.bundle "${BUNDLE_ARGS[@]}"

SYMBOLIC_STATUS="unsupported"
SYMBOLIC_REASON="not built"
if [[ "${SEMANTIST_SYMBOLIC:-1}" == "1" ]]; then
  KLEE_BUILD_OUT="${SEMANTIST_KLEE_BUILD_OUT:-$BUNDLE_DOC_DIR}"
  SYMBOLIC_TIME_BUDGET="$(build_remaining_seconds)"
  python3 -m compiler.symbolic.build \
    --build-dir "$(dirname "$OUT_BIN")" \
    --harness "$HARNESS_OUT" \
    --state-ir "$STATE_LL" \
    --function "$ST_FUNCTION" \
    --out "$KLEE_BUILD_OUT" \
    --abi "$BUNDLE_DOC_DIR/abi.json" \
    --time-budget-s "$SYMBOLIC_TIME_BUDGET"
  if [[ -r "$KLEE_BUILD_OUT/klee/manifest.json" ]]; then
    SYMBOLIC_STATUS="supported"
    SYMBOLIC_REASON=""
  else
    if [[ -r "$KLEE_BUILD_OUT/klee/UNSUPPORTED.json" ]]; then
      SYMBOLIC_REASON="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("status", "unsupported") + ": " + d.get("reason", "KLEE build failed"))' "$KLEE_BUILD_OUT/klee/UNSUPPORTED.json")"
    else
      SYMBOLIC_REASON="KLEE build did not produce a supported manifest"
    fi
  fi
fi

# The first bundle pass supplies abi.json to the KLEE builder. Rewrite the
# contract afterwards so its capability and contract_id match the actual KLEE
# result used by the campaign.
python3 -m compiler.harness.bundle "${BUNDLE_ARGS[@]}" \
  --symbolic "$SYMBOLIC_STATUS" --symbolic-reason "$SYMBOLIC_REASON"

echo "[build_target] function: $ST_FUNCTION"
echo "[build_target] wrote $OUT_BIN"
