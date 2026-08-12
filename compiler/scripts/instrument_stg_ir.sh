#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export LLVM_SYS_211_PREFIX="${LLVM_SYS_211_PREFIX:-/usr/lib/llvm-21}"

exec cargo run \
  --quiet \
  --jobs 1 \
  --manifest-path "${ROOT}/compiler/stg/Cargo.toml" \
  --bin stg-ir-instrument \
  -- "$@"
