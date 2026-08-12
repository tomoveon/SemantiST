#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BENCH_DIR="$ROOT/benchmarks/oscat_basic"
MANIFEST="$BENCH_DIR/manifest.json"
OUT_DIR="${SEMANTIST_OSCAT_SEMANTIC_DIR:-$ROOT/artifacts/benchmarks/oscat_semantic/stg}"

mapfile -t POUS < <(
  python3 - "$MANIFEST" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for target in manifest["targets"]:
    print(target["function"])
PY
)

args=()
for pou in "${POUS[@]}"; do
  args+=("--pou" "$pou")
done

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

cd "$ROOT"
./compiler/scripts/generate_stg.sh \
  --project-root "$ROOT" \
  --output "$OUT_DIR" \
  "${args[@]}" \
  "$BENCH_DIR/plc.json"

python3 "$BENCH_DIR/check_semantic_suite.py" \
  --root "$ROOT" \
  --manifest "$MANIFEST" \
  --model "$OUT_DIR/stg-model.json"

echo "[oscat_semantic] artifacts: $OUT_DIR"
