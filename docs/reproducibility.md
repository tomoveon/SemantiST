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

The main comparison profile uses a fixed 5-minute online budget per target.
A 10-minute profile, when run, is a separate sensitivity experiment and must
not be merged with the 5-minute trials. Online time does not include static
preprocessing.

For SemantiST, preprocessing includes STG extraction, semantic IR
instrumentation, compilation, harness generation, and semantic task-plan
construction. Semantic initial-seed generation starts after online time zero,
matching the measurement contract used by the comparison runner. Record static
preprocessing time separately from online time.

Baseline preprocessing preserves each tool's runtime ABI. AFL++ uses the
SemantiST image only to produce normalized, LLVM-16-compatible IR and the
pinned RuSTy standard library; the final object, compatibility layer, harness,
and executable are compiled and linked by `afl-clang-fast` inside the pinned
AFL++ 4.21c image. This prevents newer host-image glibc requirements from
leaking into the AFL++ target. ICSQuartz target contexts contain the fixed POU
plus its required dependencies; OSCAT contexts deterministically include the
pinned OSCAT source with the target POU removed and the checked-in compatibility
stubs.

## Initialization Policy

Evaluate each fuzzer with its native initialization strategy, following the
style used by ICSQuartz. SemantiST's semantic seed generator is part of the
full tool. Record generated and admitted semantic seed counts when available.

Do not hand-write vulnerability-triggering seeds for the benchmark runs.

## Recommended Baseline Layout

For each supported target, run each tool with:

- 5 independent trials.
- A 5-minute online budget for the main profile; keep any 10-minute profile separate.
- One fuzzing worker per trial. Reserve two CPU slots for ICSQuartz (broker plus
  one client) and one CPU slot for the other default tools, and record this
  resource difference.
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

Build the development/reproduction image from a clean repository checkout. The
build downloads its Ubuntu, LLVM, Python, Cargo, and RuSTy dependencies:

```bash
docker build \
  --build-arg SEMANTIST_SOURCE_REVISION="$(git rev-parse HEAD)" \
  -t semantist:0.1.0 .
```

Create a host output directory and mount only that directory. Do not mount the
whole repository over `/work/SemantiST`, because doing so replaces the source
snapshot that was compiled and checked while building the image:

```bash
mkdir -p artifacts
docker run --rm -it \
  --mount type=bind,src="$PWD/artifacts",dst=/work/SemantiST/artifacts \
  semantist:0.1.0
```

Inside the container, run tests:

```bash
python3 -m pytest
cargo test --workspace --locked --jobs 1
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

The image stores Rust build products in `/opt`, so mounting
`/work/SemantiST/artifacts` cannot hide the prebuilt tools.

## GitHub Release Checklist

Before publishing:

- Keep source code, benchmarks, manifests, docs, `Cargo.lock`, and `LICENSE`.
- Exclude `artifacts/`, `target/`, `.venv/`, caches, logs, and local
  environment files.
- Run `git diff --check`.
- Run the lightweight unit tests that do not require long fuzzing campaigns.
- Document any unsupported baseline targets explicitly instead of silently
  skipping them.
