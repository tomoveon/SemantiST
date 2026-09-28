#!/usr/bin/env bash
set -euo pipefail

FUNCTION="${1:?usage: prepare_baseline_target.sh FUNCTION}"
OUT_LL="${OUT_LL:?OUT_LL is required}"
PREPARED_LL="${PREPARED_LL:?PREPARED_LL is required}"
HARNESS_OUT="${HARNESS_OUT:?HARNESS_OUT is required}"
SEMANTIST_HARNESS_SOURCE="${SEMANTIST_HARNESS_SOURCE:?SEMANTIST_HARNESS_SOURCE is required}"
RUSTY_STDLIB_OUT="${RUSTY_STDLIB_OUT:?RUSTY_STDLIB_OUT is required}"
RUSTY_STDLIB_LIB="${SEMANTIST_RUSTY_STDLIB_LIB:-/opt/rusty-semantic/target/release/libiec61131std.a}"
LLVM_BIN="${SEMANTIST_LLVM_BIN:-/usr/lib/llvm-21/bin}"

mkdir -p \
  "$(dirname "${PREPARED_LL}")" \
  "$(dirname "${HARNESS_OUT}")" \
  "$(dirname "${RUSTY_STDLIB_OUT}")"

python3 -m compiler.scripts.prepare_target_ir \
  "${OUT_LL}" \
  "${FUNCTION}" \
  --output-ll "${PREPARED_LL}" \
  --llvm-bin "${LLVM_BIN}"

cp "${SEMANTIST_HARNESS_SOURCE}" "${HARNESS_OUT}"
cp "${RUSTY_STDLIB_LIB}" "${RUSTY_STDLIB_OUT}"

echo "[prepare_baseline_target] wrote ${PREPARED_LL}"
echo "[prepare_baseline_target] copied ${HARNESS_OUT}"
echo "[prepare_baseline_target] copied ${RUSTY_STDLIB_OUT}"
