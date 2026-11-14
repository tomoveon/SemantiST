#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

# This host assigns CPU IDs 0-31 to 32 distinct physical cores.
python3 - <<'PY'
from fuzzer.cli.experiment import _physical_cpus

expected = [[cpu, cpu + 1] for cpu in range(0, 32, 2)]
if _physical_cpus(16) != expected:
    raise SystemExit("quick ablation requires physical CPU IDs 0-31 to be available and distinct")
PY

image_tag="semantist:ablation-quick-$(date +%Y%m%d-%H%M%S)"
run_root="$repo_root/artifacts/experiments/core-ablation-quick-$(date +%Y%m%dT%H%M%S)"
mkdir -p "$repo_root/artifacts/experiments"
mkdir "$run_root"
printf 'Image: %s\nRoot: %s\n' "$image_tag" "$run_root"

podman build --no-cache --format docker \
  -f benchmarks/experiments/Containerfile -t "$image_tag" . \
  2>&1 | tee "$run_root/image-build.log"

python3 -m fuzzer.cli.experiment prepare --execute --allow-formal \
  --experiment core_ablation --seed 0 --root "$run_root" --image "$image_tag" \
  --llm-env-file "$repo_root/.env.semantist-llm" \
  --build-jobs 8 --plan-jobs 4 \
  2>&1 | tee "$run_root/prepare.log"

configurations=(full no_llm no_klee no_native_stg)
cpu_pools=(0-7 8-15 16-23 24-31)
pids=()
for index in "${!configurations[@]}"; do
  configuration="${configurations[$index]}"
  cpu_pool="${cpu_pools[$index]}"
  printf '%s: physical CPUs %s, four concurrent two-core trials\n' "$configuration" "$cpu_pool"
  (
    taskset -c "$cpu_pool" python3 -m fuzzer.cli.experiment run \
      --execute --allow-formal --defer-summary \
      --experiment core_ablation --configuration "$configuration" --seed 0 \
      --root "$run_root" --image "$image_tag" --jobs 4 --memory 8g \
      2>&1 | tee "$run_root/run-$configuration.log"
  ) &
  pids+=("$!")
done

failed=0
for index in "${!pids[@]}"; do
  if ! wait "${pids[$index]}"; then
    printf '%s run failed; inspect %s/run-%s.log\n' \
      "${configurations[$index]}" "$run_root" "${configurations[$index]}" >&2
    failed=1
  fi
done

python3 -m fuzzer.cli.experiment summary \
  --experiment core_ablation --seed 0 --root "$run_root"
exit "$failed"
