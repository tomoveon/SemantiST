# Experiment runner

This runner declares five comparisons: overall effectiveness, three core
ablations, scan-cycle guidance, budget sensitivity, and case/cost analysis. The
last item reuses the other runs. The 20-target subset in
`selection.json` was chosen from the OSCAT manifest by POU kind and manifest
order, before search results. The unique matrix has 3,500 search jobs: 3,300 at
300 seconds and 200 at 600 seconds. The formal `overall` main experiment is
1,500 of those jobs (100 targets x 3 tools x 5 seeds, all at 300 seconds).
A job reused by several comparisons runs
only once.

The runner defaults to a dry run. Build the combined image once:

```bash
podman build --no-cache --format docker -f benchmarks/experiments/Containerfile -t semantist:experiment .
```

For a preliminary core-ablation check, run `bash benchmarks/experiments/run_ablation_quick.sh`.
It prepares 100 targets once and runs `full`, `no_llm`, `no_klee`, and
`no_native_stg` with seed 0 at 300 seconds each (400 trials). The four groups
run concurrently on physical CPU IDs 0-7, 8-15, 16-23, and 24-31, respectively.
Each group runs four two-core trials at a time. The script checks that all 32
CPU IDs are available and refer to distinct physical cores, then writes one
summary after all four groups finish. This single-seed check is not the formal
five-seed ablation result. Keep heavy host work off CPUs 0-31 and sibling
threads 64-95 during the run.

For the full core ablation, run `bash benchmarks/experiments/run_ablation_full.sh`.
It prepares 100 targets, then runs five seeds for each of four configurations
(2,000 trials, 300 online seconds each). The groups run concurrently on
physical CPU IDs 0-9, 10-19, 20-29, and 30-39, with five two-core trials
per group. The script uses `localhost/semantist:exp1` by default; set
`SEMANTIST_ABLATION_IMAGE` to use another installed image.

Before each frozen LLM plan, preparation retrieves target-specific chunks
from the three OSCAT PDF manuals through a page-addressed SQLite FTS5 index.
It also supplies the frozen target ST source and comments. The first LLM phase
proposes up to three typed output requirements, marked `exact` or `estimated`
and tied to evidence. A fixed generator validates and compiles them into a C
harness monitor. The second LLM phase plans tests against that checked binary.
`no_llm` uses the base binary; the other configurations share the checked
binary and frozen plan. Generated property violations are reported separately
from native crash and bounds findings. PDFs are cached under
`artifacts/oscat-manual-cache`; each experiment freezes its own index and
source excerpts. Other libraries require their own supplied documentation.

For the development improvement check, run `bash benchmarks/experiments/run_improvement.sh`.
`improvement_targets.json` freezes the 36 FUNCTION_BLOCK targets with zero
confirmed SemantiST findings across all five `overall/full/300s` trials in
`overall-20261110T111746`. The script verifies this selection against that
result, then prepares and runs one `seed=0`, 100-second SemantiST trial per
target in a new `artifacts/experiments/improvement-*` root. The `improved`
configuration keeps LLM planning and offline KLEE seeds, with online KLEE
disabled. Four concurrent trials use eight distinct physical cores. This is
an exploratory development run, not a matched overall comparison. The script
also writes `candidate_recheck.json` after independently replaying saved
native candidates. Numeric boundary observations are tracked separately from
findings because integer wrapping is exploratory under the execution contract.

The earlier hand-written `FILTER_MAV_DW` property remains available for
standalone opt-in use through `--property-file`; it is not part of the formal
ablation manifest. Generated and opt-in property findings appear under
`declared_property_findings`, while shared crash and bounds rates use only
`findings`. A changed manifest or source file requires a new experiment root.

SemantiST online trials use one native executor, one feedback stream, and one
shared corpus. `combined` uses a fixed semantic/raw split of 30%/70%; use
`--mutation-mode semantic-only|byte-only|combined` and
`--semantic-ratio 0.30` to select the operator mix. The fixed-seed smoke set
for SMTP_CLIENT, IRTRANS_DECODE, NET_VAR_BUFFER, XML_READER, PARSET, and a
multi-cycle SEQUENCE_64 target is described in
[`mutation_short.md`](mutation_short.md). It is separate from the 20-target
100-second development pilot and the formal matrix.
The historical development record, which predates the current formal trial
configuration, is in
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
LLM plans before starting any timed search. From the repository root, this
single command builds the image, completes prepare, launches the three tool
commands concurrently in their disjoint CPU pools, waits for all three, and
writes the final summary:

```bash
bash benchmarks/experiments/run_overall.sh
```

The equivalent manual commands below run the tools sequentially; they are
useful when controlling or retrying one tool at a time:

```bash
set -euo pipefail
IMAGE_TAG="semantist:experiment-$(date +%Y%m%d-%H%M%S)"
podman build --no-cache --format docker -f benchmarks/experiments/Containerfile -t "$IMAGE_TAG" .
RUN_ROOT="$PWD/artifacts/experiments/overall-$(date +%Y%m%dT%H%M%S)"
mkdir -p "$PWD/artifacts/experiments"
mkdir "$RUN_ROOT"
python3 -m fuzzer.cli.experiment prepare --execute --allow-formal \
  --experiment overall --root "$RUN_ROOT" --image "$IMAGE_TAG" \
  --llm-env-file "$PWD/.env.semantist-llm" \
  --build-jobs 8 --plan-jobs 4 \
  2>&1 | tee "$RUN_ROOT/prepare.log"
python3 -m fuzzer.cli.experiment run --execute --allow-formal \
  --experiment overall --tool semantist --root "$RUN_ROOT" --image "$IMAGE_TAG" \
  --schedule standard --jobs 5 --memory 8g \
  2>&1 | tee "$RUN_ROOT/run-semantist.log"
python3 -m fuzzer.cli.experiment run --execute --allow-formal \
  --experiment overall --tool aflpp --root "$RUN_ROOT" --image "$IMAGE_TAG" \
  --schedule standard --jobs 5 --memory 8g \
  2>&1 | tee "$RUN_ROOT/run-aflpp.log"
python3 -m fuzzer.cli.experiment run --execute --allow-formal \
  --experiment overall --tool icsquartz --root "$RUN_ROOT" --image "$IMAGE_TAG" \
  --schedule standard --jobs 5 --memory 8g \
  2>&1 | tee "$RUN_ROOT/run-icsquartz.log"
python3 -m fuzzer.cli.experiment summary --experiment overall \
  --root "$RUN_ROOT"
```

Smoke mode permits at most two targets, six jobs, and ten seconds of online
search per job. Full 300/600-second searches require both `--execute` and
`--allow-formal`. Pass `--llm-env-file` when preparing full SemantiST plans.
Builds are shared per target; planning runs once per target; each search gets an
independent working directory and seed. This root is under `/home` (the `/home`
disk), inside the repository's `artifacts/experiments/`; all five commands use
the same root. A changed SemantiST implementation requires a new image tag and
new experiment root for its five-seed rerun. `--retry-failed`
preserves old directories with a `.previous-N` suffix before restarting.
For a failed build or frozen plan, inspect `logs/builds/` or `logs/plans/`,
then repeat the same prepare command with `--retry-failed` before any run.
Each `requests/...json` records the target, tool, configuration, seed, online
budget, frozen build/plan package paths, output directory, two assigned
physical CPU IDs and memory limit.

One trial is one target, one tool, one random seed and one online budget.
The three formal commands each select exactly 500 jobs, with at most five
concurrent trials. SemantiST uses physical CPU IDs 0-9, AFL++ 10-19,
and ICSQuartz 20-29. Each trial gets a distinct pair in its tool's pool and an
8 GiB container limit. The scheduler verifies these IDs are available and
distinct physical cores. Do not begin any run until the single prepare command
has completed all 100 shared builds, 200 baseline adapter builds and 100
frozen SemantiST plans.
Avoid heavy host work on CPUs 0-29 and sibling threads 64-93 during the run.
The root remains on the `/home` disk; preserve it for summary and reruns.

Each worker sets and checks affinity to its two distinct physical cores; the
host launcher also uses `taskset`. Podman applies a two-CPU quota and an 8 GiB
memory limit without requiring a delegated `cpuset` cgroup controller. Search containers
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
in-process native probes. The scheduler and explicit `summary` command write
`summary.json` and `per-target.json` from the same frozen root. Formal per-tool
`run` commands write only their own trial results, so three processes can run
concurrently; execute `summary` after all three have exited.

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
search trial is scheduled. AFL++ copies its independent raw seed inside its
online timer.

The online wall clock starts before SemantiST campaign initialization and
before each baseline search process launches. It includes engine initialization,
startup symbolic work and SemantiST online KLEE. No trial gets more than its
300-second online window. Rust uses that same monotonic deadline; engine exit
after the deadline is recorded as `engine_postdeadline`. Native confirmation
and root-cause review follow in a separate phase.
Rust candidate metadata records when a candidate was emitted. The default
search records native edge novelty, state progress, finding candidates, and
mutation/corpus counts in `metrics/engine-summary.json` and
`metrics/events.jsonl`; the current full configuration retains bounded online
KLEE queries and feedback reinjection.
The result excludes candidates emitted after the online endpoint and reports
their count separately; the raw campaign report remains available. Each
finding's `first_candidate_online_s` is the primary time metric. Only candidates
created inside 300 seconds and confirmed by post-run native replay contribute
to the primary discovery rate. `preparation_costs_s` separates common build,
baseline adapter build, LLM planning, offline KLEE and planning review.
`first_candidate_end_to_end_s` adds an amortized preparation share: common
build/15 across all tools and seeds, plus tool-specific adapter or SemantiST
plan costs/5, to the online candidate time. The unamortized alternative is
`first_candidate_unamortized_s`. Post-run native review, shared engine bootstrap
and image installation remain separate, not part of either candidate time.

`summary.json` reports every planned run, including unsupported and failed
ones. It counts build and LLM planning costs once per target, and separately
reports offline KLEE, planning review, online/review time, CPU, memory,
throughput and feedback events. The shared build total appears in each tool
group for comparison and must not be summed across groups. A failed tool run
returns nonzero; inspect its `result.json` and log, fix only the failure, then
rerun that tool with `--retry-failed` on the same root. Completed trials are
never rerun by that flag. Rebuilds or code changes require a new root and
image, and old trials must not be mixed with the new five-seed results.
`per-target.json` reports each target's five-run discovery rate. `cases.json`
indexes each online confirmed finding and its reproducer across runs, with
root-cause IDs only where manually adjudicated. The
`independent_defects_adjudicated` field counts only manually assigned root
causes. To assign them, create `root-causes.json` in the experiment root with
keys listed in `summary.json`'s `unadjudicated_locations`, mapping each key to
a reviewed root-cause ID. Until then, independent defect counting is
incomplete. Smoke output is only an interface check, never a formal result.
