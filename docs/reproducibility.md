# Reproducing SemantiST Experiments

This document describes the expected setup for reproducing the SemantiST
benchmark experiments.

## Benchmark Suites

The benchmark index is `benchmarks/external/manifest.json`. The current public
benchmark groups are:

- `oscat_basic`: 61 OSCAT Basic targets, including `FUNCTION` and
  `FUNCTION_BLOCK` POUs.
- `icsquartz_icsfuzz`: 17 ICSFuzz-lineage targets imported from ICSQuartz.
- `icsquartz_scan_cycle`: 12 scan-cycle targets imported from ICSQuartz.

Generated run outputs should stay under `artifacts/` and should not be checked
into Git.

## Timing Policy

Use fixed fuzzing-time budgets, for example 5 minutes and 10 minutes per
target. Fuzzing time does not include preprocessing.

For SemantiST, preprocessing includes STG extraction, semantic IR
instrumentation, compilation, harness generation, and semantic seed generation.
Record preprocessing time separately from fuzzing time.

## Initialization Policy

Evaluate each fuzzer with its native initialization strategy, following the
style used by ICSQuartz. SemantiST's semantic seed generator is part of the
full tool. Record generated and admitted semantic seed counts when available.

Do not hand-write vulnerability-triggering seeds for the benchmark runs.

## Recommended Baseline Layout

For each supported target, run each tool with:

- 5 independent trials.
- 5-minute and 10-minute fuzzing budgets.
- One CPU core per trial, unless explicitly evaluating parallel fuzzing.
- A fresh output directory per trial.

Record at least:

- target id, suite, kind, function, and trial id;
- preprocessing time and fuzzing budget;
- total executions and executions per second;
- whether a finding was discovered;
- first-finding time and first-finding executions;
- finding type and count;
- edge coverage and semantic coverage when available;
- initial seed counts and semantic generated/admitted seed counts when
  available;
- failure or unsupported reason.

## Docker

Build the development/reproduction image from the repository root:

```bash
docker build -t semantist:latest .
```

Run an interactive container with the repository mounted so that experiment
artifacts are written back to the host:

```bash
docker run --rm -it \
  -v "$PWD":/work/SemantiST \
  -w /work/SemantiST \
  semantist:latest
```

Inside the container, run tests:

```bash
cargo test
LLVM_SYS_211_PREFIX=/usr/lib/llvm-21 cargo test -p semantist-stg --jobs 1
.venv/bin/python -m pytest
```

Run a small SemantiST example:

```bash
semantist \
  --st-file benchmarks/oscat_basic/functions/inc2/target.st \
  --function INC2 \
  --compatibility-manifest benchmarks/oscat_basic/source/compatibility.json \
  --run-dir artifacts/runs/inc2-example \
  --fuzz-timeout 300
```

## GitHub Release Checklist

Before publishing:

- Keep source code, benchmarks, manifests, docs, `Cargo.lock`, and `LICENSE`.
- Exclude `artifacts/`, `target/`, `.venv/`, caches, logs, and local
  environment files.
- Run `git diff --check`.
- Run the lightweight unit tests that do not require long fuzzing campaigns.
- Document any unsupported baseline targets explicitly instead of silently
  skipping them.
