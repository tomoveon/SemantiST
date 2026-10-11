#!/usr/bin/env bash
set -euo pipefail

PREPARED_LL="${PREPARED_LL:?PREPARED_LL is required}"
OUT_OBJ="${OUT_OBJ:?OUT_OBJ is required}"
OUT_BIN="${OUT_BIN:?OUT_BIN is required}"
HARNESS_OUT="${HARNESS_OUT:?HARNESS_OUT is required}"
RUSTY_STDLIB_OUT="${RUSTY_STDLIB_OUT:?RUSTY_STDLIB_OUT is required}"
AFL_CC="${AFL_CC:-$(command -v afl-clang-fast)}"
AFL_LL="${OUT_OBJ%.o}.afl.ll"

mkdir -p "$(dirname "${OUT_OBJ}")" "$(dirname "${OUT_BIN}")"
cp "${PREPARED_LL}" "${AFL_LL}"
# Be defensive when consuming cached prepared IR generated before the final
# LLVM 16 compatibility normalization in prepare_target_ir.
perl -0pi -e 's/\s+captures\(none\)//g; s/getelementptr inbounds nuw/getelementptr inbounds/g' "${AFL_LL}"

read -r -a SANITIZER_FLAGS <<< "${SEMANTIST_TARGET_SANITIZERS:--fsanitize=address -fno-omit-frame-pointer}"
"${AFL_CC}" -g -O1 "${SANITIZER_FLAGS[@]}" -c "${AFL_LL}" -o "${OUT_OBJ}"

LINK_INPUTS=("${HARNESS_OUT}" "${OUT_OBJ}" "${RUSTY_STDLIB_OUT}")
if [[ -n "${SEMANTIST_COMPATIBILITY_MANIFEST:-}" ]]; then
  COMPATIBILITY_BUILD_DIR="${SEMANTIST_COMPATIBILITY_BUILD_DIR:-$(dirname "${OUT_OBJ}")/compatibility}"
  COMPATIBILITY_LINK_FILE="${COMPATIBILITY_BUILD_DIR}/link-inputs.txt"
  mkdir -p "${COMPATIBILITY_BUILD_DIR}"
  python3 -m compiler.toolchain.compatibility.native \
    --manifest "${SEMANTIST_COMPATIBILITY_MANIFEST}" \
    --build-dir "${COMPATIBILITY_BUILD_DIR}" \
    --clang "${AFL_CC}" \
    --compile-arg=-fsanitize=address \
    --compile-arg=-fno-omit-frame-pointer > "${COMPATIBILITY_LINK_FILE}"
  mapfile -t COMPATIBILITY_LINK_INPUTS < "${COMPATIBILITY_LINK_FILE}"
  LINK_INPUTS+=("${COMPATIBILITY_LINK_INPUTS[@]}")
fi

"${AFL_CC}" -g -O1 "${SANITIZER_FLAGS[@]}" \
  "${LINK_INPUTS[@]}" \
  -no-pie \
  -Wl,--allow-multiple-definition \
  -ldl -lpthread -lm \
  -o "${OUT_BIN}"

test -x "${OUT_BIN}"
echo "[aflplusplus-build] wrote ${OUT_BIN}"
