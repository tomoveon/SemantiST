# Running comparison experiments

Use the root `run_experiment.py` entry for both smoke checks and main runs. It
uses small named presets in the style of ICSQuartz; target preparation,
per-trial measurement, CPU scheduling and validation remain in the existing
runner. Deployment and offline interpretation stay manual. Full server steps:
[Chinese runbook](../../docs/server_experiment_runbook_zh.md).

The 2026-10-12 change switches collection to `post_run`. It has only been
edited and reviewed as source; no tests, smoke runs, builds or validation
experiments were executed for this change. Short runs are optional, not a
required step before starting a new main campaign.

## Setup and routine commands

Build the SemantiST image from the same clean revision as the host checkout,
and build the AFL++ / ICSQuartz images using the
[baseline instructions](../baselines/README.md). Export `SEMANTIST_IMAGE` with
the built image ID if it differs from the default `semantist:0.1.0`.
For this experiment-script-only update, reuse the existing toolchain and
baseline images after syncing the host source. New runs still prepare targets
and build per-target ICSQuartz images. The runbook includes the Windows commit
and push steps, a clean Linux checkout update, and the existing server image ID.

Run from the repository root on the Linux host. Choose CPU IDs allowed by the
server affinity mask; `2-9` below is only an example.

```bash
python run_experiment.py --experiment main --run-id main_postrun_01 --cpus 2-9
# Optional preset, only when a short run is wanted.
python run_experiment.py --experiment smoke --run-id smoke_postrun_01 --cpus 2-9
```

| Preset | Tools | Targets | Online seconds | Trials | Default CPUs |
| --- | --- | --- | --- | --- | --- |
| `smoke` | SemantiST, AFL++, ICSQuartz | 3 representative targets | 20 | 1 | `1-2` |
| `main` (default) | Same | All 90 manifest targets | 300 | 5 | `1-8` |

Smoke targets are `oscat_basic_charname`, `icsfuzz_bf_mcpy_1`, and
`scan_cycle_aircraft_oobw_4`. Explicit options override the preset:

```bash
# Optional all-target run with a short budget; not a main-run prerequisite.
python run_experiment.py --experiment main --run-id acceptance_01 \
  --fuzz-time 20 --fuzz-trials 1 --cpus 2-9

# An independent tool and selected targets, if needed.
python run_experiment.py --tools semantist --run-id selected_01 \
  --target-id oscat_basic_inc2 --cpus 2-9
```

`--target-id` is repeatable and replaces the preset target list. `--tools`
accepts one or more comma-separated names. `--fuzz-time` and `--fuzz-trials`
set the per-trial budget and number of repetitions. `--help` shows routine
options; `--help-all` includes advanced settings. Existing `--experiment-id`
and `--trials` are aliases for `--run-id` and `--fuzz-trials`.

Optional `--preflight-only` writes the plan and static support matrix without
building or running targets. It reserves its own ID and does not prove online
execution works. Every invocation requires an unused output ID. Failures
retain their records; fix the cause and rerun the selected group under a new
ID. There is no automatic resume, preprocessing reuse or overwrite.

A single runner prepares all selected targets before online execution and
shares the specified CPU pool across all tools. Each trial has one fuzz
worker on one CPU slot. ICSQuartz's broker/respawner and single client share
that slot; the container-local Launcher core ordinal is `0`. `--serial`
selects one trial at a time. Separate runner processes must use disjoint CPU
pools; there is no global CPU reservation. Avoid concurrent heavy builds and
online measurements. Ctrl+C may wait for the current batch; after interrupting
preprocessing, check for residual build containers before starting again.

If requested separately, a successful smoke checks the interface; it need not
find a crash and is not a tool ranking. ICSQuartz uses checked-in fixtures and a pinned image, with
no external checkout. Optional ICSFuzz still needs CODESYS artifacts and
calibration.

## Measurement settings

Each tool retains its native seed policy. SemantiST starts from the checked-in structured seed and performs STG-based semantic initial generation inside the online budget. AFL++ starts from the two bytes `31 0A` (`echo 1` in the ICSQuartz baseline). ICSQuartz retains its upstream forced evaluation of ten inputs: the initial PLC bytes, empty, one-byte zero, full zero, full `0xff`, half length, alternating `0xaa/0x55`, repeated `0xab`, increasing, and decreasing bytes. Compilation and static preparation are recorded separately. No historical corpus is imported. SemantiST's cycle limit defaults to 256 (`--semantist-fb-max-cycles`, range 1–10000); use the same limit across compared configurations.

SemantiST runs each input in its own process by default (`--semantist-execution inprocess`): preparation also links the target into `semantist-inprocess`, whose engine resets the target's writable globals between inputs and restarts after a crash. `persistent` keeps the AFL++ forkserver child for many inputs, and `fork` forks a child per input as earlier batches did. The mode is recorded in `environment.json` and in `tool_specific_metrics.execution`. All three report the same native execution counter: every executed testcase counts, including mutants that repeat an earlier input, and the counter continues across in-process restarts. `tool_specific_metrics.scan_cycles_reported` is the PLC scan-cycle counter from the same native report. Replay and coverage replay always use the forkserver binary `fuzz_target` with the input on stdin.

The numeric RNG seed is paired by target and trial, not by tool or output
directory. Independent invocations therefore use the same seed namespace by
default (`semantist-comparison-v1`). Set `--seed-namespace` explicitly when a
new experimental campaign needs a different but still cross-tool-paired seed
series.

The online comparison interface uses AddressSanitizer as the common memory
fault detector and builds native targets with `-O1 -fno-omit-frame-pointer`
where the toolchain exposes those flags. UndefinedBehaviorSanitizer is not
enabled for only a subset of tools. ICSQuartz uses ASan, its ordinary crash
feedback, original in-process executor, and upstream Launcher. The Launcher
records a crashing client and restarts it; scan-cycle state remains alive
between non-crashing inputs in the same client.
Its upstream alternative non-aborting ASan feedback is deliberately disabled:
on this common IR it exited during seed execution, including when the target
was built with `-fsanitize-recover=address`. This is a documented deviation
from the upstream memory-primitive experiments, not a claim to reproduce
their exact performance configuration. An early ICSQuartz exit is invalid even
if ASan sets exit code 0. Native crash artifacts are candidates, not unique
vulnerabilities.

SemantiST's function-block state trace is off by default. Bounded abstract
state feedback uses shared memory independently of that trace. Enabling
`--semantist-fb-state-trace` adds diagnostic output and I/O; label such runs
separately rather than mixing trace-on and trace-off trials.

The AFL++ and ICSQuartz baseline targets consume the same SemantiST-prepared
LLVM IR, IEC standard library, compatibility sources, and checked harness.
ICSQuartz contributes its LibAFL instrumentation and fuzzing engine after that
common target preparation step; it does not substitute a different PLC memory
layout.

Stateful execution is not identical across tools: SemantiST uses semantic
sequences for `FUNCTION_BLOCK`; ICSQuartz enables scan-cycle mutations and
resets for the dedicated `icsquartz_scan_cycle` suite; AFL++ and other
ICSQuartz function blocks run a single cycle per testcase. Report execution
models with results, especially for function blocks. A normal main run does
not require rewriting those native interfaces to be identical.

Collection uses `collection_mode=post_run`. The online budget has no recurring
candidate, stats or log collection and no native-counter watchdog. Each tool
runs until it exits or its budget expires; after stopping and confirming
cleanup, the adapter reads its native counters, candidates and metadata.
`--observer-poll-interval-ms` remains a compatibility argument, but its value
is ignored and the effective value is `0`.

AFL++ native `saved_crashes`/`saved_hangs` counters must match the final
artifacts visible to the host. ICSQuartz retains its upstream Launcher so an
in-process crash can be recorded and its one client restarted. Final counters
and logs are checked after stopping; sparse native reporting no longer causes
an online `watchdog_timeout`. Collected inputs remain candidates, not unique
bugs. An incomplete collection is recorded explicitly rather than reported
as a successful run with no findings.

## Outputs and offline comparison

After every stopped batch, the runner persists available trial records and
exports per-trial metrics, including failure and validity fields. A table's
existence is not evidence of successful validation. Invalid or interrupted
campaigns retain partial records and return a nonzero exit status; optional
aggregation remains gated by successful validation.
The selected preset is saved as `experiment` in the manifest and environment;
resolved targets and actual budgets remain recorded separately.

```text
artifacts/experiments/<run-id>/
  environment.json
  experiment-manifest.json
  support-matrix.csv
  schedule.jsonl
  campaign-status.json        # completed, failed and not_started run keys
  runs/<tool>/<suite>/<target>/trial_XX/
    run-result.json             # authoritative trial record and paper_metrics
    process-result.json        # process lifecycle evidence, when provided
    post-run-findings.json     # candidate snapshot collected after stopping
    ...native inputs, reports, commands, stdout/stderr...
  raw-runs.jsonl               # partial until all scheduled rows are recorded
  metrics.jsonl
  metrics.csv
```

The measurement contract is `semantist.independent-tool-metrics/2`. Do not
combine these post-run records with legacy `/1` online-observation campaigns.
Native crash candidates, associated execution counts and inputs, final
reported executions, throughput, sources and validity are still collected.
A valid trial requires supported execution, full budget completion, complete
candidate collection, no failure reason, and a positive native execution
count. Early exits remain recorded but are invalid for the fixed-budget
comparison. Missing measurements stay null (empty CSV cells); an empty
candidate list with `candidate_collection_complete=false` does not establish
that no crash occurred.

All tools now use `native_stop_snapshot_may_include_shutdown_tail` for the
finding and total-execution windows. The snapshot can include work performed
during stopping, and native reports can lag actual execution. Neither counts
nor candidates imply an exact host-budget cutoff. Unified host-observed
`first_crash_time_seconds` is null because no online observation was made.
Native candidate times, when present, remain separate in
`first_crash_native_time_seconds` with `first_crash_native_time_origin`.
The first candidate is selected by native execution count only when every
crash candidate has that count; otherwise its order stays unknown. Execution
models remain explicit because one testcase can contain multiple PLC cycles.

A trial-level tool or collection failure continues to later batches when
cleanup is confirmed. Infrastructure failure, unconfirmed cleanup or inability
to persist a trustworthy result stops subsequent batches. Campaign status
retains completed, failed and not-started rows; there is no automatic retry or
resume, and an invalid matrix is not silently aggregated as a successful one.

After all runs finish:

```bash
python experiments/comparison/analysis/compare_tools.py \
  --experiment-dir artifacts/experiments/main_01 \
  --out artifacts/comparisons/main_01
```

This reads authoritative trial files and writes `metrics.csv`, `metrics.jsonl`, `comparison.csv`, and `comparison.json`. Repeat `--experiment-dir` if tools were run separately. It requires at least two tools and accepts only SemantiST `configuration_id=full`. It checks matching fixtures, budgets, trials and paired seeds, rejects duplicate/missing records, and reports per-target summaries. Use `common_supported_target=true` rows for direct multi-tool comparisons. Read `comparability_notes` for execution model and CPU differences. File hashes alone do not prove equivalence between compiler/runtime implementations.

Success rates use valid trials as denominator and report invalid trials alongside them. Time/count medians are explicitly successful-trial-only: report success rates and missing-count totals too. Native candidates are not automatically replay-confirmed crashes or unique bugs.

The intended main workflow stops at these raw records. Replay, root-cause
deduplication, statistical testing, and final interpretation can be performed
manually or as explicitly requested offline steps.

SemantiST mechanism ablations use the same online interface and raw records;
see [the ablation profiles and runners](../ablations/README.md).

## Optional replay and verification

Replay is separate from the online budget:

```bash
python -m experiments.comparison.analysis.replay \
  --experiment-dir artifacts/experiments/main_01 \
  --container-runtime podman
```

Use `docker` instead on a Docker host. The main runner and offline container
analysis accept both runtimes; `auto` probes Docker first and then Podman.

Rerun offline comparison afterwards to refresh exports. Replay uses stdin or a file argument according to the tool. Unsupported conversion/history remains unverified. The current ICSQuartz adapter does not supply a standalone replay binary; persistent scan history is not reconstructed. Replay never erases the original discovery count/time. Native coverage is diagnostic, not a cross-tool score.

Optional regression commands for a separately requested validation task
(not executed for this change):

```bash
python -m unittest discover -s tests/unit -p test_comparison_experiment.py
python -m unittest discover -s tests/unit -p test_independent_experiments.py
```

`run_full_comparison.py`, `run_complete_comparison.py`, `run_smoke_comparison.py`, and `run_tool.py` remain compatibility entries using the same runner. Routine commands use `run_experiment.py`. This source change has not been accepted through a server run; historical local interface tests do not cover the new collection mode.
