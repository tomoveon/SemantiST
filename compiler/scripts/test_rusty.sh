#!/usr/bin/env bash
# Run RuSTy's own test suites (cargo test --workspace and lit) on the patched
# tree that build_rusty_semantic.sh produced, e.g. /opt/rusty-semantic in the
# toolchain image.
#
# Test-only backports from later upstream revisions
# (compiler/toolchain/rusty-test-backports/) are applied to the tree first.
# They change test expectations only, never compiler sources, so the binary
# and the patch series recorded in TargetIdentity are unchanged. Run this in a
# throw-away container (podman run --rm ...) or on a tree you own.
#
# lit is not part of the image: pass the wheel of lit 18.1.8 (pure Python,
# `pip download lit==18.1.8`) as SEMANTIST_LIT_WHEEL, or have `lit` importable.
#
# cargo fetches RuSTy's dev-dependencies, which the image does not cache:
# give the container network access for this step (build time, not intake).
# lit runs 8 jobs by default; one job per CPU of a large host makes plc fail
# to start its thread pool under the container's process limit.
#
# Usage: test_rusty.sh [--cargo-log PATH] [--lit-log PATH] [lit args...]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SOURCE="${SEMANTIST_RUSTY_SEMANTIC_DIR:-/opt/rusty-semantic}"
TARGET="${SEMANTIST_RUSTY_CARGO_TARGET_DIR:-${SOURCE}/target}"
BACKPORTS="${ROOT}/compiler/toolchain/rusty-test-backports"
CARGO_LOG=/dev/stdout
LIT_LOG=/dev/stdout
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cargo-log) CARGO_LOG="$2"; shift 2 ;;
    --lit-log) LIT_LOG="$2"; shift 2 ;;
    *) break ;;
  esac
done

for patch in $(find "${BACKPORTS}" -maxdepth 1 -name '*.patch' | sort); do
  if git -C "${SOURCE}" apply --reverse --check "${patch}" 2>/dev/null; then
    echo "[test_rusty] already applied $(basename "${patch}")"
  else
    git -C "${SOURCE}" apply "${patch}"
    echo "[test_rusty] applied $(basename "${patch}")"
  fi
done

LIT_ARGS=("$@")
[[ ${#LIT_ARGS[@]} -gt 0 ]] || LIT_ARGS=(-j8 tests/lit)

status=0
(cd "${SOURCE}" && CARGO_TARGET_DIR="${TARGET}" cargo test --workspace) > "${CARGO_LOG}" 2>&1 || status=1

# Same packaging as upstream scripts/build.sh (create_shared_library): the
# shared library must contain the ST-compiled stdlib objects of the archive.
LIB="$(mktemp -d)"
mkdir -p "${LIB}/lib" "${LIB}/include"
cp "${TARGET}/release/libiec61131std.a" "${LIB}/lib/"
clang --shared -L"${LIB}/lib" -Wl,--whole-archive "${LIB}/lib/libiec61131std.a" -Wl,--no-whole-archive \
  -o "${LIB}/lib/libiec61131std.so" -lm -fuse-ld=lld
cp "${SOURCE}"/libs/stdlib/iec61131-st/*.st "${LIB}/include/"
LIT_PATH=""
if ! python3 -c 'import lit' 2>/dev/null; then
  if [[ -z "${SEMANTIST_LIT_WHEEL:-}" ]]; then
    echo "[test_rusty] lit is not importable and SEMANTIST_LIT_WHEEL is not set" >&2
    exit 2
  fi
  LIT_PATH="$(mktemp -d)"
  python3 -m pip install --no-index --no-deps --target "${LIT_PATH}" "${SEMANTIST_LIT_WHEEL}" -q
fi
(cd "${SOURCE}" && PYTHONPATH="${LIT_PATH}" python3 -c 'import sys, lit.main; sys.argv[0] = "lit"; lit.main.main()' \
  -DLIB="${LIB}" -DCOMPILER="${TARGET}/release/plc" "${LIT_ARGS[@]}") > "${LIT_LOG}" 2>&1 || status=1
exit "${status}"
