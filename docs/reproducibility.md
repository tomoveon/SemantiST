# Reproducing SemantiST Experiments

Checked against the working tree on 2026-10-09. This is the current procedure;
dated validation reports describe their own tested snapshots.

This document describes the expected setup for reproducing the SemantiST
benchmark experiments.

The routine entry is `python run_experiment.py --experiment main`; choose
`--experiment smoke` for a short check.
`smoke` selects three representative targets for 20 seconds and one trial;
`main` selects all 90 targets for 300 seconds and five trials. Both default to
SemantiST, AFL++ and ICSQuartz. Set a new `--run-id` and the server's available
`--cpus`; explicit `--tools`, `--target-id`, `--fuzz-time` and `--fuzz-trials`
override the preset. See the [server runbook](server_experiment_runbook_zh.md)
for source transfer, building, checks and offline analysis. `--help-all`
lists advanced options.

Run this script from the repository checkout on the host, not as an installed
console command inside a trial container. It uses
`experiments/comparison/run_full_comparison.py`; the earlier comparison
scripts remain compatibility entries. `--experiment-id` and `--trials` are
aliases of `--run-id` and `--fuzz-trials`. Explicit target selections replace
the preset targets, and the actual configuration is saved with the results.

## Benchmark Suites

The benchmark index is `benchmarks/external/manifest.json`. The current public
benchmark groups are:

- `oscat_basic`: 61 OSCAT Basic targets, including `FUNCTION` and
  `FUNCTION_BLOCK` POUs.
- `icsquartz_icsfuzz`: 17 ICSFuzz-lineage targets imported from ICSQuartz.
- `icsquartz_scan_cycle`: 12 scan-cycle targets imported from ICSQuartz.

Generated run outputs should stay under `artifacts/` and should not be checked
into Git.

Use a fresh, exclusive run ID for every invocation, including preflight. The
runner rejects existing experiment/preprocessing/trial directories; resume,
preprocessing reuse, and overwrite modes are removed. Preserve failed output,
fix the cause, and manually rerun the selected group under a new ID.

## Timing Policy

The main comparison profile uses a fixed 5-minute online budget per target
and trial.
A 10-minute profile, when run, is a separate sensitivity experiment and must
not be merged with the 5-minute trials. Online time does not include static
preprocessing. The host timer starts before tool launch, so container startup,
native initialization and calibration are inside the online budget. Shutdown
does not extend the nominal budget. Native total counters are last reported
values; see the [measurement standard](experiment_standard.md) for the
different reporting windows and execution models.

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
- One fuzzing worker per trial, constrained to one logical CPU. ICSQuartz keeps
  its Launcher broker/respawner and one client inside that same CPU set; the
  broker is management overhead, not a second fuzz worker.
- A fresh output directory per trial.

The unified entry and compatibility entries schedule trials within `--cpus` by default;
`--serial` selects one trial at a time. Separate runner processes must have
disjoint CPU pools. SemantiST defaults to a 256-cycle limit, shared across
generation, mutation, and harness execution; keep it fixed across profiles.
State trace is optional diagnostic output; abstract shared-memory state
feedback does not depend on it.

Runtime ablations retain the same runner and output contract. Select one of
the seven named profiles through the [ablation entries](../experiments/ablations/README.md),
keep targets and budgets paired, and interpret each configuration separately.
The main cross-tool comparison accepts only the SemantiST `full` profile.

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

The comparison adapters instead mount the host checkout at `/repo` and use
the image's `/opt` binaries. Freeze both at the same revision and rebuild all
SemantiST targets after telemetry ABI changes. ICSQuartz needs only the
checked-in fixtures and pinned baseline image; optional ICSFuzz still needs
its external CODESYS layout and calibration. A destination-server cold build
is required; a short acceptance run over all 90 targets is recommended for
the first deployment. Local unit tests do not establish server readiness.
Main-image APT and Python build dependencies are not fully
locked; record the built image IDs for the campaign.

Online entries collect raw results and export per-trial metrics after strict
validation, without automatic comparative analysis. Run main offline
comparison explicitly using SemantiST `full`; each ablation directory also
contains its own exports for manual interpretation. Ctrl+C may wait for
the active trial batch to finish; after interrupting compilation, check for
leftover containers before restarting. Rootful Docker may leave root-owned
artifacts, so handle their ownership after all writers have stopped. See the
[server runbook](server_experiment_runbook_zh.md) for the manual workflow.

## GitHub Release Checklist

Before publishing:

- Keep source code, benchmarks, manifests, docs, `Cargo.lock`, and `LICENSE`.
- Exclude `artifacts/`, `target/`, `.venv/`, caches, logs, and local
  environment files.
- Run `git diff --check`.
- Run the lightweight unit tests that do not require long fuzzing campaigns.
- Document any unsupported baseline targets explicitly instead of silently
  skipping them.
