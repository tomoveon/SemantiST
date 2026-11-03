#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export LLVM_SYS_211_PREFIX="${LLVM_SYS_211_PREFIX:-/usr/lib/llvm-21}"

if [[ -n "${SEMANTIST_STG_BIN:-}" ]]; then
  exec "${SEMANTIST_STG_BIN}" "$@"
fi

STG_BIN="${CARGO_TARGET_DIR:-${ROOT}/target}/release/semantist-stg"
cargo build \
  --quiet \
  --locked \
  --release \
  --jobs 1 \
  --manifest-path "${ROOT}/compiler/stg/Cargo.toml" \
  --bin semantist-stg
exec "${STG_BIN}" "$@"
