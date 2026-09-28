#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export LLVM_SYS_211_PREFIX="${LLVM_SYS_211_PREFIX:-/usr/lib/llvm-21}"

STG_BIN="${SEMANTIST_STG_BIN:-${CARGO_TARGET_DIR:-${ROOT}/target}/release/semantist-stg}"
if [[ -x "${STG_BIN}" ]]; then
  exec "${STG_BIN}" "$@"
fi

exec cargo run \
  --quiet \
  --locked \
  --release \
  --jobs 1 \
  --manifest-path "${ROOT}/compiler/stg/Cargo.toml" \
  --bin semantist-stg \
  -- "$@"
