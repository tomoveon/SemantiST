#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

SEMANTIST_ARTIFACT_DIR="${SEMANTIST_ARTIFACT_DIR:-artifacts}"
SEMANTIST_BUILD_ARTIFACT_DIR="${SEMANTIST_BUILD_ARTIFACT_DIR:-${SEMANTIST_ARTIFACT_DIR}/build}"
SEMANTIST_CLEAN_BUILD="${SEMANTIST_CLEAN_BUILD:-0}"
SEMANTIST_CLEAN_TOOLCHAIN="${SEMANTIST_CLEAN_TOOLCHAIN:-0}"

rm -rf "${SEMANTIST_ARTIFACT_DIR}/runs"
rm -rf "${SEMANTIST_ARTIFACT_DIR}/benchmarks"

if [[ "$SEMANTIST_CLEAN_BUILD" == "1" ]]; then
  if [[ -d "$SEMANTIST_BUILD_ARTIFACT_DIR" ]]; then
    find "$SEMANTIST_BUILD_ARTIFACT_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
  fi
fi

if [[ "$SEMANTIST_CLEAN_TOOLCHAIN" == "1" ]]; then
  rm -rf "${SEMANTIST_ARTIFACT_DIR}/cargo-target"
fi

# Clean legacy root-level run locations from older layouts too.
if [[ "$SEMANTIST_CLEAN_BUILD" == "1" && -d build ]]; then
  find build -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
fi

rm -rf runs
rm -rf corpus
rm -rf crashes
rm -rf findings
if [[ "$SEMANTIST_CLEAN_BUILD" == "1" ]]; then
  rm -rf ir
  rm -rf harness
fi
rm -rf .cur_input_*
find fuzzer compiler tests -type d -name __pycache__ -prune -exec rm -rf -- {} +
find fuzzer compiler tests -type d -name .pytest_cache -prune -exec rm -rf -- {} +
