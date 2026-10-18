# SemantiST Architecture

Source review date: **2026-10-09**. This describes the current implementation;
the [restructure report](restructure-report.md) records an earlier migration.

## Entry points

SemantiST has two related entry points:

- `semantist`, installed from `pyproject.toml`, builds and fuzzes one ST target
  and can generate its vulnerability report. Its implementation is
  `fuzzer/pipeline/core.py`.
- `python run_experiment.py` runs the controlled experiment matrix. This thin
  entry delegates to `experiments/comparison/run_full_comparison.py`;
  `--experiment smoke` selects three representative targets, 20 seconds and one
  trial, while `--experiment main` selects the 90-target manifest, 300 seconds
  and five trials. Both default to SemantiST, AFL++ and ICSQuartz. Explicit
  tools, targets, budgets and trial counts override the preset.

Use the [server runbook](server_experiment_runbook_zh.md) for deployment and the
[experiment standard](experiment_standard.md) for measurement rules. The
experiment entry is a repository script, not an installed Python console command.

## Single-target pipeline

1. `compiler/instrumentation/compile.py` and `compiler/scripts/` invoke the
   pinned RuSTy toolchain, build compiler-semantic STG artifacts and LLVM IR,
   and instrument semantic targets. Library/dialect compatibility is handled by
   explicit compatibility manifests and profiles.
2. `compiler/harness/abi.py` derives the target ABI from that build only: STG
   types (resolved constants, bounds, string capacities, VAR_IN_OUT pointees)
   and IR layout (FB member offsets, FUNCTION argument positions). The C
   harness in `compiler/harness/generator.py` asserts the IR offsets at compile
   time; a fixed benchmark harness must equal the one regenerated from the
   build.
3. `fuzzer/semantic/planning.py` derives semantic tasks from STG targets and
   typed dependencies. User seed ingestion and corpus metadata are managed by
   `fuzzer/corpus/store.py`; bounded semantic initial generation is implemented
   in `fuzzer/corpus/generation.rs`.
4. The Rust engine in `fuzzer/engine/` uses a LibAFL forkserver, coverage/time
   feedback, structured mutation and semantic task budgets. Scheduling combines
   semantic selection with queue fallback; execution history and observed
   semantic progress update subsequent selection. Function-block inputs can
   contain bounded sequences of scan cycles.
   The same harness and target code run in three execution modes. `fork`
   (the engine default) forks a child per input. `--execution persistent`
   keeps the forkserver child for up to 10000 inputs and passes each input
   through AFL++ shared memory. `semantist-inprocess`, which `build_target.sh`
   links when `SEMANTIST_INPROCESS_LIB` names `libsemantist_inprocess.a`,
   contains the engine and the target in one process and restarts from the
   saved fuzzer state after a crash or timeout. Both non-fork modes restore the
   target's writable globals from the first input's snapshot before every
   later input. `tests/integration/execution_consistency.py` checks that they
   report the same edges, semantic hits, telemetry and outputs as `fork`.
   Replay always runs the forkserver binary on one input.
5. Runtime findings and reproducer seeds are persisted separately from the
   coverage corpus. `fuzzer/reporting/` deduplicates findings, replays
   representatives and optionally minimizes them before writing reports.

The engine separates implementation across `fuzzer/semantic/`,
`fuzzer/scheduler/`, `fuzzer/corpus/` and `fuzzer/stages/`. The campaign in
`fuzzer/engine/src/campaign.rs` is shared by the forkserver binary
(`main.rs`) and the in-process library (`lib.rs`, `inprocess.rs`). Engine state
that must survive an in-process restart lives in the LibAFL fuzzer state.
Several files still share crate scope through `include!`; the physical directory split is not a
claim that each directory is an independent Rust crate. See
[semantic ranking](stg_semantic_ranking_zh.md) and
[semantic instrumentation](semantic_ir_instrumentation.md) for algorithm details.

## Experiment layer

The comparison runner reads the checked-in benchmark manifest and delegates
tool-specific preparation, online execution and metrics collection to adapters
under `experiments/comparison/adapters/`. It records resolved settings, support
decisions, schedules and preprocessing provenance before online execution.
Trials use disjoint CPU slots from `--cpus`; an existing experiment directory is
rejected rather than reused or overwritten. A runner/cleanup error stops later
batches.

Successful execution requires valid native runs and exports `metrics.csv` and
`metrics.jsonl`. Replay, unified coverage and comparative aggregation are
separate, optional analysis steps. The `smoke` preset checks the integration;
it does not establish complete benchmark support or scientific effectiveness.
SemantiST ablation entry points remain under `experiments/ablations/`.

## Artifacts and evidence

The single-target CLI requires `--run-dir`; `artifacts/runs/<name>/` is the
recommended location. Build locations default to `artifacts/build/` and Cargo
output to `artifacts/cargo-target/` on the host (with environment overrides).
The Dockerfile instead sets `CARGO_TARGET_DIR=/opt/semantist/cargo-target`,
so the image's compiled engines are separate from a mounted `artifacts/`
results directory.
Experiment results default to `artifacts/experiments/<run-id>/`, with separate
per-tool, per-target and per-trial evidence. These generated directories are
ignored by Git and must be archived separately when preserving an experiment.

An online objective is not automatically an independently reproduced
vulnerability. Report confidence depends on replay evidence, and benchmark
metrics follow the experiment validity and online-budget rules. Optional LLM
report text is presentation only; it does not control target selection, initial
seed generation, scheduling or confirmation. See
[vulnerability reporting](vulnerability_reporting.md) for the exact report flow.
