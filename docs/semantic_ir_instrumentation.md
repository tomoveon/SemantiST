# STG-Guided Semantic IR Instrumentation

Source cross-check: 2026-10-09. This describes the current implementation, not a
claim that all benchmarks have been revalidated on this date.

The default semantic coverage path is:

```text
ST source/project
  -> RuSTy Source AST, Index, annotations, Typed/Lowered AST
  -> STG stable targets and codegen anchors
  -> pinned RuSTy semantic metadata
  -> LLVM CFG edge and hazard instrumentation
  -> dedicated semantic bitmap + bounded execution telemetry
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
  bitmap independent from AFL `trace-pc-guard`, and records ordered events and
  scan/state observations in a second shared-memory segment.
- `compiler/harness/generator.py` brackets real target calls with cycle hooks
  and, for FB targets, abstract state observations.
- `fuzzer/semantic/observers.rs` maps dense hits back to stable target ID,
  outcome role, POU, semantic edge IDs, and source location; it also consumes
  bounded telemetry. `fuzzer/engine/src/main.rs` wires these observers into the engine.

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
  --run-dir artifacts/runs/semantic-ir-example \
  --fb-max-cycles 256 --fuzz-timeout 20
```

Semantic IR and STG processing use the LLVM 21 toolchain.
The low-level `compile_st.sh` command produces IR; `build_target.sh` consumes
that IR and links the harness, sanitizer/AFL runtime, IEC runtime and semantic
runtime. Run them in that order. The Python pipeline handles both steps.

For the standard multi-tool experiment, use the repository entry
`python run_experiment.py --experiment smoke --run-id smoke_01 --cpus 2-9`
after building the experiment image; see [server workflow](server_experiment_runbook_zh.md).
Its preset and CPU selection are independent from this single-target CLI.

## Build Artifacts

The STG directory contains:

- `stg-model.json`
- `stg-manifest.json`
- `stg-codegen-map.json`
- `stg-runtime-ids.json`
- `stg-ir-mapping.json`

The default pipeline also writes `RUN_DIR/semantic-task-plan.json`. At runtime,
the Rust observer reverses those IDs to the Stable Target IDs used by the
task plan and testcase `SeedProfile`.

When `--semantic-debug-artifacts` is enabled, the pipeline also writes
`stg-diagnostics.json`, `stg-statistics.json`, `dot/*.dot`,
`stg-ir-diagnostics.json`, and `semantic-ir-build.json` inside `RUN_DIR/stg/`,
and enables `RUN_DIR/semantic-runtime.jsonl` during execution. The narrower
`--semantic-runtime-trace` flag enables just the per-hit runtime trace. Normal
runs keep diagnostics embedded in `stg-model.json` and `stg-ir-mapping.json`.

`stg-ir-mapping.json` records stable and dense IDs, semantic edge IDs, POU,
Lowered AST ID, LLVM function/block/instruction ordinal, successor index,
destination, generated probe block, and mapping kind. One STG target may map
to several LLVM locations. Unmapped targets always receive diagnostics.
The instrumenter verifies the generated LLVM module, but it does not fail
solely because some STG targets remain unmapped. Check mapping statistics and
diagnostics before interpreting a coverage denominator.

## Runtime Telemetry And Scan Bounds

The engine resets the semantic bitmap and telemetry before each input. Bitmap
counters saturate at 255. The telemetry ABI in `semantic_coverage.h` and
`fuzzer/semantic/observers.rs` contains:

- the number of real harness calls (`scan_count`);
- up to 4096 ordered `(runtime_id, cycle)` events;
- up to 512 abstract FB state pairs `(before, after)`;
- active/truncation flags.

Telemetry event cycle numbers are zero-based call ordinals, independent of
the labels in a CSV input. The optional JSONL trace retains the supplied cycle
label for debugging. Truncated telemetry does not invalidate the separate
bitmap, but event overflow makes the ordered trace incomplete. The observer
does not present that sequence as complete transition evidence.

FB state feedback uses bounded field abstraction, not full-memory identity:
up to 32 selected snapshot fields contribute small-integer/logarithmic buckets,
floating-point categories, or aggregate prefix summaries. The observer retains
up to 256 distinct state/transition signatures per execution and 4096 globally.
The signatures exclude cycle ordinal so repeating an unchanged state does not
create novelty just by extending the input. Hash collisions and abstraction
loss remain possible; this feedback is not a proof of state equivalence.

Raw-field snapshot hashes are computed separately when explicit
`--fb-state-trace` or a positive `--fb-stale-threshold` requires them. Normal
state guidance does not need text tracing. For a fixed benchmark harness, the
IR build adds telemetry only to its prepared copy after checking its layout
and hook locations; an unrecognized FB wrapper is rejected.

The experiment runner defaults `--semantist-fb-max-cycles` to 256 and
`--semantist-fb-stale-threshold` to 0. The single-target pipeline forwards
`--fb-max-cycles` and `--fb-stale-threshold` when supplied. During normal fuzzing,
the Rust engine also resolves the cycle limit (default 256, maximum 10000) and
exports it to the child target, so the ordinary `semantist` pipeline still has
the default 256-cycle bound without an explicit CLI override. Only a direct
execution of the generated harness outside the engine, without these
environment settings, has no explicit cycle cap and no stale stopping.
Telemetry capacity itself is not an execution limit.

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
- `HazardViolation`: a runtime predicate observed its modeled violation
  condition. It is candidate evidence, not by itself a confirmed exploitable bug.

Exact runtime predicates currently cover integer and floating division,
integer overflow, floating non-finite arithmetic, array index bounds when
dimensions and the lowered index are retained, null pointer dereference, and
LLVM integer truncation loss. Nullness is the current pointer check: a non-null
pointer is not thereby proven live or within its allocation. Integer-literal
implicit narrowing remains exact
when RuSTy folds away the truncation: the metadata carries the structured
source constant plus source/target width and signedness, and the IR pass emits
a constant true/false violation predicate at the annotated store. A
conservative violation target is not reported as hit merely because its
instruction executed.

Conservative boundaries include non-constant implicit narrowing
when both the original wide value and an exact structured constant are
unavailable, and pointer arithmetic where LLVM object provenance/extent is
unavailable. Reaching such an operation can still be observed where mapped;
its Violation target is not marked hit just because the operation executes.
Unsupported violation instrumentation is reported by diagnostics such as `STG-IR-W006`
and unmapped targets by `STG-IR-E003`. Exponentiation, opaque calls, and other
operations may also lack an exact predicate; inspect the target's modeling
status and final mapping instead of assuming every static target is observable.

## Verification

Run the relevant checks inside the built Linux toolchain environment:

```bash
cargo test --manifest-path compiler/stg/Cargo.toml
cargo test --manifest-path fuzzer/engine/Cargo.toml
python3 -m pytest tests/unit/test_semantic_tasks.py
python3 -m pytest tests/unit/test_harness_telemetry.py
python3 -m pytest tests/integration/test_semantic_ir_runtime.py
./tests/e2e/semantic_ir_e2e.sh
./tests/e2e/semantic_ir_hazard_e2e.sh
./tests/e2e/rusty_semantic_off_equivalence.sh
./benchmarks/oscat_basic/run_semantic_suite.sh
```

The OSCAT suite selects POUs from `benchmarks/oscat_basic/manifest.json`.
Target counts and mapped/unmapped totals must come from the newly generated
`stg-model.json` and `stg-ir-mapping.json`; historical counts do not establish
coverage of a changed source or compiler. The scripts above are available
verification commands, not a record that they were rerun for this documentation
update. The suite script recreates its configured output directory, so use a
dedicated artifact location when preserving previous results.
