# Independent tool experiments

Run each tool separately, collect the same metrics, and compare results offline. This workflow assumes no existing experiment results. See [the measurement standard](../../docs/experiment_standard.md) for definitions.

## Linux setup and smoke

From the repository root, build the tool images using [the baseline instructions](../baselines/README.md), including the SemantiST toolchain image. Use the same server and benchmark checkout for every tool.

Freeze a clean source revision before building the SemantiST image, and embed
that revision in the image metadata:

```bash
git diff --quiet && git diff --cached --quiet
docker build --pull \
  --build-arg SEMANTIST_SOURCE_REVISION="$(git rev-parse HEAD)" \
  -t semantist:0.1.0 .
```

```bash
python benchmarks/check_benchmark_files.py --check-files

python experiments/comparison/run_tool.py \
  --tool semantist --run-id semantist_preflight_new \
  --target-id oscat_basic_inc2 --trials 1 --fuzz-time 20 \
  --container-runtime docker --cpus 1-2 --preflight-only

python experiments/comparison/run_tool.py \
  --tool semantist --run-id semantist_smoke_new \
  --target-id oscat_basic_inc2 --trials 1 --fuzz-time 20 \
  --container-runtime docker --cpus 1-2
```

Repeat the smoke with `--tool aflplusplus` and `icsquartz`, each with a fresh run id. Preflight describes the planned run and static support; it is not evidence that online fuzzing works. Inspect the new trial records for status and measurement availability. A crash need not occur during a smoke.

Choose CPUs allowed by the server affinity mask. ICSQuartz reserves two CPU slots for one fuzzing client plus its broker; other default tools reserve one. This resource difference is recorded. Trials run serially within each invocation. Avoid competing experiments on the same CPUs.

## Separate full runs

Each command can be launched and completed independently:

```bash
python experiments/comparison/run_tool.py --tool semantist \
  --run-id semantist_main_new --trials 5 --fuzz-time 300 --timeout-ms 1000 \
  --container-runtime docker --cpus 1-2

python experiments/comparison/run_tool.py --tool aflplusplus \
  --run-id aflplusplus_main_new --trials 5 --fuzz-time 300 --timeout-ms 1000 \
  --container-runtime docker --cpus 1-2

python experiments/comparison/run_tool.py --tool icsquartz \
  --run-id icsquartz_main_new --trials 5 --fuzz-time 300 --timeout-ms 1000 \
  --container-runtime docker --cpus 1-2
```

Use repeated `--target-id` arguments for a smaller identical target set. Defaults select the benchmark manifest. Unsupported targets and failed builds produce explicit records. Optional `--tool icsfuzz` retains its native limitations; first-crash count/input can be unavailable.

Each tool retains its native seed policy. SemantiST's semantic initial generation remains inside the online budget. Compilation and static preparation are recorded separately. No historical corpus is imported. A fresh run id is required: the single-tool entry rejects resume/overwrite and does not automatically run replay or coverage.

## Outputs and offline comparison

```text
artifacts/experiments/<run-id>/
  environment.json
  experiment-manifest.json
  support-matrix.csv
  schedule.jsonl
  runs/<tool>/<suite>/<target>/trial_XX/
    run-result.json             # authoritative trial record and paper_metrics
    measurement-events.jsonl   # newly observed findings and metadata updates
    ...native inputs, reports, commands, stdout/stderr...
  raw-runs.jsonl
  metrics.jsonl
  metrics.csv
```

Metrics include native crash detection, first-crash count/time/input, reported total executions, throughput, sources and validity. Missing measurements stay null (empty CSV cells). First-crash time is the runner's first observation, including polling/write delays. Total counts are last reported values, not guaranteed exact stop counters. Execution models remain explicit because one testcase can contain multiple PLC cycles.

After all runs finish:

```bash
python experiments/comparison/analysis/compare_tools.py \
  --experiment-dir artifacts/experiments/semantist_main_new \
  --experiment-dir artifacts/experiments/aflplusplus_main_new \
  --experiment-dir artifacts/experiments/icsquartz_main_new \
  --out artifacts/comparisons/main_new
```

This reads authoritative trial files and writes `metrics.csv`, `metrics.jsonl`, `comparison.csv`, and `comparison.json`. It checks matching fixtures, budgets and trials, rejects duplicate/missing records, and reports per-target summaries. Use `common_supported_target=true` rows for direct multi-tool comparisons. Read `comparability_notes` for execution model and CPU differences. File hashes alone do not prove equivalence between compiler/runtime implementations.

Success rates use valid trials as denominator and report invalid trials alongside them. Time/count medians are explicitly successful-trial-only: report success rates and missing-count totals too. Native candidates are not automatically replay-confirmed crashes or unique bugs.

## Optional replay and verification

Replay is separate from the online budget:

```bash
python -m experiments.comparison.analysis.replay \
  --experiment-dir artifacts/experiments/semantist_main_new \
  --container-runtime docker
```

Rerun offline comparison afterwards to refresh exports. Replay uses stdin or a file argument according to the tool. Unsupported conversion/history remains unverified. The current ICSQuartz adapter does not supply a standalone replay binary; persistent scan history is not reconstructed. Replay never erases the original discovery count/time. Native coverage is diagnostic, not a cross-tool score.

Fresh synthetic regression tests (no prior experiment data):

```bash
python -m unittest discover -s tests/unit -p test_comparison_experiment.py
python -m unittest discover -s tests/unit -p test_independent_experiments.py
```

`run_full_comparison.py` and `run_complete_comparison.py` remain compatibility entries. The complete profile no longer adds replay/coverage by default. Prefer one tool per run followed by offline comparison.
