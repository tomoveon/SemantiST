#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FIXTURE="${ROOT}/compiler/stg/tests/fixtures/control_flow.st"
ORIGINAL="${SEMANTIST_ORIGINAL_RUSTY_COMPILER:-/opt/rusty/target/release/plc}"
PATCHED="${RUSTY_COMPILER:-${ROOT}/artifacts/rusty-semantic/target/release/plc}"
if [[ ! -x "${PATCHED}" && -x /opt/rusty/target/debug/plc ]]; then
  PATCHED=/opt/rusty/target/debug/plc
fi
if [[ ! -x "${ORIGINAL}" || ! -x "${PATCHED}" ]]; then
  echo "[rusty_semantic_off] original and patched RuSTy compilers are required" >&2
  exit 77
fi

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

env -u SEMANTIST_STG_CODEGEN_MAP \
  "${ORIGINAL}" "${FIXTURE}" \
  --ir --single-module --error-format none -O none \
  -o "${WORK}/original.ll"
env -u SEMANTIST_STG_CODEGEN_MAP \
  "${PATCHED}" "${FIXTURE}" \
  --ir --single-module --error-format none -O none \
  -o "${WORK}/patched.ll"

sed '/^; ModuleID =/d' "${WORK}/original.ll" >"${WORK}/original.normalized.ll"
sed '/^; ModuleID =/d' "${WORK}/patched.ll" >"${WORK}/patched.normalized.ll"
cmp "${WORK}/original.normalized.ll" "${WORK}/patched.normalized.ll"

echo "[rusty_semantic_off] byte-equivalent LLVM IR with instrumentation disabled"
