#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export LLVM_SYS_211_PREFIX="${LLVM_SYS_211_PREFIX:-/usr/lib/llvm-21}"

INSTRUMENT_BIN="${SEMANTIST_STG_IR_INSTRUMENT_BIN:-${CARGO_TARGET_DIR:-${ROOT}/target}/release/stg-ir-instrument}"
if [[ -x "${INSTRUMENT_BIN}" ]]; then
  exec "${INSTRUMENT_BIN}" "$@"
fi

exec cargo run \
  --quiet \
  --locked \
  --release \
  --jobs 1 \
  --manifest-path "${ROOT}/compiler/stg/Cargo.toml" \
  --bin stg-ir-instrument \
  -- "$@"
