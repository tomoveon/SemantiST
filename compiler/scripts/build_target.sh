#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

ST_FILE="${1:-${ST_FILE:-benchmarks/oscat_basic/functions/inc2/target.st}}"
ST_FUNCTION="${2:-${ST_FUNCTION:-$(basename "$ST_FILE" .st)}}"
TARGET_NAME="$(echo "$ST_FUNCTION" | tr '[:upper:]' '[:lower:]')"
SEMANTIST_ARTIFACT_DIR="${SEMANTIST_ARTIFACT_DIR:-artifacts}"
SEMANTIST_BUILD_ARTIFACT_DIR="${SEMANTIST_BUILD_ARTIFACT_DIR:-${SEMANTIST_ARTIFACT_DIR}/build}"
OUT_LL="${OUT_LL:-${SEMANTIST_BUILD_ARTIFACT_DIR}/ir/${TARGET_NAME}.ll}"
OUT_OBJ="${OUT_OBJ:-${SEMANTIST_BUILD_ARTIFACT_DIR}/objects/${TARGET_NAME}.o}"
OUT_BIN="${OUT_BIN:-${SEMANTIST_BUILD_ARTIFACT_DIR}/targets/fuzz_target}"
HARNESS_OUT="${HARNESS_OUT:-${SEMANTIST_BUILD_ARTIFACT_DIR}/harness/harness.c}"
PREPARED_LL="${PREPARED_LL:-${OUT_LL%.ll}.prepared.ll}"

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

python3 -m compiler.scripts.generate_harness "$ST_FILE" "$ST_FUNCTION"
PREPARE_ARGS=(
  "$OUT_LL"
  "$ST_FUNCTION"
  --output-ll "$PREPARED_LL"
  --llvm-bin "$SEMANTIST_LLVM_BIN"
)
python3 -m compiler.scripts.prepare_target_ir "${PREPARE_ARGS[@]}"

mkdir -p "$(dirname "$OUT_OBJ")" "$(dirname "$OUT_BIN")"
echo "[build_target] compiling LLVM IR to AFL-compatible instrumented object"
"$LLVM_CLANG" -g -O1 "${AFL_COVERAGE_FLAGS[@]}" "${TARGET_SANITIZER_FLAGS[@]}" -c "$PREPARED_LL" -o "$OUT_OBJ"

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

"$LLVM_CLANG" -g -O1 "${AFL_COVERAGE_FLAGS[@]}" "${TARGET_SANITIZER_FLAGS[@]}" "${LINK_OBJECTS[@]}" -ldl -lpthread -lm -o "$OUT_BIN"

echo "[build_target] function: $ST_FUNCTION"
echo "[build_target] wrote $OUT_BIN"
