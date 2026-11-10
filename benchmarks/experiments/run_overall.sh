#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

image_tag="semantist:experiment-$(date +%Y%m%d-%H%M%S)"
run_root="$repo_root/artifacts/experiments/overall-$(date +%Y%m%dT%H%M%S)"
mkdir -p "$repo_root/artifacts/experiments"
mkdir "$run_root"
printf 'Image: %s\nRoot: %s\n' "$image_tag" "$run_root"

podman build --no-cache --format docker \
  -f benchmarks/experiments/Containerfile -t "$image_tag" . \
  2>&1 | tee "$run_root/image-build.log"

python3 -m fuzzer.cli.experiment prepare --execute --allow-formal \
  --experiment overall --root "$run_root" --image "$image_tag" \
  --llm-env-file "$repo_root/.env.semantist-llm" \
  --build-jobs 8 --plan-jobs 4 \
  2>&1 | tee "$run_root/prepare.log"

tools=(semantist aflpp icsquartz)
pids=()
for tool in "${tools[@]}"; do
  (
    python3 -m fuzzer.cli.experiment run --execute --allow-formal \
      --experiment overall --tool "$tool" --root "$run_root" --image "$image_tag" \
      --schedule standard --jobs 5 --memory 8g \
      2>&1 | tee "$run_root/run-$tool.log"
  ) &
  pids+=("$!")
done

failed=0
for index in "${!pids[@]}"; do
  if ! wait "${pids[$index]}"; then
    printf '%s run failed; inspect %s/run-%s.log\n' \
      "${tools[$index]}" "$run_root" "${tools[$index]}" >&2
    failed=1
  fi
done

python3 -m fuzzer.cli.experiment summary --experiment overall --root "$run_root"
exit "$failed"
