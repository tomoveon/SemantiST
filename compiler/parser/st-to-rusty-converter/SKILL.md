---
name: st-to-rusty-converter
description: Perform a one-time, non-destructive intake of a vendor IEC 61131-3 ST function library into SemantiST. Use when an AI agent must normalize CODESYS, TwinCAT, or generic ST, inventory RuSTy/compiler/standard-library/runtime dependencies, create only reviewed stubs or semantic adapters, prove RuSTy parsing and LLVM generation, and native-link selectable FUNCTION or FUNCTION_BLOCK targets before fuzzing.
---

# ST library intake for RuSTy and SemantiST

Treat this Skill as an AI-executed, offline onboarding gate. Run it once for a
function library, then reuse the same compatible library and `stubs.st` for all
of its POUs. Never invoke conversion from SemantiST's fuzzing loop, scheduler,
corpus generation, or seed generation.

## Establish the immutable boundary

Identify the original `.st` file, dialect, output directory, pinned RuSTy
compiler, IEC ST library, and `libiec61131std.a`. Use a fresh directory below
`artifacts/cache/<library>` unless the user supplies another output directory.
Never overwrite or edit the original library.

Before changing anything:

1. Inspect `git status --short` and preserve unrelated/user changes.
2. Read this file and every script in `scripts/`.
3. Inspect the selected RuSTy IEC ST declarations and archive symbols with
   `rg` and `nm -g --defined-only`.
4. Inspect the SemantiST compile, Harness, native-link, and selected POU flow.
5. For OSCAT work, read [references/oscat-codesys-semantics.md](references/oscat-codesys-semantics.md).

## Prepare a derived workspace

Run:

```bash
python3 compiler/parser/st-to-rusty-converter/scripts/prepare_library.py \
  path/to/abc.st \
  --dialect codesys \
  --output-dir artifacts/cache/abc
```

This creates an immutable-source-compatible copy plus:

```text
artifacts/cache/abc/
├── abc.st
├── stubs.st
├── compatibility.json
├── transformations.json
├── verification.json
└── missing-deps/
    └── dependency-report.json
```

Review every transformation. `transformations.json` records source locations,
columns, rule IDs, hashes, and before/after text. Reject transformations that
touch comments/strings incorrectly, change behavior without evidence, or are
not required by the pinned RuSTy compiler.

Rules marked `review_required` are refused unless the AI records equivalence
evidence and repeats `--allow-reviewed-rule RULE_ID` during preparation.
Constructs marked `unsupported` (including vendor object `METHOD` POUs) block
intake; do not flatten them into guessed free functions.

## Resolve dependencies in strict order

For every proposed declaration or implementation, search in this order:

1. RuSTy compiler built-ins and intrinsics;
2. pinned RuSTy IEC ST library declarations/implementations;
3. exported symbols from pinned `libiec61131std.a`;
4. compatible input-library definitions;
5. reviewed library-specific `stubs.st` content.

Use both compiler and linker logs:

```bash
python3 compiler/parser/st-to-rusty-converter/scripts/stubs_analyzer.py \
  artifacts/cache/abc/validation/compile.log \
  artifacts/cache/abc/validation/link.log \
  --compatibility-manifest artifacts/cache/abc/compatibility.json \
  --runtime-archive artifacts/cargo-target/release/libiec61131std.a \
  --environment-model DETERMINISTIC_CLOCK \
  --output artifacts/cache/abc/missing-deps/dependency-report.json -v
```

Do not treat successful LLVM generation as executable validation. The analyzer
parses RuSTy E048/E052 and GNU/lld undefined-symbol diagnostics. In particular,
do not classify `DATE_TO_DWORD` as a RuSTy intrinsic: the pinned toolchain does
not provide it.

## Make auditable dependency decisions

Record every non-provided dependency with exactly one classification:

- `declaration`: genuine external interface with a real link-time provider;
- `semantic_adapter`: reviewed implementation with proven vendor semantics;
- `environment_model`: deterministic clock/network/hardware/runtime model;
- `unresolved`: missing or unproven semantics; block intake.

For an `environment_model`, record its deterministic behavior, assumptions,
state/reset behavior, and impact on experimental conclusions. Never claim it is
fully equivalent to physical PLC hardware.

`stubs.st` may contain missing types/constants, genuine external declarations,
reviewed semantic adapters, and explicit environment models. It must not:

- duplicate or alter a compiler/standard-library symbol or signature;
- add a fixed-zero/default-return executable function to silence compilation;
- infer executable semantics only from a call signature;
- leave an `@EXTERNAL` declaration without a link provider or documented model.

If semantics cannot be established, classify the symbol `unresolved` and stop.

## Prove semantic adapters

Create an adapter only for a syntax alias, vendor-documented behavior, an
available original implementation, or an equivalent composition of RuSTy
primitives. Cite the evidence in the dependency report. Add executable tests
covering normal values, boundaries, truncation, overflow/wrap behavior, and any
vendor-documented undefined range. Keep compatibility logic in the library's
`stubs.st`; never put it in the C Harness.

## Run the completion gate

Validate at least one FUNCTION and one FUNCTION_BLOCK when the library contains
both. Repeat `--pou` for additional smoke targets:

```bash
python3 compiler/parser/st-to-rusty-converter/scripts/validate_library.py \
  --workspace artifacts/cache/abc \
  --compatibility-manifest artifacts/cache/abc/compatibility.json \
  --environment-model DETERMINISTIC_CLOCK \
  --pou FUNCTION_NAME \
  --pou FUNCTION_BLOCK_NAME
```

Do not declare intake complete unless `verification.json` says `passed` and all
of these are true:

1. RuSTy parsing and type checking passed for the full project.
2. LLVM IR was generated.
3. selected POU Harnesses linked into native executables with
   `libiec61131std.a`.
4. every potentially reachable IR external declaration has an inventoried
   provider and no linker undefined reference remains.
5. transformations and dependency decisions are complete and reviewable.
6. `dependency-report.json` contains no `unresolved` entry.

## Hand off to SemantiST

Pass the intake-generated compatibility manifest explicitly:

```bash
SEMANTIST_COMPATIBILITY_MANIFEST=artifacts/cache/abc/compatibility.json \
./compiler/scripts/compile_st.sh path/to/extracted-target.st POU_NAME
```

For a reusable provider, repeat `--profile NAME` while running
`prepare_library.py`. For a library-specific native implementation, repeat
`--native-source FILE`. Intake copies these providers into the derived
workspace, and validation and production builds consume the same manifest.

Use `compiler/scripts/genfunction.py POU_NAME --library <compatible-library>`
to extract any FUNCTION/FUNCTION_BLOCK for review or corpus organization. The
fixed RuSTy IEC ST files and runtime archive belong to the toolchain; do not
copy them into the Skill output.
