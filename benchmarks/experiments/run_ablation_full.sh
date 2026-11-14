#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

python3 - <<'PY'
import os
import subprocess

lines = subprocess.check_output(["lscpu", "-p=CPU,CORE,SOCKET"], text=True).splitlines()
allowed = os.sched_getaffinity(0)
cores = {}
for line in lines:
    if line.startswith("#") or not line:
        continue
    cpu, core, socket = map(int, line.split(",")[:3])
    if cpu in range(40):
        if cpu not in allowed:
            raise SystemExit(f"CPU {cpu} is unavailable")
        cores.setdefault((socket, core), []).append(cpu)
if len(cores) != 40 or any(len(cpus) != 1 for cpus in cores.values()):
    raise SystemExit("CPU IDs 0-39 must be 40 available, distinct physical cores")
PY

image_tag="${SEMANTIST_ABLATION_IMAGE:-localhost/semantist:exp1}"
run_root="${SEMANTIST_ABLATION_ROOT:-$repo_root/artifacts/experiments/core-ablation-full-$(date +%Y%m%dT%H%M%S)}"
mkdir -p "$repo_root/artifacts/experiments"
mkdir "$run_root"
printf 'Image: %s\nRoot: %s\n' "$image_tag" "$run_root"

python3 -m fuzzer.cli.experiment prepare --execute --allow-formal \
  --experiment core_ablation --root "$run_root" --image "$image_tag" \
  --llm-env-file "$repo_root/.env.semantist-llm" \
  --build-jobs 8 --plan-jobs 4 \
  2>&1 | tee "$run_root/prepare.log"

configurations=(full no_llm no_klee no_native_stg)
cpu_pools=(0-9 10-19 20-29 30-39)
pids=()
for index in "${!configurations[@]}"; do
  configuration="${configurations[$index]}"
  cpu_pool="${cpu_pools[$index]}"
  printf '%s: physical CPUs %s, five concurrent two-core trials\n' "$configuration" "$cpu_pool"
  (
    taskset -c "$cpu_pool" python3 -m fuzzer.cli.experiment run \
      --execute --allow-formal --defer-summary \
      --experiment core_ablation --configuration "$configuration" \
      --root "$run_root" --image "$image_tag" --jobs 5 --memory 8g \
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
  --experiment core_ablation --root "$run_root"
exit "$failed"
