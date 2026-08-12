#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

ST_FILE="${1:-${ST_FILE:-benchmarks/oscat_basic/functions/inc2/target.st}}"
ST_FUNCTION="${2:-${ST_FUNCTION:-$(basename "$ST_FILE" .st)}}"
ST_LIBRARY="${3:-${SEMANTIST_LIBRARY_FILE:-}}"
ST_STUBS="${4:-${SEMANTIST_LIBRARY_STUBS:-}}"
ST_COMPATIBILITY_MANIFEST="${SEMANTIST_COMPATIBILITY_MANIFEST:-}"
TARGET_NAME="$(echo "$ST_FUNCTION" | tr '[:upper:]' '[:lower:]')"
SEMANTIST_ARTIFACT_DIR="${SEMANTIST_ARTIFACT_DIR:-artifacts}"
SEMANTIST_BUILD_ARTIFACT_DIR="${SEMANTIST_BUILD_ARTIFACT_DIR:-${SEMANTIST_ARTIFACT_DIR}/build}"
OUT_LL="${OUT_LL:-${SEMANTIST_BUILD_ARTIFACT_DIR}/ir/${TARGET_NAME}.ll}"
mkdir -p "$(dirname "$OUT_LL")"

SEMANTIC_MODE="${SEMANTIST_SEMANTIC_INSTRUMENTATION:-ir}"
if [[ "$SEMANTIC_MODE" != "ir" && "$SEMANTIC_MODE" != "off" ]]; then
  echo "[compile_st] SEMANTIST_SEMANTIC_INSTRUMENTATION must be ir or off" >&2
  exit 2
fi
if [[ "${SEMANTIC_MODE}" == "ir" ]]; then
  COMPILER="${RUSTY_COMPILER:-${ROOT}/artifacts/rusty-semantic/target/release/plc}"
  if [[ ! -x "${COMPILER}" ]]; then
    COMPILER="$(compiler/scripts/build_rusty_semantic.sh | tail -n 1)"
  fi
else
  COMPILER="${RUSTY_COMPILER:-${ROOT}/artifacts/rusty-semantic/target/release/plc}"
fi

echo "[compile_st] using compiler: $COMPILER"
COMPILE_ARGS=(
  "$ST_FILE"
  "$ST_FUNCTION"
  --out-ll "$OUT_LL"
  --plc "$COMPILER"
)
if [[ -n "$ST_LIBRARY" ]]; then
  COMPILE_ARGS+=(--library "$ST_LIBRARY")
fi
if [[ -n "$ST_STUBS" ]]; then
  COMPILE_ARGS+=(--stubs "$ST_STUBS")
fi
if [[ -n "$ST_COMPATIBILITY_MANIFEST" ]]; then
  COMPILE_ARGS+=(--compatibility-manifest "$ST_COMPATIBILITY_MANIFEST")
fi
if [[ "${SEMANTIC_MODE}" == "ir" ]]; then
  COMPILE_ARGS+=(--semantic-ir)
  if [[ -n "${SEMANTIST_STG_DIR:-}" ]]; then
    COMPILE_ARGS+=(--stg-output "${SEMANTIST_STG_DIR}")
  fi
fi
python3 -m compiler.scripts.compile_st_project "${COMPILE_ARGS[@]}"

if [[ "${SEMANTIC_MODE}" != "ir" ]] && grep -q 'captures(none)' "$OUT_LL"; then
  perl -0pi -e 's/\s+captures\(none\)//g' "$OUT_LL"
fi

echo "[compile_st] function: $ST_FUNCTION"
echo "[compile_st] wrote $OUT_LL"
