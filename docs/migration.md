# Layout and Interface Migration

Updated against the current source on **2026-10-09**. The path table describes
the earlier layout migration; the experiment section below covers the current
interface. For normal use, start with the
[server runbook](server_experiment_runbook_zh.md) or
[architecture](architecture.md), rather than reproducing the old migration.

## Package and source paths

Install the Python package with `python -m pip install -e '.[dev]'` to obtain
the `semantist` console commands. Imports no longer require an implicit
`PYTHONPATH`. Relative CLI input paths still depend on the working directory,
and repository examples should be run from the repository root. Installing the
Python package alone does not install the compiler, LLVM or fuzzing toolchain.

| Legacy path | Current path |
| --- | --- |
| `stg/` | `compiler/stg/` |
| `fuzzer/st.py` | `compiler/parser/st.py` |
| `fuzzer/harness.py` | `compiler/harness/generator.py` |
| `fuzzer/compile.py` | `compiler/instrumentation/compile.py` |
| `runtime/semantic_coverage.*` | `compiler/instrumentation/runtime/` |
| `rusty-patches/`, `vendor/` | `compiler/toolchain/` |
| `src/main.rs` | `fuzzer/engine/src/main.rs` plus responsibility files |
| `fuzzer/semantic.py` | `fuzzer/semantic/planning.py` |
| `fuzzer/pipeline.py` | `fuzzer/pipeline/core.py` |
| `fuzzer/corpus.py` | `fuzzer/corpus/store.py` |
| `fuzzer/report.py`, `minimize.py` | `fuzzer/reporting/` |
| `fuzzer/llm.py` seed generation | removed; use bounded semantic generation |
| `test/` | `tests/unit`, `tests/integration`, `tests/e2e` |
| `st/oscat.st` | `benchmarks/oscat_basic/source/oscat.st` |
| `st/stubs.st` | `benchmarks/oscat_basic/source/stubs.st` (OSCAT-only adapters) |
| legacy compiler command wrappers | `compiler/scripts/` |
| legacy pipeline wrapper | `semantist` |
| legacy report wrapper | `semantist-report` |
| root `build/`, `target/`, run dirs | `artifacts/build`, `artifacts/cargo-target`, `artifacts/runs` |

The source/LLM targeting prototype has been removed. The following interfaces
have no replacement because compiler-semantic STG owns target identity and
scheduling:

- `--targeting-mode`, `--legacy-targeting`, and `--rank-only`;
- `--branch-rank` and all branch-rank provider/batch options;
- `--semantic-instrumentation source`;
- `branch-targets.json`, `branch-rank.json`, and source-rewritten ST files.

LLM initial-seed and function-block-sequence generation have also been removed.
Initial corpus expansion consumes compiler-semantic STG facts through LibAFL's
Generator interface. Optional LLM report summarization remains presentation-only
and cannot define targets or promote findings.

## Current experiment interface

Use the root entry for new scripts:

```bash
python run_experiment.py --experiment smoke --run-id smoke_01 --cpus 2-9
python run_experiment.py --experiment main --run-id main_01 --cpus 2-9
```

Replace `2-9` with available CPUs on the machine. The `main` preset defaults to
three tools, 90 targets, 300 seconds and five trials; `smoke` defaults to the
same tools, three representative targets, 20 seconds and one trial. The common
overrides are `--tools`, `--target-id`, `--fuzz-time`, `--fuzz-trials` and
`--cpus`. `--help` shows everyday options and `--help-all` includes advanced ones.

`--experiment-id` and `--trials` remain aliases of `--run-id` and
`--fuzz-trials`. Existing scripts under `experiments/comparison/` remain
compatible entry points; they are not required steps before the root runner.
Ablations keep their dedicated entry points under `experiments/ablations/`.

`--resume`, `--overwrite` and `--reuse-preprocessing` are no longer supported.
Choose a fresh run ID for every attempt. Successful runs export raw metrics;
replay, coverage and comparative analysis remain explicit operations. Historical
experiment outputs should be retained as historical evidence, not silently
merged with new runs. See [reproducibility](reproducibility.md) for the required
source, image and result provenance.
