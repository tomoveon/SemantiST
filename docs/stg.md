# Compiler-Semantic STG

Source cross-check: 2026-10-09 (`compiler/stg`, compilation pipeline, and semantic runtime).

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
The low-level build scripts also accept `SEMANTIST_SEMANTIC_INSTRUMENTATION=off`
for baseline/equivalence builds. The normal `semantist` pipeline selects `ir`
explicitly; changing that environment variable does not disable its STG path.

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

Standalone `--debug-sidecars true` additionally writes `stg-diagnostics.json`
and `stg-statistics.json`; `--dot true` independently enables `dot/*.dot` graph
visualizations. The pipeline's `--semantic-debug-artifacts` enables both. The main
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

The following static facts can be exact when the compiler exposes the required
information; the actual `modeling` field is authoritative for each object:

- IEC scalar/array/string/struct/pointer type facts exposed by RuSTy.
- Resolved symbols and variable roles.
- Structured expressions, assignments, conversions, branch guards, CASE
  labels/default, loop topology, and function-block temporal state.
- Division/modulo, array access, pointer dereference, finite-width arithmetic,
  explicit lowered narrowing, and external-output reach nodes.

Currently conservative or opaque:

- Internal call bodies are represented with conservative signature effects;
  interprocedural body inlining is not performed. Signature-derived effects
  are not a complete proof of callee global or aliased memory effects.
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
- Exponentiation and conversions without a supported exact predicate can also
  be conservative. An exact pointer-dereference target currently checks nullness
  at runtime, not arbitrary non-null pointer lifetime or object bounds.
- Target ABI field offsets and future LLVM instruction locations are left
  unset rather than guessed. RuSTy's Index provides semantic field order and
  type size, but this extractor does not manufacture padding-free ABI offsets.

Expression precision propagates into enclosing expressions and control-edge
transfer summaries. An assignment containing a conservative or opaque call is
therefore not reported as an exact transfer.

The schema's POU and expression kinds describe what STG can represent. They do
not imply that the harness or task planner can execute and guide every such
kind. Likewise, an exact static target is not necessarily mapped into LLVM:
check `stg-ir-mapping.json` for the final mapping and diagnostics. Static STG
does not contain the bounded runtime event order, scan count, or abstract
state-transition feedback; those are maintained separately in shared memory.

## Validation

The validator checks per-POU node/edge/target ID uniqueness, edge endpoints,
required predicate outcome roles and evaluation edges, CASE Default,
loop back/exit edges, stateful temporal relations, typed guard references,
presence of transfer summaries, expression DAG operands, assignment conversion
records, and target-to-source mappings. It does not prove guard disjointness,
whole-project expression ID uniqueness, version consistency, or callee effects.
A generated model may
contain warnings. Validation errors are counted in the model statistics and
embedded diagnostics, with a separate diagnostics file only when requested.
The generator currently still writes artifacts when this validator records an
error; exit success alone is not evidence of a valid model. Inspect
`statistics.error_count` and diagnostics, then inspect the IR mapping when using
the model for coverage. `Exact` is a modeling label, not a vulnerability verdict.

See [semantic_ir_instrumentation.md](semantic_ir_instrumentation.md) for the
cross-layer mapping, LLVM pass, runtime bitmap, and end-to-end commands.
