# Experiment runner

This runner declares five comparisons: overall effectiveness, three core
ablations, scan-cycle guidance, budget sensitivity, and case/cost analysis. The
last item reuses the other runs. The 20-target subset in
`selection.json` was chosen from the OSCAT manifest by POU kind and manifest
order, before search results. The unique matrix has 3,700 search jobs: 3,500 at
300 seconds and 200 at 600 seconds. A job reused by several comparisons runs
only once.

The runner defaults to a dry run. Build the combined image once:

```bash
podman build --no-cache --format docker -f benchmarks/experiments/Containerfile -t semantist:experiment .
```

SemantiST online trials use one native executor, one feedback stream, and one
shared corpus. `combined` uses a fixed semantic/raw split of 30%/70%; use
`--mutation-mode semantic-only|byte-only|combined` and
`--semantic-ratio 0.30` to select the operator mix. The fixed-seed smoke set
for SMTP_CLIENT, IRTRANS_DECODE, NET_VAR_BUFFER, XML_READER, PARSET, and a
multi-cycle SEQUENCE_64 target is described in
[`mutation_short.md`](mutation_short.md). It is separate from the 20-target
100-second development pilot and the formal matrix.
The implementation and verification record is in
[`semantist_reduction_report.md`](semantist_reduction_report.md).

The image includes the SemantiST toolchain, AFL++ 4.09c and the pinned
ICSQuartz engine from `semantist-icsquartz-env:8021bd4`. Then inspect or smoke
test the overall comparison:

```bash
python3 -m fuzzer.cli.experiment matrix --experiment overall
python3 -m fuzzer.cli.experiment run --execute --experiment overall \
  --target oscat_basic_inc2 --tool aflpp --seed 0 --smoke-seconds 5 \
  --jobs 1 --build-jobs 1 --root /tmp/semantist-experiment-smoke
```

For the formal main experiment (100 targets, three tools, five independent
repetitions, 300 seconds online per trial), prepare all binaries and frozen
LLM plans before starting any timed search. Run from the repository root:

```bash
set -euo pipefail
IMAGE_TAG="semantist:experiment-$(date +%Y%m%d-%H%M%S)"
podman build --no-cache --format docker -f benchmarks/experiments/Containerfile -t "$IMAGE_TAG" .
RUN_ROOT="/tmp/semantist-experiment-main-$(date +%Y%m%d-%H%M%S)"
python3 -m fuzzer.cli.experiment prepare --execute --allow-formal \
  --experiment overall --root "$RUN_ROOT" --image "$IMAGE_TAG" \
  --llm-env-file "$PWD/.env.semantist-llm" \
  --build-jobs 8 --plan-jobs 4 \
  2>&1 | tee "$RUN_ROOT-prepare.log"
python3 -m fuzzer.cli.experiment run --execute --allow-formal \
  --experiment overall --root "$RUN_ROOT" --image "$IMAGE_TAG" \
  --schedule target_waves --jobs 15 \
  2>&1 | tee "$RUN_ROOT-run.log"
python3 -m fuzzer.cli.experiment summary --experiment overall \
  --root "$RUN_ROOT"
```

Smoke mode permits at most two targets, six jobs, and ten seconds of online
search per job. Full 300/600-second searches require both `--execute` and
`--allow-formal`. Pass `--llm-env-file` when preparing full SemantiST plans.
Builds are shared per target; planning runs once per target; each search gets an
independent working directory and seed. `/tmp/semantist-experiment` is the default
working root. Move or archive completed artifacts separately. `--retry-failed`
preserves old directories with a `.previous-N` suffix before restarting.
Each `requests/...json` records the target, tool, configuration, seed, online
budget, frozen build/plan package paths, output directory, two assigned
physical CPU IDs and memory limit.

One trial is one target, one tool, one random seed and one online budget.
`--schedule target_waves --jobs 15` submits all five seeds for each of the
three tools in one target wave. The next target starts after the entire wave,
including per-trial review, has finished. Each trial has two physical cores
and an 8 GiB container limit, so a full wave uses 30 physical cores and has a
combined 120 GiB memory limit. On the 64-core EPYC host, the three 10-core
bands are CPUs 0-9, 10-19 and 20-29. Tool-to-band assignments rotate every
target: SemantiST/ICSQuartz/AFL++ use bands A/B/C, then B/C/A, then C/A/B.
Avoid heavy host work on CPUs 0-29 and sibling threads 64-93 during the run.
The root lives on the NVMe-backed `/tmp`; archive results after completion.

Each container is pinned to two distinct physical cores with `taskset`, and
Podman applies a two-CPU quota and an 8 GiB memory limit. Search containers
have separate network namespaces and see the experiment root read-only; only
their own run directory parent is mounted writable. Each trial has its own
corpus, solver files, logs, result directory and random seed. One LLM plan is
generated and frozen per target, then reused by its five SemantiST trials.
AFL++'s two processes synchronize only inside one trial;
the five repetitions never exchange their new corpus entries. ICSQuartz's
two workers share only that trial's corpus and broker. These controls prevent
the scheduler's trials from sharing search state. They cannot eliminate
contention for shared cache, memory bandwidth and storage, or interference
from unrelated host processes. `--jobs`,
`--build-jobs`, and `--plan-jobs` control independent concurrent jobs, not
in-process native probes. The scheduler is the only writer of `summary.json`
and `per-target.json`.

## Baseline adapters

`compiler/harness/common.py` generates the Common PLC Harness from the target
ABI and LLVM layout. `compiler/harness/semantist_adapter.py` is the SemantiST
Sequence Adapter: it is the only place that converts LLM/KLEE frame plans and
`0.field` sequences to raw records for a SemantiST-owned execution. The
baseline adapters do not import it.

For a short interface smoke in the experiment container, build one FUNCTION,
one FUNCTION_BLOCK and one string/pointer target, then run each baseline for
five seconds:

```bash
python3 -m fuzzer.cli.experiment run --execute --experiment overall \
  --target oscat_basic_inc2 --tool aflpp --seed 0 --smoke-seconds 5 \
  --jobs 1 --build-jobs 1 --root /tmp/semantist-common-smoke
python3 -m fuzzer.cli.experiment run --execute --experiment overall \
  --target oscat_basic_delay --tool icsquartz --seed 0 --smoke-seconds 5 \
  --jobs 1 --build-jobs 1 --root /tmp/semantist-common-smoke
python3 -m fuzzer.cli.experiment run --execute --experiment core_ablation \
  --target oscat_basic_uppercase --tool semantist --configuration no_llm \
  --seed 0 --smoke-seconds 5 \
  --jobs 1 --build-jobs 1 --root /tmp/semantist-common-smoke
```

The same command can be repeated for `oscat_basic_uppercase` or another
STRING/pointer target. The SemantiST smoke uses `no_llm` only to avoid requiring
an LLM endpoint; it still builds and runs the semantic IR/runtime path. This is
an interface check only and does not alter the formal matrix.

`adapters.json` runs the installed AFL++ and pinned ICSQuartz engines against
the generated Common PLC Harness. AFL++ uses two cooperating fuzz processes;
ICSQuartz uses its two-core LibAFL launcher with its scan-cycle observer and
mutation for function blocks. Both receive fixed-size raw ABI bytes and are
replayed by their own native binary. AFL++ starts from its own raw ABI seed; ICSQuartz
uses its internal initial-input generator. Neither adapter reads the SemantiST
text frame corpus or semantic runtime artifacts.

Adapter commands are argument arrays with no shell interpolation. Supported
placeholders for build commands include `{common_build}`, `{source}`,
`{original_source}`, `{function}`, `{output}` and `{compatibility_manifest}`.
The build command must write
`{output}/build-result.json`, including `status` (`supported` or `unsupported`),
`reason` where needed, `version`, `canonical_testcases: true`, and
`oracle_equivalent: true` only when the adapter can honor the common execution
contract. Build commands can also use `{normalized_source}`, `{abi}`, `{contract}`,
and `{initial_corpus}` for SemantiST-owned adapters. The run command receives
`{source}`, `{function}`, `{seed}`, `{seconds}`, `{build}`, `{output}`, `{abi}`,
`{contract}`, and `{replay_binary}`. It must write
`{output}/adapter-result.json` with `online_elapsed_s`:

```json
{
  "status": "completed",
  "testcase_format": "common-raw",
  "cold_reset": true,
  "online_elapsed_s": 300,
  "executions": 0,
  "scans": 0,
  "candidates": [
    {"testcase_file": "candidates/one.seed", "first_candidate_s": 12.5}
  ]
}
```

Candidate files are raw bytes emitted by the native fuzzer. The adapter
replays each candidate with the same native baseline binary and records the
return code and timeout in its result. An adapter that cannot provide the raw
input and reset contract must report `unsupported`, not a finding count. The
tool's own binary, seed, preparation time, and version belong in its build
package and run logs. Formal runs stop before building if any adapter is
unconfigured.

## Results and timing

The SemantiST build package keeps the original library source, the normalized
target actually compiled, the compatibility project with its relative source
layout, both the SemantiST and Common Harness artifacts, ABI, STG and tool
versions. The package records input file
sizes and modification times; a changed input requires a new experiment root
and rebuild. No input hashes are calculated. Formal runs check baseline build
and run adapters, LLM configuration, and container limits before preparing
targets. The read-only plan package keeps its
seeds, suggestions, prompt, costs, feedback, and planning-stage findings.
Only plan seeds and suggestions enter the five online trials. Planning-stage
findings remain in the plan package and are never counted at online time zero.
Target compilation, STG generation, IR instrumentation, baseline binary
preparation and the one frozen LLM plan per target finish before any online
search trial is scheduled. AFL++ copies its independent raw seed before its
online timer starts.

The online clock starts immediately before the native engine and ends when it
stops. Native confirmation and root-cause review follow in a separate phase.
Rust candidate metadata records when a candidate was emitted. The default
search records native edge novelty, state progress, finding candidates, and
mutation/corpus counts in `metrics/engine-summary.json` and
`metrics/events.jsonl`; it does not run a live solver or traced replay.
The result excludes candidates emitted after the online endpoint and reports
their count separately; the raw campaign report remains available. Each
finding's `first_candidate_end_to_end_s` charges that target's build and frozen
planning costs once before its online candidate time. Shared tool installation
and common engine bootstrap are recorded in logs, outside this target-level
measure.

`summary.json` reports every planned run, including unsupported and failed
ones. It counts build and LLM planning costs once per target, and separately
reports online/review time, CPU, memory, throughput and feedback events.
`per-target.json` reports each target's five-run discovery rate. `cases.json`
indexes each online confirmed finding and its reproducer across runs, with
root-cause IDs only where manually adjudicated. The
`independent_defects_adjudicated` field counts only manually assigned root
causes. To assign them, create `root-causes.json` in the experiment root with
keys listed in `summary.json`'s `unadjudicated_locations`, mapping each key to
a reviewed root-cause ID. Until then, independent defect counting is
incomplete. Smoke output is only an interface check, never a formal result.
