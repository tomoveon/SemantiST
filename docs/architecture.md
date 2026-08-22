# SemantiST Architecture

Core capabilities in the current implementation:

- Parse IEC 61131-3 Structured Text declarations for `FUNCTION` and
  `FUNCTION_BLOCK` targets.
- Support scalar IEC types, `STRING`/`WSTRING`, `ARRAY`, and `POINTER` seed
  fields.
- Compile ST through RuSTy, generate STG and LLVM IR, instrument semantic
  targets, generate a C harness, and build an AFL-instrumented target.
- Ingest user seeds, generate bounded type-valid seeds from STG facts, and
  retain runtime-discovered seeds.
- Rank semantic tasks and corpus entries deterministically from STG targets,
  observed coverage, typed dependencies, and execution history.
- Run LibAFL forkserver fuzzing with structured ST mutation, coverage/time
  feedback, runtime finding detection, and content-hash deduplication.
- Record runtime findings, replay evidence, optional minimized reproducers,
  and JSON/Markdown/HTML vulnerability reports.
