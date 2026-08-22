# Compiler-Semantic STG

`semantist-stg` generates a versioned static semantic model for each RuSTy POU:

```text
M_P = <G_P, Sigma_P, mu_P, T_P>
```

The implementation is in `compiler/stg/`. It calls RuSTy compiler crates pinned to
revision `be1de6f175ec1bed7928904dfb694208a24043fb` (`v0.5.0-3`) and does not
parse `--ast-lowered` debug text. Target extraction uses compiler facts only.

## Usage

Generate from one or more ST files:

```bash
./compiler/scripts/generate_stg.sh \
  --project-root . \
  --output artifacts/compiler/stg/example \
  path/to/source.st
```

Generate from a RuSTy project directory or its `plc.json`:

```bash
./compiler/scripts/generate_stg.sh \
  --project-root . \
  --output artifacts/compiler/stg/project \
  --pou MY_FUNCTION_BLOCK \
  path/to/project
```

Repeat `--pou` to extract several POUs from one compiler run.

The normal SemantiST pipeline always uses semantic IR instrumentation and
generates these artifacts under `RUN_DIR/stg/` before RuSTy code generation.
The resulting codegen sidecar is consumed by the pinned RuSTy extension.
`SEMANTIST_SEMANTIC_INSTRUMENTATION=off` remains available only for the
semantic-off equivalence test.

LLVM 21 is required by the pinned RuSTy revision. The wrapper defaults
`LLVM_SYS_211_PREFIX` to `/usr/lib/llvm-21`.

## Compiler Facts

The extractor runs two compiler pipelines over the same project:

1. Parse, index, and annotate without lowering. This preserves source-level
   IF/ELSIF/CASE/WHILE/REPEAT/FOR structure and supplies resolved names and
   types.
2. Parse, run RuSTy's official lowering participants, index, annotate, and
   validate. This supplies typed/lowered AST locations and compiler validation.

Stable semantic IDs use the project-relative file, qualified POU, source
range, semantic kind, and deterministic outcome ordinal. RuSTy `AstId` values
are retained only as mapping facts and never participate in an ID.

## Artifacts

- `stg-model.json`: complete `semantist.stg/1.2.0` project model.
- `stg-manifest.json`: artifact paths and deterministic model digest.
- `stg-codegen-map.json`: Source/Lowered AST anchors and semantic events for
  RuSTy code generation.
- `stg-runtime-ids.json`: deterministic dense runtime IDs and their stable STG
  target reverse mapping.

Debug sidecars (`--debug-sidecars` or the pipeline's
`--semantic-debug-artifacts`) additionally write `stg-diagnostics.json`,
`stg-statistics.json`, and optional `dot/*.dot` graph visualizations. The main
model already embeds diagnostics and statistics, so normal semantic-guided
fuzzing runs do not need the duplicate JSON files.

The JSON contains no runtime coverage, hit counts, fuzzing history, scheduling
weights, or mutation state.

## Graph Semantics

Predicates and outcomes are separate nodes and targets. CASE labels and
Default are distinct. WHILE and FOR use `LoopEnter`/`LoopExit`; REPEAT uses a
structural first `LoopEnter`, then `LoopContinue` for a false UNTIL result and
`LoopExit` for a true result. This prevents post-test loop exit from being
misread as an ordinary true branch.

Evaluation edges carry typed expression DAG guards. Control-transfer edges
carry ordered transfer operations, read/write sets, per-path local versions,
explicit conversions, and implicit hold semantics for unmodified variables.
Loop back edges remain cyclic. Stateful POUs have Entry, CycleEntry,
CycleExit, Exit, and a temporal CycleExit-to-CycleEntry edge describing
persistent-state inheritance. Ordinary cycle inputs, in-out references,
configuration inputs, and carried instance state are represented separately.

`Symbol.constant` is reserved for IEC/RuSTy constant semantics.
`Symbol.configuration_source` distinguishes `iec_constant` from the
`var_input_constant_annotation` source convention used by OSCAT and related
libraries, so vendor annotations remain usable without being presented as
compiler-proven facts.

RETURN is a terminating transfer to the current invocation boundary: Exit for
functions and CycleExit for function blocks. Statements following RETURN do
not remain on an executable STG path.

## Modeling Boundaries

`Exact` is used when the source AST, annotations, and Index directly determine
the operation. `Conservative` is used when effects are bounded but a complete
callee or arithmetic proof is unavailable. `Opaque` is used for unresolved or
external operations whose semantics cannot be stated safely.

Currently exact:

- IEC scalar/array/string/struct/pointer type facts exposed by RuSTy.
- Resolved symbols and variable roles.
- Structured expressions, assignments, conversions, branch guards, CASE
  labels/default, loop topology, and function-block temporal state.
- Division/modulo, array access, pointer dereference, finite-width arithmetic,
  explicit lowered narrowing, and external-output reach nodes.

Currently conservative or opaque:

- Internal call bodies are represented with conservative signature effects;
  interprocedural body inlining is not performed.
- External and unsupported library calls are opaque, with argument reads and
  known output/by-reference effects where RuSTy exposes them.
- Integer-literal implicit narrowing is exact even when RuSTy folds the
  conversion before LLVM IR generation: the structured source constant,
  source/target widths, and source/target signedness are carried in the
  narrowing predicate. Dynamic narrowing is exact when lowering retains an
  LLVM truncation. Other implicit conversions remain conservative if neither
  the original value nor an exact structured constant survives. Pointer
  arithmetic lowered to an inbounds GEP is also conservative because object
  provenance and allocation extent are not fully represented in the current
  STG predicate.
- User safety properties have model types reserved, but no property-language
  frontend is defined yet.
- Target ABI field offsets and future LLVM instruction locations are left
  unset rather than guessed. RuSTy's Index provides semantic field order and
  type size, but this extractor does not manufacture padding-free ABI offsets.

Expression precision propagates into enclosing expressions and control-edge
transfer summaries. An assignment containing a conservative or opaque call is
therefore not reported as an exact transfer.

## Validation

The validator checks unique and non-dangling IDs, complete predicate outcome
partitions, CASE Default, loop back/exit edges, stateful temporal relations,
typed guard references, transfer summaries, expression DAG operands,
assignment conversions, and target-to-source mappings. A generated model may
contain warnings, but validation errors are counted in the statistics and
written to both the model and diagnostics artifact.

See [semantic_ir_instrumentation.md](semantic_ir_instrumentation.md) for the
cross-layer mapping, LLVM pass, runtime bitmap, and end-to-end commands.
