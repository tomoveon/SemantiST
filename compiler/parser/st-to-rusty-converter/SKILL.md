---
name: st-to-rusty-converter
description: Run the deterministic intake of a vendor IEC 61131-3 ST library into SemantiST. Use when a CODESYS/TwinCAT, Siemens SCL or SIMATIC AX, MATIEC/OpenPLC or other supported ST library must be normalized for the pinned RuSTy, checked per POU for type check, LLVM IR and native link, and handed to the fuzzing pipeline through compatibility.json. The agent runs fixed commands and reviews the evidence; it never edits the normalized sources by hand.
---

# Deterministic ST library intake

Intake is an offline step before fuzzing. Normalization is done by the rule
engine in `compiler/intake/normalize`, not by the agent: the same input with
the same toolchain gives byte-identical output, there is no LLM and no network
in the loop, and every edit is recorded. The agent's job is to run the
commands, read the reports, and stop where the reports say the library is not
supported. Never invoke intake from SemantiST's fuzzing loop, scheduler, or
seed generation. The normalization rules and verification probes live under
`compiler/intake/normalize/`.

## Rules for the agent

- Do not edit the original library, the normalized output, or `stubs.st` to
  make a POU compile. A construct the engine rejects stays rejected; report the
  file, line, construct and reason from `transformations.json` or
  `compatibility-report.json`.
- Generate stubs only through `compiler/intake/stubs/`; do not edit them by hand.
  Classify each as `semantic`, `approximate`, or `unsupported`. When the ABI is
  known, a compile-verified default return or empty effect may be an
  `approximate` fallback. Carry that fidelity into the manifest and reports;
  a finding under this replacement model does not establish vendor behavior.
- Do not describe an accepted POU as semantically equivalent. Acceptance means
  type check + LLVM IR + native link with the pinned RuSTy. Behaviour evidence
  is the separate `behaviour` field of the compatibility report.
- A new rewrite belongs in the rule library (`compiler/intake/normalize/rules/`)
  with a probe in `compiler/intake/normalize/probes/` (the pinned compiler
  rejects the original form, accepts the rewrite, and the program prints the
  vendor-documented result), not in a one-off edit. Propose it to the user;
  do not add it silently.
- `rusty_compatibility/` is read only. Outputs go under `artifacts/`.

## 1. Check the toolchain

Run inside the toolchain container (`localhost/semantist:executor-perf`), whose
`/opt/rusty-semantic` is RuSTy `be1de6f175ec1bed7928904dfb694208a24043fb` with
the patches in `compiler/toolchain/rusty-patches/`. `compiler/intake/rusty.py`
records the compiler, standard library and patch hashes in every output; a
result produced with another toolchain is a different result.

## 2. Normalize

For a library from the collected corpus (`rusty_compatibility/<vendor>/`),
extract it first and run the library intake, which also resolves the
dependency closure inside the vendor folder:

```bash
python3 -m compiler.intake.extract run --work-dir artifacts/intake/extract
python3 -m compiler.intake library --vendor 02_CODESYS23 \
  "artifacts/intake/extract/outputs/02_CODESYS23/.st文件/<library>.st" \
  --output artifacts/intake/libraries/02_CODESYS23/<library>
```

For any other `.st` file or directory, choose the dialect profile
(`compiler/intake/normalize/dialects/*.json`: `codesys`, `siemens-scl`,
`simatic-ax`, `iec`, `sysmac`, `br`, `pcworx`, `plcnext`, `gxworks`,
`control-expert`) and run:

```bash
python3 compiler/parser/st-to-rusty-converter/scripts/prepare_library.py \
  path/to/library --dialect codesys --output-dir artifacts/intake/<library>
```

Rockwell Logix is not supported (different program model, too little open
corpus); say so instead of trying another dialect.

## 3. Review the evidence

- `transformations.json`: every rule application with rule id and version,
  triggering diagnostic, span, before/after text; `renames` maps renamed
  POUs/identifiers; `remaining_errors` lists what no rule handles.
- `<file>.st.sourcemap.json`: normalized line to original line.
- `verification.json` (library intake): per unit `type_check`, `ir`, `link`,
  diagnostics and undefined symbols.
- `compatibility-report.json`: syntax intake level (`full`, `partial`,
  `none`) and behaviour evidence (`rule_equivalence`,
  `unverified_environment`, `needs_platform`, `tested`), pragmas that RuSTy
  ignores, time sources, implicit check functions that are not in effect, and
  representation adapters with their assumptions.
- `dependency-report.json`: providers of external symbols. An `unresolved`
  entry blocks the affected POU only when no semantic or ABI-known approximate
  stub is selected; an accepted approximate stub continues through IR/native
  checks and remains visible as replacement-model evidence.

Report the result per POU as accepted / rejected with the first reason. If the
target POU is not accepted, stop and report; do not work around it.

## 4. Link check of selected POUs

`python3 -m compiler.intake library` and `validate_library.py` both build
every unit and write `accepted/`: the normalized files with the rejected units
blanked (same lines), listed as `accepted_sources` in `compatibility.json`, so
that an accepted POU compiles and links even when other units of its library
are rejected. `validate_library.py` does this with the current `stubs.st`, so
run it again after reviewing stubs. Link the selected targets:

```bash
python3 compiler/parser/st-to-rusty-converter/scripts/validate_library.py \
  --workspace artifacts/intake/<library> \
  --compatibility-manifest artifacts/intake/<library>/compatibility.json \
  --pou FUNCTION_NAME --pou FUNCTION_BLOCK_NAME
```

`verification.json` must say `passed`; a POU the intake rejected is reported
as rejected, with a pointer to `unit-verification.json` (written by
`validate_library.py`). A `declaration`-only provider is valid
only when the final link supplies the symbol; time, network and hardware
interfaces need a named, deterministic environment model
(`--environment-model NAME`), documented as an assumption.

## 5. Hand off to SemantiST

Pass the generated contract explicitly; it carries the compiler arguments
(for example `--byte-pointer-arithmetic` for CODESYS) and the STRING byte range
of the dialect:

```bash
semantist --st-file artifacts/intake/<library>/accepted/<normalized file>.st \
  --function POU_NAME \
  --compatibility-manifest artifacts/intake/<library>/compatibility.json \
  --run-dir artifacts/runs/<name> --fuzz-timeout 20 --fb-max-cycles 256
```

Use the renamed POU name from `transformations.json` when the target was
renamed. For OSCAT-specific semantics, read
[references/oscat-codesys-semantics.md](references/oscat-codesys-semantics.md).
