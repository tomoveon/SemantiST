# ST Dialect Normalization

Source cross-check: 2026-10-09 (`prepare_library.py`, `validate_library.py`,
`stubs_analyzer.py`, and `compiler/toolchain/compatibility`).

SemantiST treats vendor dialect compatibility as an input-normalization stage,
separate from STG extraction, instrumentation, and fuzzing. The intake workflow
for a new library is:

```text
vendor ST library
  -> dialect analysis and auditable source rewrites
  -> RuSTy-compatible ST library
  -> dependency classification against RuSTy
  -> library-specific compatibility sources and declarations
  -> compile/link validation of selected POUs
  -> reviewed runtime smoke inputs when required
  -> compiler-semantic STG and fuzzing pipeline
```

The front end lives in `compiler/parser/st-to-rusty-converter`. It is intended
to normalize CODESYS, TwinCAT, and other IEC 61131-3 dialects without changing
the tested program's security-relevant behavior. Transformations must be
recorded and reviewable. Retain the original library for provenance; compilation
and fuzzing consume the reviewed normalized copy as a derived artifact.

## Normalization Output Contract

After completing intake, retain these library-level artifacts:

- a RuSTy-compatible copy of the library;
- a transformation manifest containing source locations, rule identifiers,
  and before/after forms;
- a dependency report classifying the symbols observed in compile/link
  diagnostics and the validator's IR declaration inventory;
- `compatibility.json`, the executable project contract shared by RuSTy
  compilation, native-link validation, and production fuzz builds;
- `stubs.st` for declarations, types, constants, and environment interfaces
  that are genuinely external to the tested library;
- when needed, a dialect compatibility ST module containing reviewed semantic
  adapters for vendor operations that RuSTy does not expose under the same
  name or representation;
- `verification.json` and `validation/` logs for the selected compile/link checks.

`prepare_library.py` initially writes pending verification and dependency
records. It does not prove compatibility or generate working implementations
of unknown dependencies. Run `validate_library.py` after reviewing the
transformations and supplying required providers. The resulting compatibility
workspace can be reused for multiple selected functions or function blocks;
revalidate it when its sources, providers, or toolchain change.

`compatibility.json` supports multiple normalized ST files, multiple ST
adapters, C/C++ sources, precompiled objects or libraries, compiler arguments,
and final linker arguments. Reusable vendor profiles live under
`compiler/toolchain/compatibility/profiles`; `prepare_library.py --profile`
selects and copies them into the derived intake workspace. Library-specific providers remain in
that workspace rather than becoming global Harness behavior.

## Dependency Precedence

Dependency resolution follows this order:

1. RuSTy language built-ins and compiler intrinsics;
2. the pinned RuSTy IEC 61131-3 ST standard library;
3. the pinned RuSTy standard-library runtime archive;
4. definitions in the normalized input library;
5. native compatibility providers;
6. named environment models, ST semantic adapters, then external declarations
   in the supplied compatibility ST sources (reported under `stubs.st`).

The dependency analyzer inventories these providers when classifying compile
and link diagnostics. The preparer itself creates an empty `stubs.st` template,
or copies `--reviewed-stubs`; it does not synthesize a fully resolved dependency
set. Before adding a declaration, check the compiler, standard library, and
input library.
Generated declarations must not shadow, replace, or change the signature of a
RuSTy built-in or standard-library symbol. Duplicate definitions and ABI
mismatches are normalization failures.

`stubs.st` is not allowed to make an executable dependency silently return a
default value. A runtime-reachable function needs a semantics-preserving ST or
runtime implementation. A declaration-only `@EXTERNAL` stub is valid only when
the final link supplies that symbol. Hardware, clock, network, and PLC-runtime
interfaces need an explicit deterministic model and documented assumptions.
`validate_library.py --environment-model NAME` labels such providers in its
dependency report; the scripts do not prove their equivalence to a real PLC.

## Compile And Link Boundary

RuSTy accepting all ST sources proves parsing, type checking, and IR generation;
it does not prove that the fuzz target is linkable. The normalization gate must
therefore perform both:

1. project compilation with the normalized library, its compatibility sources,
   `stubs.st`, and the pinned RuSTy ST standard library;
2. final executable linking with the RuSTy runtime archive and every declared
   external implementation.

Treat successful checks as an intake requirement before including a target in
an experiment. The current production `load_project` validates the compatibility
schema and referenced paths; it does **not** read `verification.json` or
automatically enforce a completed intake audit. Production builds still fail
on compilation/link errors, but the operator must review and retain the intake
record.

The validator builds one executable for each repeated `--pou`. It does not run
those executables and does not establish whole-library runtime equivalence.
Its `reachable_externals` check inventories IR external declarations; it is
not a full call-graph reachability proof. Run representative known inputs and
compare required semantics separately before accepting rewrites. A successful
link alone cannot prove preservation of security-relevant behavior.

## Commands

Run from the repository root in the Linux toolchain environment. These are
manual intake steps, separate from the experiment runner:

```bash
# Use a new/empty directory; the original source stays unchanged.
python3 compiler/parser/st-to-rusty-converter/scripts/prepare_library.py \
  path/to/vendor-library --dialect codesys \
  --output-dir artifacts/intake/vendor-library

# Review transformations.json and complete compatibility providers first.
python3 compiler/parser/st-to-rusty-converter/scripts/validate_library.py \
  --workspace artifacts/intake/vendor-library \
  --compatibility-manifest artifacts/intake/vendor-library/compatibility.json \
  --pou POU_NAME

# After checking verification.json and representative runtime behavior:
semantist --st-file artifacts/intake/vendor-library/target.st \
  --function POU_NAME \
  --compatibility-manifest artifacts/intake/vendor-library/compatibility.json \
  --run-dir artifacts/runs/vendor-example --fuzz-timeout 20 --fb-max-cycles 256
```

Replace `target.st` with the normalized source that actually declares the
selected POU. Repeat `--pou` for additional link checks. `--already-compatible`
means copy without rewrites; it does not skip the need to validate the chosen
targets. `--allow-reviewed-rule ID` records an approved review-required rewrite
but does not create equivalence evidence. The reusable profile currently
checked into the repository is `codesys-memory`; include it with
`--profile codesys-memory` only when its interfaces match the library.

The regular pipeline takes the supplied compatibility manifest as an input.
The `main`/`smoke` presets in `run_experiment.py` use the benchmark manifest;
they do not onboard arbitrary libraries or invoke dialect conversion.

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
primitives. Intake scripts can produce compile and native-link evidence;
their records must be reviewed as described above. Conversion is a separate
manual or assisted intake action and is never invoked by the fuzzing scheduler.

The OSCAT intake contract is
`benchmarks/oscat_basic/source/compatibility.json`. The same mechanism accepts
directory-based libraries, so onboarding does not require concatenating every
ST source into one benchmark-specific file.
