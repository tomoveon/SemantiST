# KLEE library models (R5)

Every symbol in these models is classified as one of:

- **faithful** — same value/state transition as the RuSTy standard library
  implementation; covered by a native comparison test.
- **concretizing** — uses `klee_get_value`/native arithmetic on a concretized
  argument; the result is a real computation for that path.
- **havoc** — returns a fresh unconstrained symbolic value because the library
  operation has no exact bitcode model yet.
- **approximating** — uses a bounded approximation; native replay is required
  and a negative result is not a completeness claim.

When a query reaches a `concretizing`, `havoc`, or `approximating` function, the query result is
reported in `solver_scope.imprecise`; a failure to find a model is then
`UNKNOWN`, never `UNSAT`.

## `stdlib_stubs.c`

| Function group | Class | Notes |
| --- | --- | --- |
| `TIME` | faithful | virtual clock (patch 0007) |
| `TON` / `TOF` / `TP` and `_TIME`/`_LTIME` aliases | faithful | ST instance layout, virtual clock, `iec_ge`/`rusty_gt` |
| `R_TRIG` / `F_TRIG` | faithful | RuSTy `flanks.rs` rising/falling edge with one-bit memory |
| `LIMIT`, `MAX`, `MIN`, `DATE_AND_TIME_TO_*` | faithful | direct integer/real operations |
| `ROUND__REAL` | approximating | half-away-from-zero conversion through `int64_t`; extreme values and symbolic floats remain imprecise |
| `DWORD_TO_REAL`, `REAL_TO_DWORD` | faithful | IEEE-754 bit reinterpretation |
| `ROL__DWORD`, `ROL__BYTE`, `ROR__BYTE` | faithful | fixed-width rotate |
| `MAX__TIME` | faithful | maximum of the supplied TIME array |
| `STRING_TO_DINT` | approximating | bounded signed conversion with RuSTy radix prefixes; native replay remains authoritative |
| `LEN__STRING`, `MID_EXT__STRING` | faithful | byte string length/substring |
| `ACOS/ASIN/ATAN/COS/SIN/TAN/LN/SQRT` | havoc | no exact libm bitcode is linked; fresh symbolic result |
| `EXP__REAL` | approximating | bounded series approximation, with native replay required |
| `DAY_OF_WEEK` | faithful | modulo of the input value |

## `memory_stubs.c`

| Function group | Class | Notes |
| --- | --- | --- |
| `__SEMANTIST_MEMCPY/MEMMOVE/MEMSET(_CHECKED)` | faithful for data movement | intentionally omits the native ASan abort; native execution is the oracle |
| `__SEMANTIST_BITCPY` | faithful | bit-level copy |
| `calloc` | faithful | zero-filled allocation |

## `libc_stubs.c`

| Function group | Class | Notes |
| --- | --- | --- |
| `memcpy`, `memset`, `malloc`, `free`, `strlen`, `strtol`, `__ctype_b_loc` | faithful/opaque | freestanding implementations for the target's startup path |
| `abort` | faithful | `klee_report_error(..., "abort")`; a crash objective is signalled to KLEE |

## Evidence

Each supported KLEE bundle records `model_classes` (`faithful`,
`concretizing`, `havoc`, `approximating`) and `imprecise` in its
`klee/manifest.json`. Query results add contract/stub fidelity and query
status; these are separate from native finding confirmation. Historical batch
counts do not validate the current 100-target OSCAT roster.
