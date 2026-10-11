# Restructure Report

> Historical record, reviewed on **2026-10-09**. The line counts and test results
> below belong to the earlier repository restructure; they are not a current
> acceptance result. The repository does not link those historical checks to a
> retained log or exact commit here. Current usage and verification procedures
> are in [architecture](architecture.md), [reproducibility](reproducibility.md)
> and the [server runbook](server_experiment_runbook_zh.md).

## Current status (2026-10-09)

The package/workspace layout remains in use. Since the restructure, the tree
has gained the controlled experiment layer under `experiments/`, external
benchmark fixtures under `benchmarks/external/`, and the thin root
`run_experiment.py` entry with `main` and `smoke` presets. Several Rust
responsibility files have changed and `fuzzer/corpus/generation.rs` plus
`fuzzer/semantic/io.rs` are also included by the engine. The historical file
line counts below should not be used to describe today's source size.

The current Rust workspace still contains the engine and STG crates, and the
installed Python command list below still matches `pyproject.toml`. The new
root experiment script is an intentional supported entry, so the old statement
about removing root wrappers applies only to the legacy compiler/pipeline
wrappers. Current test results, where available, are recorded separately in
[algorithm validation](algorithm_validation_20261008_zh.md); server acceptance
must still be performed for the deployed source and image.

## Result

```text
semantist/
├── compiler/{parser,stg,instrumentation,harness,toolchain,scripts}
├── fuzzer/{engine,scheduler,semantic,stages,corpus,runtime,pipeline,reporting,cli}
├── benchmarks/oscat_basic/{functions,function_blocks,source}
├── tests/{unit,integration,e2e,fixtures}
├── artifacts/{cache,build,cargo-target,runs}
├── docs/
├── Cargo.toml
├── Cargo.lock
├── pyproject.toml
├── requirements-dev.txt
├── README.md
├── LICENSE
└── .gitignore
```

The complete old-to-new path table is in [migration.md](migration.md).

## Rust Split

At the time of the restructure, the former 6048-line `src/main.rs` became a
1124-line engine entry assembled
from responsibility files:

- `fuzzer/engine/src/config.rs` and `cli.rs`;
- `fuzzer/scheduler/engine.rs`;
- `fuzzer/semantic/{runtime,metadata,observers}.rs`;
- `fuzzer/stages/semantic_budget.rs`;
- `fuzzer/engine/src/mutations.rs`.

The split preserves one crate scope, Stable Target IDs, scheduler formulae,
observer semantics, seed serialization, and mainline JSON schemas.

## Compatibility

Python commands are installed from `pyproject.toml`: `semantist`,
`semantist-report`, `semantist-minimize`, `semantist-check-llm`,
`semantist-compile`, `semantist-prepare-ir`, and `semantist-harness`.
Compiler shell scripts remain thin wrappers under `compiler/scripts` and use
package modules. Removed root script paths have explicit replacements in
`migration.md`.

## Verification

- `cargo test --workspace --jobs 1`: passed, including engine and STG tests.
- `cargo test -p semantist-stg --jobs 1`: passed, 8 tests.
- `python -m pytest`: passed, 33 tests.
- `tests/e2e/semantic_ir_e2e.sh`: passed.
- `tests/e2e/semantic_ir_hazard_e2e.sh`: passed.
- `tests/e2e/rusty_semantic_off_equivalence.sh`: byte-equivalent LLVM IR.
- OSCAT semantic suite: 17 POUs, 631 nodes, 726 edges, 697 targets validated.
- Editable Python install and `semantist --help`: passed.
- Python `compileall`, `cargo fmt --check`, and `git diff --check`: passed.

## Residual Risk

- The Rust responsibility files use `include!` to preserve the exact original
  crate scope and avoid visibility changes. They are physically separated and
  tested, but can later become conventional Rust modules in a behavior-changing
  development cycle.
- Root compatibility scripts were removed to keep the project root clean.
  Existing automation must use the documented installed commands or new
  `compiler/scripts` paths.
- The pre-existing benchmark deletions and replacement curated benchmark tree
  were preserved rather than reverted.
