#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

image_tag="semantist:improvement-$(date +%Y%m%d-%H%M%S)"
run_root="$repo_root/artifacts/experiments/improvement-$(date +%Y%m%dT%H%M%S)"
mkdir -p "$repo_root/artifacts/experiments"
mkdir "$run_root"

python3 - "$run_root" <<'PY'
import json
import sys
from pathlib import Path

from fuzzer.experiments.matrix import Job, ROOT

root = Path(sys.argv[1])
selection = json.loads((ROOT / "benchmarks/experiments/improvement_targets.json").read_text())
baseline = json.loads((ROOT / selection["source_root"] / "per-target.json").read_text())
unfound = {row["target_id"] for row in baseline
           if row["experiment"] == "overall" and row["tool"] == "semantist"
           and row["configuration"] == "full" and row["online_seconds"] == 300
           and row["planned_runs"] == 5 and row["runs_with_confirmed"] == 0}
manifest = json.loads((ROOT / "benchmarks/oscat/manifest.json").read_text())["targets"]
by_id = {target["id"]: target for target in manifest}
target_ids = selection["targets"]
if (len(target_ids) != 36 or len(set(target_ids)) != 36
        or set(target_ids) != {target_id for target_id in unfound
                              if by_id[target_id]["kind"] == "FUNCTION_BLOCK"}):
    raise SystemExit("improvement target set no longer matches the frozen overall results")
jobs = [Job(by_id[target_id], "semantist", "improved", 0, 100, ["improvement"])
        for target_id in target_ids]
(root / "matrix.json").write_text(json.dumps({
    "schema_version": "semantist.experiment-matrix/1",
    "jobs": [job.as_dict() for job in jobs],
}, indent=2) + "\n")
(root / "improvement_targets.json").write_text(json.dumps(selection, indent=2) + "\n")
print(f"Frozen {len(jobs)} SemantiST jobs at 100 seconds each in {root}")
PY

printf 'Image: %s\nRoot: %s\n' "$image_tag" "$run_root"
podman build --no-cache --format docker \
  -f benchmarks/experiments/Containerfile -t "$image_tag" . \
  2>&1 | tee "$run_root/image-build.log"

python3 -m fuzzer.cli.experiment prepare --execute --allow-formal \
  --experiment improvement --root "$run_root" --image "$image_tag" \
  --llm-env-file "$repo_root/.env.semantist-llm" \
  --build-jobs 4 --plan-jobs 4 \
  2>&1 | tee "$run_root/prepare.log"

python3 -m fuzzer.cli.experiment run --execute --allow-formal \
  --experiment improvement --tool semantist --configuration improved --seed 0 \
  --root "$run_root" --image "$image_tag" --jobs 4 --memory 8g \
  2>&1 | tee "$run_root/run-semantist.log"

python3 -m fuzzer.cli.experiment summary --experiment improvement --root "$run_root"
python3 -m benchmarks.experiments.review_improvement_candidates "$run_root"
