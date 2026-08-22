# Layout Migration

The repository now uses package and workspace entry points. Install the Python
package with `pip install -e '.[dev]'`; commands no longer require an implicit
`PYTHONPATH` or a particular working directory.

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
have no replacement because compiler-semantic STG now owns target identity and
scheduling:

- `--targeting-mode`, `--legacy-targeting`, and `--rank-only`
- `--branch-rank` and all branch-rank provider/batch options
- `--semantic-instrumentation source`
- `branch-targets.json`, `branch-rank.json`, and source-rewritten ST files

LLM initial-seed and function-block-sequence generation have also been removed.
Initial corpus expansion is now deterministic and consumes compiler-semantic STG
facts through LibAFL's Generator interface. Optional LLM report summarization
remains presentation-only and cannot define targets or promote findings.
