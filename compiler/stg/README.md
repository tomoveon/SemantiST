# SemantiST STG Generator

This crate implements the compiler-semantic STG model documented in
[`docs/stg.md`](../../docs/stg.md).

```bash
./compiler/scripts/generate_stg.sh \
  --project-root . \
  --output artifacts/compiler/stg/demo \
  compiler/stg/tests/fixtures/control_flow.st
```

Use repeated `--pou` options to select multiple POUs while compiling a project
only once.

Run its integration tests with:

```bash
LLVM_SYS_211_PREFIX=/usr/lib/llvm-21 \
  cargo test --manifest-path Cargo.toml --jobs 1
```
