# ST Dialect Normalization

SemantiST treats vendor dialect compatibility as an input-normalization stage,
separate from STG extraction, instrumentation, and fuzzing. A library must pass
this stage before it becomes a fuzzing target:

```text
vendor ST library
  -> dialect analysis and auditable source rewrites
  -> RuSTy-compatible ST library
  -> dependency classification against RuSTy
  -> library-specific compatibility sources and declarations
  -> compile/link smoke test
  -> compiler-semantic STG and fuzzing pipeline
```

The front end lives in `compiler/parser/st-to-rusty-converter`. It is intended
to normalize CODESYS, TwinCAT, and other IEC 61131-3 dialects without changing
the tested program's security-relevant behavior. Transformations must be
recorded and reviewable; the original library remains the experiment input and
the normalized copy is a derived artifact.

## Normalization Output Contract

For an input library, the normalization stage produces:

- a RuSTy-compatible copy of the library;
- a transformation manifest containing source locations, rule identifiers,
  and before/after forms;
- a dependency manifest classifying every unresolved symbol;
- `compatibility.json`, the executable project contract shared by RuSTy
  compilation, native-link validation, and production fuzz builds;
- `stubs.st` for declarations, types, constants, and environment interfaces
  that are genuinely external to the tested library;
- when needed, a dialect compatibility ST module containing reviewed semantic
  adapters for vendor operations that RuSTy does not expose under the same
  name or representation;
- a successful compile/link smoke-test record.

These outputs are library-level inputs. They are generated once per normalized
library, not handwritten for every selected function or function block.

`compatibility.json` supports multiple normalized ST files, multiple ST
adapters, C/C++ sources, precompiled objects or libraries, compiler arguments,
and final linker arguments. Reusable vendor profiles live under
`compiler/toolchain/compatibility/profiles`; `st-to-rusty` selects and copies
them into the derived intake workspace. Library-specific providers remain in
that workspace rather than becoming global Harness behavior.

## Dependency Precedence

Dependency resolution follows this order:

1. RuSTy language built-ins and compiler intrinsics;
2. the pinned RuSTy IEC 61131-3 ST standard library;
3. the pinned RuSTy standard-library runtime archive;
4. definitions in the normalized input library;
5. reviewed dialect compatibility implementations;
6. external environment declarations in `stubs.st`.

The converter must inventory the first four sources before emitting a stub.
Generated declarations must not shadow, replace, or change the signature of a
RuSTy built-in or standard-library symbol. Duplicate definitions and ABI
mismatches are normalization failures.

`stubs.st` is not allowed to make an executable dependency silently return a
default value. A runtime-reachable function needs a semantics-preserving ST or
runtime implementation. A declaration-only `@EXTERNAL` stub is valid only when
the final link supplies that symbol. Hardware, clock, network, and PLC-runtime
interfaces require an explicit deterministic model and must be reported in the
experiment manifest.

## Compile And Link Boundary

RuSTy accepting all ST sources proves parsing, type checking, and IR generation;
it does not prove that the fuzz target is linkable. The normalization gate must
therefore perform both:

1. project compilation with the normalized library, its compatibility sources,
   `stubs.st`, and the pinned RuSTy ST standard library;
2. final executable linking with the RuSTy runtime archive and every declared
   external implementation.

Only a target that passes both checks enters STG extraction and fuzzing. This
keeps missing dependencies out of benchmark execution and makes failures
attributable to normalization rather than to the fuzzer.

## Current OSCAT Instance

`benchmarks/oscat_basic/source/oscat.st` is the already-compatible OSCAT input
used by the current artifact. `benchmarks/oscat_basic/source/stubs.st` contains
reviewed CODESYS-to-RuSTy semantic adapters required by that library. RuSTy
supplies arithmetic and numerical functions
such as `MIN__DINT`, `LIMIT__DINT`, `LN__REAL`, and `EXP__REAL` through its
standard-library runtime archive.

The previous Harness-side `iec_compat.c` and weak `LEN__*`/`MIN__*` fallbacks
have been removed. Temporal adapters now express CODESYS seconds/milliseconds
in ST using RuSTy's documented nanosecond representation and wide conversion
primitives. The Skill produces compile and native-link evidence before a
library can be selected by the normal `semantist` command. Conversion remains a
one-time AI intake action and is never invoked by the fuzzing scheduler.

The OSCAT intake contract is
`benchmarks/oscat_basic/source/compatibility.json`. The same mechanism accepts
directory-based libraries, so onboarding does not require concatenating every
ST source into one benchmark-specific file.
