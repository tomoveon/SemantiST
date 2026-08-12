# OSCAT Basic benchmark suite

This suite contains 61 real OSCAT Basic targets: 31 `FUNCTION` targets and 30
`FUNCTION_BLOCK` targets. The machine-readable target index is `manifest.json`;
entries with `pipeline: true` are expected to run through the current SemantiST
pipeline.

The suite covers scalar and structured inputs, `STRING`, arrays, pointers,
`VAR_INPUT`, `VAR_IN_OUT`, `VAR_OUTPUT`, internal state, constants, retained
state, and multi-cycle execution.

## Function targets

`INC2` contains a modulo-by-zero boundary. The trigger sets `U = L - 1`, making
`tmp := U - L + 1` equal to zero before `MOD`.

`_STRING_TO_BUFFER` exercises bounded pointer-like storage and canary checking,
so oversized writes can be observed as crashes, sanitizer findings, or canary
corruption.

## Function-block targets

Representative targets include:

- `LIST_NEXT`: `VAR_INPUT`, `VAR_IN_OUT`, `VAR_OUTPUT`, and internal state.
- `SCHEDULER_2`: OSCAT constant-input convention.
- `FIFO_16`: constant fields and internal array state.
- `CALIBRATE`: retained state.

Function-block seeds use `NAME,TYPE,VALUE` records with an optional cycle
prefix:

```text
0.SEP,BYTE,59
0.RST,BOOL,1
LIST,STRING,;alpha;bravo;charlie
1.SEP,BYTE,59
1.RST,BOOL,0
```

The harness constructs one block instance, applies inputs cycle by cycle, and
preserves internal state. Inputs and `VAR_IN_OUT` fields are fuzz-controlled;
outputs and internal state are observed. The generic oracle covers crashes,
timeouts, OOM, sanitizer failures, and exact instrumented semantic violations.

## Compile check

```bash
./benchmarks/oscat_basic/check_rusty_compile.sh
```

## Run

```bash
semantist \
  --st-file benchmarks/oscat_basic/functions/inc2/target.st \
  --function INC2 \
  --compatibility-manifest benchmarks/oscat_basic/source/compatibility.json \
  --run-dir artifacts/runs/inc2-example \
  --fuzz-timeout 300
```

Run the semantic suite with:

```bash
./benchmarks/oscat_basic/run_semantic_suite.sh
```

Reusable build products are stored under `artifacts/build/targets/`; isolated
run corpora, events, findings, semantic state, and reports are stored under
`artifacts/runs/`.

Generate or regenerate a report with:

```bash
semantist-report \
  --run-dir artifacts/runs/inc2-example \
  --function INC2 \
  --st-file benchmarks/oscat_basic/functions/inc2/target.st \
  --llm-provider mock
```

## Toolchain

Set the LLVM toolchain used to compile instrumented targets:

```bash
export SEMANTIST_LLVM_BIN=/usr/lib/llvm-21/bin
```

Additional target sanitizers can be enabled explicitly:

```bash
export SEMANTIST_TARGET_SANITIZERS="-fsanitize=address,undefined"
```
