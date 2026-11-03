#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

usage() {
  printf 'usage: %s [--time-per-target SECONDS] [--build-overhead SECONDS] [--dry-run] [output-directory]\n' "$0"
}

TIME_PER_TARGET=600
BUILD_OVERHEAD=180
DRY_RUN=0
OUT=""
while (( $# )); do
  case "$1" in
    --time-per-target|--build-overhead)
      if (( $# < 2 )); then
        usage >&2
        exit 2
      fi
      if [[ "$1" == "--time-per-target" ]]; then
        TIME_PER_TARGET="$2"
      else
        BUILD_OVERHEAD="$2"
      fi
      shift 2
      ;;
    --time-per-target=*) TIME_PER_TARGET="${1#*=}"; shift ;;
    --build-overhead=*) BUILD_OVERHEAD="${1#*=}"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *)
      if [[ "$1" == -* || -n "$OUT" ]]; then
        usage >&2
        exit 2
      fi
      OUT="$1"
      shift
      ;;
  esac
done
if [[ ! "$TIME_PER_TARGET" =~ ^[1-9][0-9]*$ || ! "$BUILD_OVERHEAD" =~ ^[1-9][0-9]*$ ]]; then
  echo "time-per-target and build-overhead must be positive integer seconds" >&2
  exit 2
fi

OUT="${OUT:-artifacts/batch/oscat-seven-hybrid-llm-${TIME_PER_TARGET}s}"
if [[ "$OUT" != /* ]]; then
  OUT="$ROOT/$OUT"
fi
PYTHON="${SEMANTIST_PYTHON:-python3}"

ARGS=(
  --suite benchmarks
  --variant buggy
  --benchmark-manifest "$ROOT/benchmarks/oscat/seven_targets.json"
  --time-per-target "$TIME_PER_TARGET"
  --build-overhead "$BUILD_OVERHEAD"
  --search-jobs 1
  --seed 11
  --switch=--full-hybrid-duration
  --switch=--symbolic-start=import
  --switch=--solver-jobs=1
  --switch=--llm
  --switch=--llm-max-calls=12
  --out "$OUT"
)
if [[ -e "$OUT/batch-summary.json" ]]; then
  ARGS+=(--resume)
fi

if (( DRY_RUN )); then
  printf '%q ' "$PYTHON" -m fuzzer.cli.batch "${ARGS[@]}"
  printf '\n'
  exit 0
fi

if ! "$PYTHON" -c 'from fuzzer.llm.config import load_config; raise SystemExit(0 if load_config().available else 2)'; then
  echo "LLM configuration is missing; set SEMANTIST_LLM_BASE_URL and SEMANTIST_LLM_API_KEY or use .env.semantist-llm" >&2
  exit 2
fi

exec "$PYTHON" -m fuzzer.cli.batch "${ARGS[@]}"
