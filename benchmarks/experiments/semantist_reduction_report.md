# SemantiST Reduction Report

## Stage 1: Execution Path

The default campaign now uses one Rust native executor, one feedback record,
and one shared corpus. Live symbolic solving, online solver reinjection,
prefix distance selection, adaptive reset/tail guidance, snapshots, automatic
minimization, and traced replay are disabled by default. Native confirmation
still runs three probes for each candidate. LLM planning remains an offline
seed source; its validated seeds enter the same corpus.

Kept contract behavior includes frame and cycle boundaries, call order,
reset/wait/hold/pulse actions, virtual time, ABI metadata, native edge novelty,
state progress, finding candidates, and native confirmation.

The obsolete seven-target pilot manifest/script and its tests were removed.
The default experiment matrix no longer schedules the old live-feedback
comparison.

The no-LLM path also skips loading the large goal-task expression graph and
isolates native target hazards in the worker. This keeps the fixed-byte smoke
path on the same executor, feedback record, and corpus without the old online
solver startup cost. Transition lineage, adaptive reset, tail guidance, and
goal-prefix/distance scheduling are disabled in the default worker profile;
task fields and state paths remain available to semantic mutation.

## Stage 2: Mutation Mix

`semantic-only`, `byte-only`, and `combined` are available. Combined uses a
fixed configurable semantic ratio, default `0.30`, with raw bytes receiving
the remaining `0.70`. The operator is selected once per generated input. The
semantic operator owns field/type boundaries, state paths, cycle actions,
timer thresholds, reset, wait, hold, and pulse. The raw operator mutates bytes
inside the existing frame context with bit/byte flips, random and special
bytes, splice, insert/delete, length boundaries, aggregate members, and string
encoding units.

Engine summaries expose `semantic_mutations`, `raw_mutations`,
`raw_executions`, `raw_retained`, `parser_rejects`, `timeouts`, native edge
novelty, state transition novelty, and finding candidates. Worker results also
record whether the shared corpus was used and whether the run failed.

## Seed Rules

Generated and persisted seeds use `0x...` raw ABI objects. Odd hex digits,
invalid characters, wrong scalar width, and over-capacity values are rejected.
STRING/WSTRING raw storage can fill the complete `N+1` object, including a
missing terminator; textual values remain limited to `N` payload units.
BOOL raw bytes such as `0x03`, `0x80`, and `0xff` are accepted. Pointer,
ARRAY, opaque, STRUCT, and VAR_IN_OUT values use their ABI backing capacity.

## Verification

The fixed smoke plan is `mutation_short.json`: six fixed-seed FUNCTION_BLOCK
targets, all three modes, and ten seconds per job. It records first finding,
native confirmation, executions, scans, throughput, state progress, parser
rejects, timeouts, run failures, raw executions, and raw retained entries.
The 20-target, three-tool, 100-second development pilot remains available via
`improvement_pilot.py`. No 300-second formal matrix was started.

Short smoke results (six targets, ten seconds per job):

| mode | completed | executions | scans | throughput (exec/s) | semantic | raw exec | raw retained | candidates | confirmed | parser rejects | timeout/failure |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| semantic-only | 4/6 | 84,672 | 870,952 | 1,818.35 median | 84,670 | 0 | 0 | 2 | 1 | 0 | 0/2 |
| byte-only | 5/6 | 51,191 | 90,863 | 727.50 median | 0 | 51,188 | 14 | 1,471 | 5 | 0 | 0/1 |
| combined (30/70) | 5/6 | 37,118 | 155,827 | 621.50 median | 11,063 | 26,052 | 16 | 1,509 | 4 | 0 | 0/1 |

The combined run measured 29.8% semantic and 70.2% raw mutations, and raw
mutations both executed and entered the shared corpus. All completed smoke
runs reported zero state-progress events; the failures were the existing XML
reader wall-clock guard and a sequence target exit in semantic-only mode.

The requested 20-target development matrix completed 59/60 jobs. AFL++
completed 20/20 with median 251.14 exec/s; ICSQuartz completed 20/20 with
median 46.79 exec/s and 172 native crash events. SemantiST ran the 20 targets
in the no-LLM configuration because the configured planning endpoint timed
out; 19/20 completed, with 397.26 median exec/s, 356.4 median scans/s,
412,149 semantic mutations, 961,378 raw executions, 58 raw-retained entries,
237 native-edge novelties, 54,218 finding candidates, and 27,541 parser
rejects. XML_READER was the single incomplete SemantiST job because its native
engine exceeded the wall-clock guard. No formal 300-second/5-seed matrix was
started.

Rust: `cargo test -p semantist --locked`, 162 passed. Python: `pytest -q`,
396 passed, 85 skipped, and 14 subtests passed.
