#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

TIME_PER_TARGET="${2:-${SEMANTIST_BENCH_TIME_PER_TARGET:-600}}"
if [[ ! "$TIME_PER_TARGET" =~ ^[1-9][0-9]*$ ]]; then
  echo "time per target must be a positive number of seconds" >&2
  exit 2
fi
OUT="${1:-artifacts/batch/oscat-100-${TIME_PER_TARGET}s}"
if [[ "$OUT" != /* ]]; then
  OUT="$ROOT/$OUT"
fi
BUILD_OVERHEAD="${3:-${SEMANTIST_BENCH_BUILD_OVERHEAD:-600}}"
if [[ ! "$BUILD_OVERHEAD" =~ ^[1-9][0-9]*$ ]]; then
  echo "build allowance must be a positive number of seconds" >&2
  exit 2
fi

PYTHON="${SEMANTIST_PYTHON:-python3}"

ARGS=(
  --suite benchmarks
  --benchmark-manifest "$ROOT/benchmarks/oscat/manifest.json"
  --time-per-target "$TIME_PER_TARGET"
  --build-overhead "$BUILD_OVERHEAD"
  --switch=--full-hybrid-duration
  --out "$OUT"
)
if [[ -e "$OUT/batch-summary.json" ]]; then
  ARGS+=(--resume)
fi
if [[ "${SEMANTIST_BENCH_LLM:-0}" == 1 ]]; then
  ARGS+=(--switch=--llm)
fi

exec "$PYTHON" -m fuzzer.cli.batch "${ARGS[@]}"
