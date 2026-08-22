# STG-Guided Semantic IR Instrumentation

The default semantic coverage path is:

```text
ST source/project
  -> RuSTy Source AST, Index, annotations, Typed/Lowered AST
  -> STG stable targets and codegen anchors
  -> pinned RuSTy semantic metadata
  -> LLVM CFG edge and hazard instrumentation
  -> dedicated semantic bitmap
  -> dense-ID reverse mapping to stable STG targets
```

No source rewrite is required in `ir` mode. LLVM IR is not used to rediscover
ST syntax: it only selects the executable instruction or CFG edge for semantic
events already identified by the compiler-semantic STG.

## Components

- `compiler/stg/src/instrumentation.rs` assigns deterministic dense IDs and emits the
  Source/Lowered AST codegen sidecar.
- `compiler/toolchain/rusty-patches/0001-semantist-semantic-metadata.patch` is the minimal patch
  against RuSTy commit `be1de6f175ec1bed7928904dfb694208a24043fb`.
- `compiler/stg/src/ir.rs` reads `!semantist.semantic`, splits real CFG edges, inserts
  probes, and emits the final cross-layer map and diagnostics.
- `compiler/instrumentation/runtime/semantic_coverage.c` records semantic hits in a SysV shared-memory
  bitmap independent from AFL `trace-pc-guard`.
- `fuzzer/engine/src/main.rs` maps dense hits back to stable target ID, outcome role, POU,
  semantic edge IDs, and source location.

The RuSTy patch only loads a sidecar, propagates semantic identity through
codegen, and writes LLVM metadata. STG generation, instrumentation policy,
runtime storage, and feedback remain owned by SemantiST. A missing, malformed,
or unsupported sidecar is reported as `STG-IR-E101` through `STG-IR-E103`.

## Build And Run

Build the pinned compiler:

```bash
./compiler/scripts/build_rusty_semantic.sh
```

Build one target through the semantic IR path:

```bash
SEMANTIST_SEMANTIC_INSTRUMENTATION=ir \
  ./compiler/scripts/compile_st.sh path/to/target.st POU_NAME

SEMANTIST_SEMANTIC_INSTRUMENTATION=ir \
  ./compiler/scripts/build_target.sh path/to/target.st POU_NAME
```

The full Python pipeline uses `ir` by default:

```bash
semantist \
  --st-file path/to/target.st \
  --function POU_NAME \
  --run-dir artifacts/runs/semantic-ir-example
```

Semantic IR and STG processing use the LLVM 21 toolchain.

## Build Artifacts

The STG directory contains:

- `stg-model.json`
- `stg-codegen-map.json`
- `stg-runtime-ids.json`
- `stg-ir-mapping.json`

The default pipeline also writes `RUN_DIR/semantic-task-plan.json`. At runtime,
the Rust
observer immediately reverses those IDs to the Stable Target IDs used by the
task plan and testcase `SeedProfile`.

When `--semantic-debug-artifacts` is enabled, the pipeline also writes
`dot/*.dot`, `stg-ir-diagnostics.json`, `semantic-ir-build.json`, and
`semantic-runtime.jsonl` for inspection. Normal runs keep diagnostics embedded
in `stg-model.json` and `stg-ir-mapping.json`.

`stg-ir-mapping.json` records stable and dense IDs, semantic edge IDs, POU,
Lowered AST ID, LLVM function/block/instruction ordinal, successor index,
destination, generated probe block, and mapping kind. One STG target may map
to several LLVM locations. Unmapped targets always receive diagnostics.

## Probe Semantics

Conditional outcomes and loop outcomes are placed on CFG edges. Empty branch
bodies remain distinguishable because the pass splits each successor edge.
Switch labels retain separate probes even when several labels share one
destination. Loop condition outcomes and structural LoopBack events are
separate. REPEAT keeps its ST meaning: true exits and false continues.

RETURN and Function Block CycleExit may coexist on one `ret`; both events are
preserved. CycleEntry is represented by a dedicated entry edge when semantic
metadata is enabled.

Hazards use two targets:

- `HazardReach`: execution reached the operation.
- `HazardViolation`: a runtime predicate proved the violation condition.

Exact runtime predicates currently cover integer and floating division,
integer overflow, floating non-finite arithmetic, array index bounds when
dimensions and the lowered index are retained, null pointer dereference, and
LLVM integer truncation loss. Integer-literal implicit narrowing remains exact
when RuSTy folds away the truncation: the metadata carries the structured
source constant plus source/target width and signedness, and the IR pass emits
a constant true/false violation predicate at the annotated store. A
conservative violation target is not reported as hit merely because its
instruction executed.

Current explicit conservative boundaries are non-constant implicit narrowing
when both the original wide value and an exact structured constant are
unavailable, and pointer arithmetic where LLVM object provenance/extent is
unavailable. Their Reach targets are still exact; their Violation targets
remain unmapped with `STG-IR-W006` and `STG-IR-E003`.

## Verification

Focused checks:

```bash
cargo test --manifest-path compiler/stg/Cargo.toml
cargo test
python3 -m pytest tests/unit/test_semantic_tasks.py
python3 -m pytest tests/integration/test_semantic_ir_runtime.py
./tests/e2e/semantic_ir_e2e.sh
./tests/e2e/semantic_ir_hazard_e2e.sh
./tests/e2e/rusty_semantic_off_equivalence.sh
./benchmarks/oscat_basic/run_semantic_suite.sh
```

The OSCAT semantic suite currently contains 17 real POUs and 697 semantic
targets. The LLVM end-to-end audit maps 696 targets, including every
control-flow target, every Reach target, and all 24 integer-literal implicit
narrowing violations that RuSTy folds before LLVM IR generation. The sole
remaining conservative Violation target is a pointer-arithmetic
provenance/extent check.
