#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BENCH_DIR="$ROOT/benchmarks/oscat_basic"
MANIFEST="$BENCH_DIR/manifest.json"
OUT_ROOT="${SEMANTIST_OSCAT_CHECK_DIR:-$ROOT/artifacts/benchmarks/oscat_basic/rusty-compile}"

cd "$ROOT"
mkdir -p "$OUT_ROOT"

python3 - "$MANIFEST" "$BENCH_DIR" <<'PY' |
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
bench_dir = Path(sys.argv[2])
for target in manifest["targets"]:
    if target.get("rusty_compile"):
        st_file = bench_dir / target["st_file"]
        print("\t".join((target["id"], target["function"], str(st_file))))
PY
while IFS=$'\t' read -r target_id function st_file; do
  echo "[check_rusty_compile] $target_id -> $function"
  target_dir="$OUT_ROOT/$target_id"
  mkdir -p "$target_dir"
  OUT_LL="$target_dir/target.ll" ./compiler/scripts/compile_st.sh "$st_file" "$function"
done

echo "[check_rusty_compile] complete: $OUT_ROOT"
